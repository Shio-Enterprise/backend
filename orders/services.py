from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from orders.correios import (
    fetch_shipping_deadline_by_service_and_ceps,
    fetch_shipping_price_by_service_and_ceps,
)
from orders.models import (
    Cart,
    CartItem,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentStatus,
    ShippingQuote,
)
from products.models import ProductVariation


def normalize_money(value):
    """Valida reais e arredonda centavos sem passar por ponto flutuante."""
    try:
        amount = Decimal(str(value).replace(",", "."))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Valor monetário inválido.")
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount > Decimal("99999999.99"):
            raise ValueError("Valor monetário acima do limite.")
        return amount
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Valor monetário inválido.") from exc


def money_to_cents(value):
    return int(normalize_money(value) * Decimal("100"))


class CheckoutShippingUnavailable(APIException):
    status_code = 503
    default_detail = "Serviço de cálculo de frete temporariamente indisponível."
    default_code = "shipping_unavailable"


def _get_checkout_contents(user, address_id, cart_id=None, *, lock=False):
    carts = Cart.objects.filter(user=user, status="ACTIVE")
    if lock:
        carts = carts.select_for_update()
    if cart_id is not None:
        carts = carts.filter(id=cart_id)
    cart = carts.first()
    if not cart:
        raise ValidationError({"message": "Carrinho vazio."})
    items_query = cart.items.select_related("variation__product").order_by(
        "variation_id"
    )
    if lock:
        items_query = items_query.select_for_update()
    cart_items = list(items_query)
    if not cart_items:
        raise ValidationError({"message": "Carrinho vazio."})
    addresses = user.addresses.filter(id=address_id)
    if lock:
        addresses = addresses.select_for_update()
    address = addresses.first()
    if not address:
        raise ValidationError({"message": "Endereço inválido."})

    items = []
    subtotal = Decimal("0.00")
    for item in cart_items:
        variation = item.variation
        if item.quantity < 1 or variation.stock_quantity < item.quantity:
            raise ValidationError(
                {"message": f"Estoque insuficiente para {variation.product.name}."}
            )
        try:
            unit_price = normalize_money(variation.product.base_price)
            total_price = normalize_money(unit_price * item.quantity)
            subtotal = normalize_money(subtotal + total_price)
        except ValueError as exc:
            raise ValidationError({"message": "Preço de produto inválido."}) from exc
        items.append(
            {
                "variation": variation,
                "cart_item_id": str(item.id),
                "cart_item_updated_at": item.updated_at.isoformat(),
                "variation_id": variation.id,
                "product_id": variation.product_id,
                "product_name": variation.product.name,
                "size": variation.size,
                "sku": variation.sku,
                "stock_quantity": variation.stock_quantity,
                "quantity": item.quantity,
                "unit_price": unit_price,
                "total_price": total_price,
            }
        )

    destination = address.zip_code.replace("-", "").strip()
    if len(destination) != 8 or not destination.isdigit():
        raise ValidationError({"message": "CEP do endereço inválido."})
    return {
        "cart": cart,
        "address": address,
        "items": items,
        "subtotal": subtotal,
        "shipping_parameters": {
            "origin": settings.CORREIOS_REMETENTE_CEP.replace("-", "").strip(),
            "destination": destination,
            "service": str(settings.CORREIOS_CODIGO_SERVICO),
            "weight": str(settings.CORREIOS_PESO_PADRAO_GRAMAS),
            "mock_enabled": settings.CORREIOS_MOCK_ENABLED,
            "base_url": settings.CORREIOS_API_BASE_URL,
        },
    }


