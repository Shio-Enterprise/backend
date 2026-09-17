from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models, transaction
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
    CheckoutAttempt,
    CheckoutAttemptStatus,
    Coupon,
    CustomerOrder,
    OrderItem,
    OrderStatus,
    OrderStatusLog,
    Payment,
    PaymentStatus,
    ShippingQuote,
)
from products.availability import (
    get_drop_sold_quantity,
    is_product_open_for_sale,
    is_product_sellable,
)
from products.models import DropCampaign, Product, ProductVariation, StockMovement
from products.services import move_stock

ALLOWED_TRANSITIONS = {
    OrderStatus.AWAITING_PAYMENT: {
        OrderStatus.PAID,
        OrderStatus.CANCELED,
    },
    OrderStatus.PAID: {
        OrderStatus.PREPARING,
    },
    OrderStatus.PREPARING: {
        OrderStatus.SHIPPED,
    },
    OrderStatus.SHIPPED: {
        OrderStatus.DELIVERED,
    },
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELED: set(),
}


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
    items_query = cart.items.select_related(
        "variation__product", "variation__product__drop"
    ).order_by("variation_id")
    if lock:
        products = list(
            Product.objects.select_for_update()
            .filter(pk__in=cart.items.values_list("variation__product_id", flat=True))
            .order_by("pk")
        )
        drop_ids = sorted({product.drop_id for product in products if product.drop_id})
        list(
            DropCampaign.objects.select_for_update()
            .filter(id__in=drop_ids)
            .order_by("id")
        )
        items_query = items_query.select_for_update(of=("self", "variation"))
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
    priced_at = timezone.now()
    for item in cart_items:
        variation = item.variation
        if item.quantity < 1 or variation.stock_quantity < item.quantity:
            raise ValidationError(
                {"message": f"Estoque insuficiente para {variation.product.name}."}
            )
        product_available = (
            is_product_open_for_sale(variation.product)
            if lock
            else is_product_sellable(variation.product)
        )
        if not product_available:
            raise ValidationError({"message": "Produto indisponível."})
        try:
            unit_price = normalize_money(variation.product.price_at(priced_at))
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
                "base_price": variation.product.base_price,
                "is_promotion_active": variation.product.promotion_active_at(priced_at),
                "is_sellable": product_available,
                "total_price": total_price,
            }
        )

    coupon, discount = _compute_welcome_discount(user, subtotal, lock=lock)
    destination = address.zip_code.replace("-", "").strip()
    if len(destination) != 8 or not destination.isdigit():
        raise ValidationError({"message": "CEP do endereço inválido."})
    return {
        "cart": cart,
        "address": address,
        "items": items,
        "subtotal": subtotal,
        "coupon": coupon,
        "discount_amount": discount,
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

    discount_amount = contents["discount_amount"]
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
                    "base_price": str(item["base_price"]),
                    "is_promotion_active": item["is_promotion_active"],
                    "is_sellable": item["is_sellable"],
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
        "coupon": (
            {
                "id": str(contents["coupon"].pk),
                "type": contents["coupon"].discount_type,
                "value": str(contents["coupon"].discount_value),
                "expiration": str(contents["coupon"].expiration_date),
            }
            if contents["coupon"]
            else None
        ),
        "discount_amount": str(contents["discount_amount"]),
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
    contents = _get_checkout_contents(user, address_id, quote.cart_id, lock=True)
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

    if order.discount_amount > 0:
        # Linha negativa para que o total cobrado pelo gateway bata com
        # order.total_amount (subtotal - desconto + frete). Sem ela, o cliente
        # veria o desconto na UI mas seria cobrado o valor cheio.
        items_data.append(
            {
                "quantity": 1,
                "price": -int(order.discount_amount * 100),
                "description": "Desconto de boas-vindas",
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
        allow_redirects=False,
    )

    response.raise_for_status()
    data = response.json()
    checkout_url = data.get("url") if isinstance(data, dict) else None
    if (
        not isinstance(checkout_url, str)
        or not checkout_url.startswith("https://")
        or len(checkout_url) > 2048
    ):
        raise ValueError("Resposta de checkout sem URL válida.")

    Payment.objects.filter(pk=payment.pk, status=PaymentStatus.PENDING).update(
        status=PaymentStatus.PROCESSING, updated_at=timezone.now()
    )
    return checkout_url


def checkout_attempt_result(attempt):
    if (
        attempt.status == CheckoutAttemptStatus.PROCESSING
        and attempt.created_at
        + timedelta(seconds=settings.CHECKOUT_PROCESSING_TIMEOUT_SECONDS)
        <= timezone.now()
    ):
        CheckoutAttempt.objects.filter(
            pk=attempt.pk, status=CheckoutAttemptStatus.PROCESSING
        ).update(status=CheckoutAttemptStatus.UNCERTAIN, updated_at=timezone.now())
        attempt.refresh_from_db()
    body = {
        "success": attempt.status == CheckoutAttemptStatus.SUCCEEDED,
        "idempotency_key": str(attempt.idempotency_key),
        "order_id": str(attempt.order_id),
        "status": attempt.status,
    }
    if attempt.status == CheckoutAttemptStatus.SUCCEEDED:
        return {**body, "checkout_url": attempt.checkout_url}, 201
    if attempt.status == CheckoutAttemptStatus.UNCERTAIN:
        return {
            **body,
            "message": "Não foi possível confirmar a criação do pagamento. O pedido foi preservado para conferência; não inicie outra compra para substituí-lo.",
        }, 503
    return {
        **body,
        "message": "Seu checkout está sendo processado. Consulte esta mesma tentativa novamente.",
    }, 202


def prepare_checkout_attempt(user, address_id, shipping_quote_id, idempotency_key):
    """Confirma o registro local antes do envio externo; reenvios nunca reenviam ao gateway."""
    with transaction.atomic():
        # Serializa também a primeira requisição, quando ainda não há tentativa para bloquear.
        get_user_model().objects.select_for_update().get(pk=user.pk)
        attempt = CheckoutAttempt.objects.filter(
            user=user, idempotency_key=idempotency_key
        ).first()
        if attempt:
            if (
                attempt.shipping_quote_id != shipping_quote_id
                or attempt.address_id != address_id
            ):
                return None, (
                    {
                        "message": "Chave de idempotência já utilizada para outra compra.",
                        "code": "idempotency_conflict",
                    },
                    409,
                )
            return None, checkout_attempt_result(attempt)
        quote = ShippingQuote.objects.filter(pk=shipping_quote_id, user=user).first()
        existing = (
            CheckoutAttempt.objects.filter(cart_id=quote.cart_id, user=user).first()
            if quote
            else None
        )
        if existing:
            return None, (
                {
                    "message": "Este carrinho já possui uma tentativa de checkout. Consulte a tentativa original.",
                    "code": "checkout_already_started",
                    "attempt": {
                        "idempotency_key": str(existing.idempotency_key),
                        "shipping_quote_id": str(existing.shipping_quote_id),
                        "address_id": str(existing.address_id),
                    },
                },
                409,
            )
        try:
            calculation = checkout_from_shipping_quote(
                user, shipping_quote_id, address_id
            )
        except ValidationError as exc:
            return None, (exc.detail, 400)
        cart = calculation["cart"]
        address = calculation["address"]

        requested_quantity_by_drop: dict[object, int] = {}
        for item in calculation["items"]:
            drop_id = item["variation"].product.drop_id
            if drop_id:
                requested_quantity_by_drop[drop_id] = (
                    requested_quantity_by_drop.get(drop_id, 0) + item["quantity"]
                )
        for drop_id, requested_quantity in requested_quantity_by_drop.items():
            drop = DropCampaign.objects.get(pk=drop_id)
            if drop.max_quantity is None:
                continue
            sold_quantity = get_drop_sold_quantity(drop)
            if sold_quantity + requested_quantity > drop.max_quantity:
                remaining = max(drop.max_quantity - sold_quantity, 0)
                return None, (
                    {
                        "success": False,
                        "message": (
                            f"Quantidade solicitada do drop '{drop.name}' excede o "
                            f"limite disponível. Restam {remaining} unidade(s)."
                        ),
                    },
                    409,
                )

        order = CustomerOrder.objects.create(
            user=user,
            address=address,
            subtotal=calculation["subtotal"],
            shipping_cost=calculation["shipping_cost"],
            discount_amount=calculation["discount_amount"],
            coupon=calculation["coupon"],
            total_amount=calculation["total_amount"],
            shipping_zip_code=address.zip_code,
            shipping_street=address.street,
            shipping_number=address.address_number,
            shipping_complement=address.complement,
            shipping_neighborhood=address.neighborhood,
            shipping_city=address.city,
            shipping_state=address.state,
        )
        for item in calculation["items"]:
            variation = item["variation"]
            order_item = OrderItem.objects.create(
                order=order,
                variation=variation,
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                product_name=(
                    f"{variation.product.name} - {variation.size}"
                    + (f" / {variation.color}" if variation.color else "")
                ),
                sku_snapshot=variation.sku,
            )
            move_stock(
                variation=variation,
                kind="SAIDA",
                reason="VENDA",
                quantity=item["quantity"],
                origin_type="ORDER",
                origin_id=order.pk,
                idempotency_key=f"sale:{order_item.pk}",
                order_item=order_item,
                created_by=user,
            )
        cart.status = "FINISHED"
        cart.save()
        Payment.objects.create(
            order=order, method="CREDIT_CARD", total_amount=order.total_amount
        )
        attempt = CheckoutAttempt.objects.create(
            user=user,
            idempotency_key=idempotency_key,
            cart=cart,
            order=order,
            shipping_quote_id=shipping_quote_id,
            address_id=address_id,
        )
    return attempt, None


def complete_checkout_attempt(attempt, request):
    # Somente o processo que persistiu a tentativa pode executar esta chamada.
    # Timeout, queda do processo ou falha ao salvar a resposta não autorizam novo POST externo.
    try:
        checkout_url = create_infinitepay_checkout(attempt.order, request)
        if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
            raise ValueError("URL de checkout inválida.")
    except Exception:
        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            status=CheckoutAttemptStatus.UNCERTAIN, updated_at=timezone.now()
        )
    else:
        CheckoutAttempt.objects.filter(pk=attempt.pk).update(
            status=CheckoutAttemptStatus.SUCCEEDED,
            checkout_url=checkout_url,
            updated_at=timezone.now(),
        )
    attempt.refresh_from_db()
    return checkout_attempt_result(attempt)


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
    price_at = timezone.now()
    if request.user.is_authenticated:
        cart = Cart.objects.filter(user=request.user, status="ACTIVE").first()
        if not cart:
            welcome_coupon, welcome_discount_amount = get_welcome_discount_preview(
                request.user, Decimal("0.00")
            )
            return {
                "id": None,
                "items": [],
                "subtotal": Decimal("0.00"),
                "eligible_for_welcome_discount": welcome_coupon is not None,
                "welcome_discount_amount": welcome_discount_amount,
            }

        items = []
        subtotal = Decimal("0.00")
        cart_items = cart.items.select_related(
            "variation", "variation__product", "variation__product__drop"
        ).all()
        for item in cart_items:
            item.unit_price = item.variation.product.price_at(price_at)

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
                    "base_price": item.variation.product.base_price,
                    "is_promotion_active": item.variation.product.promotion_active_at(
                        price_at
                    ),
                    "total_price": total_price,
                    "stock_quantity": item.variation.stock_quantity,
                    "is_sellable": is_product_sellable(item.variation.product),
                }
            )
        welcome_coupon, welcome_discount_amount = get_welcome_discount_preview(
            request.user, subtotal
        )
        return {
            "id": cart.id,
            "items": items,
            "subtotal": subtotal,
            "eligible_for_welcome_discount": welcome_coupon is not None,
            "welcome_discount_amount": welcome_discount_amount,
        }
    else:
        session_cart = request.session.get("cart", {})
        items = []
        subtotal = Decimal("0.00")
        invalid_variation_ids = []

        variation_ids = list(session_cart.keys())
        variations = ProductVariation.objects.filter(
            id__in=variation_ids
        ).select_related("product", "product__drop")
        variations_by_id = {str(v.id): v for v in variations}

        for var_id_str, item_data in session_cart.items():
            variation = variations_by_id.get(var_id_str)
            if not variation:
                invalid_variation_ids.append(var_id_str)
                continue

            quantity = item_data.get("quantity", 0)
            unit_price = variation.product.price_at(price_at)

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
                    "base_price": variation.product.base_price,
                    "is_promotion_active": variation.product.promotion_active_at(
                        price_at
                    ),
                    "total_price": total_price,
                    "stock_quantity": variation.stock_quantity,
                    "is_sellable": is_product_sellable(variation.product),
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
            "eligible_for_welcome_discount": False,
            "welcome_discount_amount": Decimal("0.00"),
        }


