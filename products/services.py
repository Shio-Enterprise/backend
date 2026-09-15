"""Atomic catalog and inventory operations (DF-001)."""

import re
import uuid

from django.db import IntegrityError, transaction
from django.db.models import Max
from rest_framework.exceptions import APIException, ValidationError

from .models import Product, ProductVariation, StockMovement, StockOpeningBalance


class OperationConflict(APIException):
    status_code = 409
    default_detail = "Operação já utilizada com dados diferentes."


def normalize_variation(data):
    data = dict(data)
    size = " ".join(data.get("size", "Único").split()) or "Único"
    data["size"] = "Único" if size.casefold() in ("unico", "único") else size
    color = " ".join(data.get("color", "").split())
    if color.startswith("#"):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValidationError({"color": "Use #RRGGBB."})
        color = color.upper()
    data["color"] = color
    sku = data.get("sku", "").strip().upper()
    if sku and not re.fullmatch(r"[A-Z0-9_-]{1,100}", sku):
        raise ValidationError(
            {"sku": "Use até 100 letras, números, hífen ou sublinhado."}
        )
    data["sku"] = sku
    return data


@transaction.atomic
def move_stock(
    *,
    variation,
    kind,
    reason,
    quantity,
    origin_type,
    origin_id,
    idempotency_key,
    created_by=None,
    note="",
    order_item=None,
    reverses_movement=None,
):
    variation = ProductVariation.objects.select_for_update().get(pk=variation.pk)
    payload = dict(
        variation_id=variation.pk,
        kind=kind,
        reason=reason,
        quantity=quantity,
        origin_type=origin_type,
        origin_id=uuid.UUID(str(origin_id)),
        note=note,
        order_item_id=getattr(order_item, "pk", None),
        reverses_movement_id=getattr(reverses_movement, "pk", None),
        created_by_id=getattr(created_by, "pk", None),
    )
    existing = StockMovement.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if any(getattr(existing, k) != v for k, v in payload.items()):
            raise OperationConflict()
        return existing
    if quantity <= 0 or kind not in ("ENTRADA", "SAIDA"):
        raise ValidationError(
            {"quantity": "Quantidade deve ser positiva e o tipo válido."}
        )
    if origin_type == "MANUAL_ADJUSTMENT" and (not created_by or not note.strip()):
        raise ValidationError({"note": "Informe a justificativa do ajuste."})
    if origin_type == "ORDER" and (
        not order_item
        or order_item.order_id != payload["origin_id"]
        or order_item.variation_id != variation.pk
    ):
        raise ValidationError({"order_item": "Item incompatível com a origem."})
    if reverses_movement:
        if (
            reverses_movement.variation_id != variation.pk
            or reverses_movement.kind == kind
            or reverses_movement.quantity != quantity
            or reverses_movement.is_legacy
        ):
            raise ValidationError(
                {
                    "reverses_movement": "Compense a mesma quantidade e variação, com tipo oposto."
                }
            )
    balance = variation.stock_quantity + (quantity if kind == "ENTRADA" else -quantity)
    if balance < 0:
        raise ValidationError({"quantity": "Saída supera o estoque atual."})
    # Safety for imported/programmatically created variations without a ledger.
    if not variation.stock_movements.exists() and variation.stock_quantity:
        StockOpeningBalance.objects.get_or_create(
            variation=variation, defaults={"balance": variation.stock_quantity}
        )
    sequence = (
        variation.stock_movements.aggregate(value=Max("sequence"))["value"] or 0
    ) + 1
    try:
        with transaction.atomic():
            movement = StockMovement.objects.create(
                **payload,
                balance_after=balance,
                sequence=sequence,
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        raise OperationConflict("Operação ou compensação já registrada.") from None
    ProductVariation.objects.filter(pk=variation.pk).update(stock_quantity=balance)
    return movement


@transaction.atomic
def create_variation(product, data, actor=None):
    Product.objects.select_for_update().get(pk=product.pk)
    data = normalize_variation(data)
    quantity = data.pop("stock_quantity", 0)
    siblings = product.variations.all()
    if siblings.filter(size__iexact=data["size"], color__iexact=data["color"]).exists():
        raise ValidationError(
            {"variations": "Combinação de tamanho/cor já cadastrada."}
        )
    default = data["size"] == "Único" and not data["color"]
    if (default and siblings.exists()) or siblings.filter(
        size="Único", color=""
    ).exists():
        raise ValidationError(
            {
                "variations": "Edite a variação Único existente antes de adicionar combinações reais."
            }
        )
    manual = bool(data["sku"])
    for _ in range(5):
        if not manual:
            data["sku"] = f"SH-{uuid.uuid4().hex.upper()}"
        try:
            with transaction.atomic():
                variation = ProductVariation.objects.create(product=product, **data)
            break
        except IntegrityError:
            if manual:
                raise ValidationError({"sku": "SKU já cadastrado."}) from None
    else:
        raise OperationConflict("Não foi possível gerar um SKU; tente novamente.")
    if quantity:
        move_stock(
            variation=variation,
            kind="ENTRADA",
            reason="ESTOQUE_INICIAL",
            quantity=quantity,
            origin_type="INITIAL_STOCK",
            origin_id=variation.id,
            idempotency_key=f"initial:{variation.id}",
            created_by=actor,
        )
        variation.refresh_from_db()
    return variation