def _calculate_checkout(contents):
    shipping_parameters = contents["shipping_parameters"]
    origin = shipping_parameters["origin"]
    destination = shipping_parameters["destination"]
    try:
        if len(origin) != 8 or not origin.isdigit():
            raise ValueError("CEP do remetente inválido.")
        price = fetch_shipping_price_by_service_and_ceps(
            shipping_parameters["service"],
            origin,
            destination,
            shipping_parameters["weight"],
        )
        shipping_cost = normalize_money(price["pcFinal"])
        deadline = fetch_shipping_deadline_by_service_and_ceps(
            shipping_parameters["service"],
            origin,
            destination,
        )
        deadline_days = deadline.get("prazoEntrega")
        if deadline_days is not None:
            deadline_days = int(deadline_days)
            if deadline_days < 0:
                raise ValueError("Prazo de entrega inválido.")
    except Exception as exc:
        raise CheckoutShippingUnavailable() from exc

    # Desconto PIX em espera: nenhuma promoção é aplicada apenas pela interface.
    discount_amount = Decimal("0.00")
    try:
        total_amount = normalize_money(
            contents["subtotal"] + shipping_cost - discount_amount
        )
    except ValueError as exc:
        raise ValidationError({"message": "Total do pedido acima do limite."}) from exc
    return {
        **contents,
        "shipping_cost": shipping_cost,
        "discount_amount": discount_amount,
        "total_amount": total_amount,
        "prazo_dias": deadline_days,
    }


def calculate_checkout(user, address_id):
    """Calcula a compra atual sem salvar cotação, pedido ou alterar estoque."""
    return _calculate_checkout(_get_checkout_contents(user, address_id))


def _shipping_quote_snapshot(contents):
    address = contents["address"]
    return {
        "items": sorted(
            [
                {
                    "cart_item_id": item["cart_item_id"],
                    "updated_at": item["cart_item_updated_at"],
                    "variation_id": str(item["variation_id"]),
                    "product_id": str(item["product_id"]),
                    "product_name": item["product_name"],
                    "product_updated_at": item[
                        "variation"
                    ].product.updated_at.isoformat(),
                    "product_is_active": item["variation"].product.is_active,
                    "quantity": item["quantity"],
                    "unit_price": str(item["unit_price"]),
                    "total_price": str(item["total_price"]),
                    "size": item["size"],
                    "color": item["variation"].color,
                    "sku": item["sku"],
                }
                for item in contents["items"]
            ],
            key=lambda item: item["cart_item_id"],
        ),
        "address": {
            field: getattr(address, field)
            for field in (
                "zip_code",
                "street",
                "address_number",
                "complement",
                "neighborhood",
                "city",
                "state",
            )
        },
        "address_updated_at": address.updated_at.isoformat(),
        "shipping_parameters": contents["shipping_parameters"],
    }


def create_shipping_quote(user, address_id):
    """Persiste o cálculo do servidor sem criar pedido ou reservar estoque."""
    ttl = settings.SHIPPING_QUOTE_TTL_SECONDS
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ImproperlyConfigured(
            "SHIPPING_QUOTE_TTL_SECONDS deve ser inteiro positivo."
        )

    contents = _get_checkout_contents(user, address_id)
    snapshot = _shipping_quote_snapshot(contents)
    calculation = _calculate_checkout(contents)
    current = _get_checkout_contents(user, address_id, contents["cart"].id)
    if snapshot != _shipping_quote_snapshot(current):
        raise ValidationError(
            {
                "shipping_quote_id": "A compra mudou durante o cálculo. Calcule novamente."
            },
            code="quote_changed",
        )
    return ShippingQuote.objects.create(
        user=user,
        cart=contents["cart"],
        address=contents["address"],
        snapshot=snapshot,
        subtotal=calculation["subtotal"],
        shipping_cost=calculation["shipping_cost"],
        discount_amount=calculation["discount_amount"],
        total_amount=calculation["total_amount"],
        prazo_dias=calculation["prazo_dias"],
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )


def validate_shipping_quote(user, quote_id, cart_id, address_id, *, lock=False):
    """Valida o estado atual; não consome a cotação nem confirma o checkout."""
    try:
        quotes = ShippingQuote.objects.filter(id=quote_id, user=user)
        if lock:
            quotes = quotes.select_for_update()
        quote = quotes.first()
    except (DjangoValidationError, ValueError, TypeError):
        quote = None
    if quote is None:
        raise ValidationError(
            {"shipping_quote_id": "Cotação inválida."}, code="quote_invalid"
        )
    if str(quote.cart_id) != str(cart_id) or str(quote.address_id) != str(address_id):
        raise ValidationError(
            {
                "shipping_quote_id": "Cotação não pertence ao carrinho e endereço informados."
            },
            code="quote_mismatch",
        )
    if quote.invalidated_at is not None:
        raise ValidationError(
            {"shipping_quote_id": "Cotação invalidada. Calcule novamente."},
            code="quote_invalidated",
        )
    if quote.expires_at <= timezone.now():
        raise ValidationError(
            {"shipping_quote_id": "Cotação expirada. Calcule novamente."},
            code="quote_expired",
        )
    try:
        contents = _get_checkout_contents(
            user, quote.address_id, quote.cart_id, lock=lock
        )
        matches = quote.snapshot == _shipping_quote_snapshot(contents)
    except ValidationError:
        matches = False
    if not matches:
        ShippingQuote.objects.filter(pk=quote.pk, invalidated_at__isnull=True).update(
            invalidated_at=timezone.now()
        )
        raise ValidationError(
            {"shipping_quote_id": "A compra mudou. Calcule novamente."},
            code="quote_changed",
        )
    # A aquisição dos bloqueios pode ter aguardado outra transação.
    if quote.expires_at <= timezone.now():
        raise ValidationError(
            {"shipping_quote_id": "Cotação expirada. Calcule novamente."},
            code="quote_expired",
        )
    return quote


def checkout_from_shipping_quote(user, quote_id, address_id):
    """Deve ser chamado na transação do checkout para manter as linhas bloqueadas."""
    cart = Cart.objects.filter(user=user, status="ACTIVE").first()
    quote = validate_shipping_quote(
        user, quote_id, cart.pk if cart else None, address_id, lock=True
    )
    contents = _get_checkout_contents(user, address_id, quote.cart_id)
    # A validação mantém os itens, produtos e endereço bloqueados até o commit.
    return {
        **contents,
        "subtotal": quote.subtotal,
        "shipping_cost": quote.shipping_cost,
        "discount_amount": quote.discount_amount,
        "total_amount": quote.total_amount,
        "prazo_dias": quote.prazo_dias,
    }


def create_infinitepay_checkout(order, request):
    payment, _ = Payment.objects.get_or_create(
        order=order,
        defaults={"method": "CREDIT_CARD", "total_amount": order.total_amount},
    )

    items_data = []
    for item in order.items.select_related("variation", "variation__product"):
        items_data.append(
            {
                "quantity": item.quantity,
                "price": money_to_cents(item.unit_price),
                "description": item.product_name,
            }
        )

    if order.shipping_cost > 0:
        items_data.append(
            {
                "quantity": 1,
                "price": money_to_cents(order.shipping_cost),
                "description": "Frete",
            }
        )

    redirect_url = request.build_absolute_uri("/api/orders/pagamento-sucesso/")

    phone_number = ""
    try:
        profile = order.user.profile
        phone_number = profile.phone_number or ""
    except ObjectDoesNotExist:
        phone_number = ""

    payload = {
        "handle": settings.INFINITEPAY_HANDLE,
        "redirect_url": redirect_url,
        "order_nsu": str(order.id),
        "items": items_data,
        "customer": {
            "name": order.user.name,
            "email": order.user.email,
            "phone_number": phone_number,
        },
        "address": {
            "cep": order.shipping_zip_code,
            "street": order.shipping_street,
            "neighborhood": order.shipping_neighborhood,
            "number": order.shipping_number,
            "complement": order.shipping_complement or "",
        },
    }

    headers = {"Content-Type": "application/json"}

    response = requests.post(
        "https://api.checkout.infinitepay.io/links",
        json=payload,
        headers=headers,
        timeout=10,
    )

    response.raise_for_status()
    data = response.json()

    payment.status = PaymentStatus.PROCESSING
    payment.save()
    order.status = OrderStatus.AWAITING_PAYMENT
    order.save()

    return data.get("url")


def check_payment_status(order_nsu, transaction_nsu, slug):
    payload = {
        "handle": settings.INFINITEPAY_HANDLE,
        "order_nsu": order_nsu,
        "transaction_nsu": transaction_nsu,
        "slug": slug,
    }

    headers = {"Content-Type": "application/json"}

    response = requests.post(
        "https://api.checkout.infinitepay.io/payment_check",
        json=payload,
        headers=headers,
        timeout=10,
    )

    if response.status_code == 200:
        return response.json()
    return None