def add_item_to_cart(request, variation_id, quantity):
    if request.user.is_authenticated:
        with transaction.atomic():
            cart = get_or_create_user_cart(request.user)
            variation = get_object_or_404(
                ProductVariation.objects.select_for_update(of=("self",)).select_related(
                    "product", "product__drop"
                ),
                id=variation_id,
            )

            if not is_product_sellable(variation.product):
                raise ValueError(
                    f"{variation.product.name} não está disponível para compra no momento."
                )

            cart_item, _ = CartItem.objects.get_or_create(
                cart=cart,
                variation=variation,
                defaults={
                    "quantity": 0,
                    "unit_price": variation.product.effective_price,
                },
            )

            new_quantity = cart_item.quantity + quantity
            if new_quantity > variation.stock_quantity:
                raise ValueError(
                    f"Estoque insuficiente para {variation.product.name}. Disponível: {variation.stock_quantity}"
                )

            cart_item.quantity = new_quantity
            cart_item.unit_price = variation.product.effective_price
            cart_item.save()
    else:
        variation = get_object_or_404(
            ProductVariation.objects.select_related("product", "product__drop"),
            id=variation_id,
        )

        if not is_product_sellable(variation.product):
            raise ValueError(
                f"{variation.product.name} não está disponível para compra no momento."
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
            "unit_price": str(variation.product.effective_price),
        }
        request.session["cart"] = session_cart
        request.session.modified = True