def get_or_create_user_cart(user):
    cart, _ = Cart.objects.select_for_update().get_or_create(user=user, status="ACTIVE")
    return cart


def get_cart_data(request):
    if request.user.is_authenticated:
        cart = Cart.objects.filter(user=request.user, status="ACTIVE").first()
        if not cart:
            return {"id": None, "items": [], "subtotal": Decimal("0.00")}

        items = []
        subtotal = Decimal("0.00")
        for item in cart.items.select_related("variation", "variation__product").all():
            total_price = item.quantity * item.unit_price
            subtotal += total_price
            items.append(
                {
                    "variation_id": item.variation.id,
                    "product_id": item.variation.product.id,
                    "product_name": item.variation.product.name,
                    "size": item.variation.size,
                    "sku": item.variation.sku,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "total_price": total_price,
                    "stock_quantity": item.variation.stock_quantity,
                }
            )
        return {
            "id": cart.id,
            "items": items,
            "subtotal": subtotal,
        }
    else:
        session_cart = request.session.get("cart", {})
        items = []
        subtotal = Decimal("0.00")
        invalid_variation_ids = []

        variation_ids = list(session_cart.keys())
        variations = ProductVariation.objects.filter(
            id__in=variation_ids
        ).select_related("product")
        variations_by_id = {str(v.id): v for v in variations}

        for var_id_str, item_data in session_cart.items():
            variation = variations_by_id.get(var_id_str)
            if not variation:
                invalid_variation_ids.append(var_id_str)
                continue

            quantity = item_data.get("quantity", 0)
            unit_price_str = item_data.get("unit_price")
            unit_price = (
                Decimal(unit_price_str)
                if unit_price_str
                else variation.product.base_price
            )

            total_price = quantity * unit_price
            subtotal += total_price

            items.append(
                {
                    "variation_id": variation.id,
                    "product_id": variation.product.id,
                    "product_name": variation.product.name,
                    "size": variation.size,
                    "sku": variation.sku,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total_price": total_price,
                    "stock_quantity": variation.stock_quantity,
                }
            )

        if invalid_variation_ids:
            for iv_id in invalid_variation_ids:
                session_cart.pop(iv_id, None)
            request.session["cart"] = session_cart
            request.session.modified = True

        return {
            "id": None,
            "items": items,
            "subtotal": subtotal,
        }


def add_item_to_cart(request, variation_id, quantity):
    if request.user.is_authenticated:
        with transaction.atomic():
            cart = get_or_create_user_cart(request.user)
            variation = get_object_or_404(
                ProductVariation.objects.select_for_update().select_related("product"),
                id=variation_id,
            )

            cart_item, _ = CartItem.objects.get_or_create(
                cart=cart,
                variation=variation,
                defaults={"quantity": 0, "unit_price": variation.product.base_price},
            )

            new_quantity = cart_item.quantity + quantity
            if new_quantity > variation.stock_quantity:
                raise ValueError(
                    f"Estoque insuficiente para {variation.product.name}. Disponível: {variation.stock_quantity}"
                )

            cart_item.quantity = new_quantity
            cart_item.unit_price = variation.product.base_price
            cart_item.save()
    else:
        variation = get_object_or_404(
            ProductVariation.objects.select_related("product"), id=variation_id
        )
        session_cart = request.session.get("cart", {})
        var_id_str = str(variation.id)

        current_data = session_cart.get(var_id_str, {})
        current_quantity = current_data.get("quantity", 0)
        new_quantity = current_quantity + quantity

        if new_quantity > variation.stock_quantity:
            raise ValueError(
                f"Estoque insuficiente para {variation.product.name}. Disponível: {variation.stock_quantity}"
            )

        session_cart[var_id_str] = {
            "quantity": new_quantity,
            "unit_price": str(variation.product.base_price),
        }
        request.session["cart"] = session_cart
        request.session.modified = True


@transaction.atomic
def update_item_quantity(request, variation_id, quantity):
    cart = (
        get_or_create_user_cart(request.user) if request.user.is_authenticated else None
    )
    variation = get_object_or_404(
        ProductVariation.objects.select_related("product"), id=variation_id
    )

    if quantity > variation.stock_quantity:
        raise ValueError(
            f"Estoque insuficiente para {variation.product.name}. Disponível: {variation.stock_quantity}"
        )

    if request.user.is_authenticated:
        cart_item = get_object_or_404(CartItem, cart=cart, variation=variation)
        cart_item.quantity = quantity
        cart_item.unit_price = variation.product.base_price
        cart_item.save()
    else:
        session_cart = request.session.get("cart", {})
        var_id_str = str(variation.id)
        if var_id_str not in session_cart:
            raise KeyError("Item não está no carrinho.")

        session_cart[var_id_str]["quantity"] = quantity
        session_cart[var_id_str]["unit_price"] = str(variation.product.base_price)
        request.session["cart"] = session_cart
        request.session.modified = True


@transaction.atomic
def remove_item_from_cart(request, variation_id):
    if request.user.is_authenticated:
        cart = (
            Cart.objects.select_for_update()
            .filter(user=request.user, status="ACTIVE")
            .first()
        )
        if not cart:
            raise KeyError("Carrinho não encontrado.")
        cart_item = get_object_or_404(CartItem, cart=cart, variation_id=variation_id)
        cart_item.delete()
    else:
        session_cart = request.session.get("cart", {})
        var_id_str = str(variation_id)
        if var_id_str not in session_cart:
            raise KeyError("Item não está no carrinho.")
        session_cart.pop(var_id_str)
        request.session["cart"] = session_cart
        request.session.modified = True


@transaction.atomic
def clear_cart(request):
    if request.user.is_authenticated:
        cart = (
            Cart.objects.select_for_update()
            .filter(user=request.user, status="ACTIVE")
            .first()
        )
        if cart:
            cart.items.all().delete()
    else:
        request.session["cart"] = {}
        request.session.modified = True


def merge_session_cart_to_db(request, user):
    session_cart = request.session.get("cart", {})
    if not session_cart:
        return
    with transaction.atomic():
        db_cart = get_or_create_user_cart(user)
        variation_ids = list(session_cart.keys())
        # Filter only valid UUIDs to avoid ValidationError on UUIDField
        valid_ids = []
        for vid in variation_ids:
            try:
                valid_ids.append(UUID(str(vid)))
            except Exception:
                continue

        if valid_ids:
            variations = (
                ProductVariation.objects.select_for_update()
                .filter(id__in=valid_ids)
                .select_related("product")
            )
        else:
            variations = ProductVariation.objects.none()
        variations_by_id = {str(v.id): v for v in variations}

        for var_id_str, item_data in session_cart.items():
            variation = variations_by_id.get(var_id_str)
            if not variation:
                continue

            quantity = item_data.get("quantity", 0)
            cart_item, _ = CartItem.objects.get_or_create(
                cart=db_cart,
                variation=variation,
                defaults={"quantity": 0, "unit_price": variation.product.base_price},
            )

            new_quantity = cart_item.quantity + quantity
            if new_quantity > variation.stock_quantity:
                new_quantity = variation.stock_quantity

            cart_item.quantity = new_quantity
            cart_item.unit_price = variation.product.base_price
            cart_item.save()

        request.session.pop("cart", None)
        request.session.modified = True
        try:
            request.session.save()
        except Exception:
            # In some contexts session backend may not support save here;
            # ensure modified flag is set so caller can persist if needed.
            pass


def update_status(order, new_status, changed_by=None, tracking_code=None, comment=None):
    """Centraliza a atualização de status de pedidos e cria um log histórico.

    Args:
        order: CustomerOrder instance
        new_status: OrderStatus value
        changed_by: User who made the change (optional)
        tracking_code: optional tracking code to save
        comment: optional comment/observation
    """
    previous_status = order.status

    if tracking_code:
        order.tracking_code = tracking_code

    order.status = new_status
    order.save(update_fields=["tracking_code", "status", "updated_at"])

    try:
        payment = order.payment
    except Exception:
        payment = None

    if new_status == OrderStatus.CANCELED and payment:
        if payment.status != PaymentStatus.PAID:
            payment.status = PaymentStatus.FAILED
            payment.save()

    OrderStatusLog.objects.create(
        order=order,
        changed_by=changed_by,
        previous_status=previous_status,
        new_status=new_status,
        tracking_code=tracking_code,
        comment=comment,
    )