@transaction.atomic
def update_item_quantity(request, variation_id, quantity):
    cart = (
        get_or_create_user_cart(request.user) if request.user.is_authenticated else None
    )
    variation = get_object_or_404(
        ProductVariation.objects.select_related("product", "product__drop"),
        id=variation_id,
    )

    if not is_product_sellable(variation.product):
        raise ValueError(
            f"{variation.product.name} não está disponível para compra no momento."
        )

    if quantity > variation.stock_quantity:
        raise ValueError(
            f"Estoque insuficiente para {variation.product.name}. Disponível: {variation.stock_quantity}"
        )

    if request.user.is_authenticated:
        cart_item = get_object_or_404(CartItem, cart=cart, variation=variation)
        cart_item.quantity = quantity
        cart_item.unit_price = variation.product.effective_price
        cart_item.save()
    else:
        session_cart = request.session.get("cart", {})
        var_id_str = str(variation.id)
        if var_id_str not in session_cart:
            raise KeyError("Item não está no carrinho.")

        session_cart[var_id_str]["quantity"] = quantity
        session_cart[var_id_str]["unit_price"] = str(variation.product.effective_price)
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
                defaults={
                    "quantity": 0,
                    "unit_price": variation.product.effective_price,
                },
            )

            new_quantity = cart_item.quantity + quantity
            if new_quantity > variation.stock_quantity:
                new_quantity = variation.stock_quantity

            cart_item.quantity = new_quantity
            cart_item.unit_price = variation.product.effective_price
            cart_item.save()

        request.session.pop("cart", None)
        request.session.modified = True
        try:
            request.session.save()
        except Exception:
            # In some contexts session backend may not support save here;
            # ensure modified flag is set so caller can persist if needed.
            pass


@transaction.atomic
def update_status(order, new_status, changed_by=None, tracking_code=None, comment=None):
    """Centraliza a atualização de status de pedidos e cria um log histórico.

    Args:
        order: CustomerOrder instance
        new_status: OrderStatus value
        changed_by: User who made the change (optional)
        tracking_code: optional tracking code to save
        comment: optional comment/observation
    """
    locked = CustomerOrder.objects.select_for_update().get(pk=order.pk)
    order.status = locked.status
    previous_status = order.status
    if new_status == OrderStatus.CANCELED:
        shipped = (
            previous_status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED)
            or order.status_logs.filter(
                new_status__in=[OrderStatus.SHIPPED, OrderStatus.DELIVERED]
            ).exists()
        )
        if not shipped:
            restore_order_stock(order, changed_by=changed_by)

    allowed = ALLOWED_TRANSITIONS.get(previous_status, set())

    if new_status not in allowed:
        raise ValidationError(
            {"status": (f"Transição inválida: {previous_status} -> {new_status}.")}
        )

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
            payment.save(update_fields=["status"])

    OrderStatusLog.objects.create(
        order=order,
        changed_by=changed_by,
        previous_status=previous_status,
        new_status=new_status,
        tracking_code=tracking_code,
        comment=comment,
    )

    return order


def update_tracking_code(order, tracking_code, changed_by=None, comment=None):
    if order.status != OrderStatus.SHIPPED:
        raise ValidationError(
            {
                "tracking_code": (
                    "O código de rastreio só pode ser alterado em pedidos enviados."
                )
            }
        )

    if not tracking_code or not tracking_code.strip():
        raise ValidationError({"tracking_code": "O código de rastreio é obrigatório."})

    order.tracking_code = tracking_code
    order.save(update_fields=["tracking_code", "updated_at"])

    return order


@transaction.atomic
def restore_order_stock(order, changed_by=None, physical_return=False):
    CustomerOrder.objects.select_for_update().get(pk=order.pk)
    if physical_return and not changed_by:
        raise ValueError("Retorno físico exige confirmação administrativa.")
    for item in order.items.order_by("variation_id"):
        sale = StockMovement.objects.filter(
            order_item=item, reason="VENDA", origin_type="ORDER"
        ).first()
        if not sale or StockMovement.objects.filter(reverses_movement=sale).exists():
            continue
        move_stock(
            variation=item.variation,
            kind="ENTRADA",
            reason="DEVOLUCAO",
            quantity=item.quantity,
            origin_type="ORDER",
            origin_id=order.pk,
            order_item=item,
            idempotency_key=f"return:{item.pk}",
            created_by=changed_by,
            note="Retorno físico confirmado"
            if physical_return
            else "Cancelamento antes da expedição",
            reverses_movement=sale,
        )


def _compute_welcome_discount(user, subtotal, *, lock=False):
    """Lógica compartilhada de elegibilidade e cálculo do desconto de
    boas-vindas, usada por get_welcome_discount e get_welcome_discount_preview.

    Retorna (coupon, discount_amount) ou (None, Decimal('0.00')) se o usuário
    não for elegível. NÃO adquire lock algum: quem precisa serializar contra
    checkouts concorrentes (get_welcome_discount) deve travar a linha do
    usuário ANTES de chamar esta função.

    Só considera o cupom BEMVINDO10 se ele estiver ativo e não expirado, e
    respeita o discount_type configurado (PERCENTAGE ou FIXED_VALUE), de modo
    que uma edição da linha do cupom no admin não seja silenciosamente
    ignorada.
    """
    if not user.is_authenticated:
        return None, Decimal("0.00")

    has_previous_order = CustomerOrder.objects.filter(user=user).exists()
    if has_previous_order:
        return None, Decimal("0.00")

    coupons = Coupon.objects.all()
    if lock:
        coupons = coupons.select_for_update()
    coupon = (
        coupons.filter(code="BEMVINDO10", is_active=True)
        .filter(
            models.Q(expiration_date__isnull=True)
            | models.Q(expiration_date__gt=timezone.now())
        )
        .first()
    )
    if not coupon:
        return None, Decimal("0.00")

    if coupon.discount_type == "FIXED_VALUE":
        # Nunca deixa o desconto ultrapassar o subtotal (total negativo).
        discount = min(coupon.discount_value, subtotal)
    else:
        discount = subtotal * (coupon.discount_value / Decimal("100"))

    return coupon, normalize_money(min(subtotal, max(Decimal("0.00"), discount)))


def get_welcome_discount(user, subtotal):
    """Retorna (coupon, discount_amount) para o desconto de boas-vindas,
    ou (None, Decimal('0.00')) se o usuário não for elegível.

    Deve ser chamada dentro de uma transaction.atomic() (o caller,
    CheckoutAPIView.post, já está decorado com @transaction.atomic).
    Faz o lock da linha do usuário via select_for_update() antes de delegar a
    checagem de elegibilidade a _compute_welcome_discount(): isso serializa
    dois checkouts concorrentes do mesmo usuário — a segunda transação só
    prossegue além do lock depois que a primeira commitar, e nesse ponto já
    enxerga o pedido criado pela primeira, evitando aplicar o desconto duas
    vezes.
    """
    if not user.is_authenticated:
        return None, Decimal("0.00")

    User = get_user_model()
    User.objects.select_for_update().get(pk=user.pk)

    return _compute_welcome_discount(user, subtotal)


def get_welcome_discount_preview(user, subtotal):
    """Versão somente-leitura de get_welcome_discount, para uso em contextos
    que não estão dentro de uma transaction.atomic() (ex.: GET /cart/, uma
    rota de leitura que apenas exibe uma prévia do desconto).

    Mesma assinatura e retorno de get_welcome_discount — (coupon, discount) ou
    (None, Decimal('0.00')) — e mesma lógica de elegibilidade (as duas delegam
    a _compute_welcome_discount), mas SEM select_for_update(): não adquire lock
    na linha do usuário, então não serializa contra checkouts concorrentes.
    Isso é aceitável aqui porque esta função só alimenta uma prévia informativa
    no carrinho; a aplicação real e segura contra corrida do desconto acontece
    em get_welcome_discount, chamada por CheckoutAPIView.post dentro de
    @transaction.atomic.
    """
    return _compute_welcome_discount(user, subtotal)
