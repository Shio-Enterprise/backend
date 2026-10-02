from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Avg, Count
from django.utils import timezone

from orders.models import OrderItem, OrderStatus
from products.models import Product

from .models import ProductReview, RemovalReason, ReviewFit, ReviewStatus

TWO_PLACES = Decimal("0.01")
EDITABLE_FIELDS = ("rating", "comment", "fit")


class ReviewError(Exception):
    """Erro de regra de negócio; as views traduzem para HTTP."""

    message = "Operação inválida."

    def __init__(self, message=None):
        self.message = message or self.message
        super().__init__(self.message)


class NotEligible(ReviewError):
    message = "Só é possível avaliar produtos de pedidos entregues."


class AlreadyReviewed(ReviewError):
    message = "Você já avaliou este produto."


class InvalidTransition(ReviewError):
    message = "Ação não permitida no status atual da avaliação."


class MissingRemovalNote(ReviewError):
    message = "Descreva o motivo quando escolher 'Outro'."


def recompute_product_rating(product):
    """Único ponto que escreve rating_avg/rating_count no produto."""
    with transaction.atomic():
        locked = Product.objects.select_for_update().get(pk=product.pk)
        stats = ProductReview.objects.filter(
            product_id=locked.pk, status=ReviewStatus.PUBLISHED
        ).aggregate(avg=Avg("rating"), count=Count("id"))
        avg = stats["avg"]
        locked.rating_avg = (
            Decimal(str(avg)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            if avg is not None
            else Decimal("0.00")
        )
        locked.rating_count = stats["count"]
        locked.save(update_fields=["rating_avg", "rating_count"])
    product.rating_avg = locked.rating_avg
    product.rating_count = locked.rating_count
    return product


def get_eligible_order_item(user, product):
    """Item entregue mais recente do produto; pagamento reembolsado não importa."""
    return (
        OrderItem.objects.filter(
            order__user=user,
            order__status=OrderStatus.DELIVERED,
            variation__product=product,
        )
        .select_related("variation")
        .order_by("-order__created_at", "-updated_at")
        .first()
    )


def _apply_purchase(review, item):
    review.order_item = item
    review.purchased_size = item.variation.size


def _clear_removal(review):
    review.removal_reason = ""
    review.removal_note = ""
    review.removed_by = None
    review.removed_at = None


def create_review(user, product, *, rating, comment="", fit=""):
    if not 1 <= rating <= 5:
        raise ValueError("Nota deve ser um inteiro de 1 a 5.")
    with transaction.atomic():
        item = get_eligible_order_item(user, product)
        if item is None:
            raise NotEligible()
        review = ProductReview(
            user=user,
            product=product,
            rating=rating,
            comment=(comment or "").strip(),
            fit=fit or "",
        )
        _apply_purchase(review, item)
        try:
            with transaction.atomic():
                review.save()
        except IntegrityError:
            raise AlreadyReviewed() from None
        recompute_product_rating(product)
    return review


def update_review(review, **changes):
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Campos não editáveis: {sorted(unknown)}")
    with transaction.atomic():
        for field, value in changes.items():
            if field == "comment":
                value = (value or "").strip()
            if field == "fit":
                value = value or ""
            setattr(review, field, value)
        item = get_eligible_order_item(review.user, review.product)
        if item is not None:
            _apply_purchase(review, item)
        elif (
            review.order_item_id
            and not OrderItem.objects.filter(pk=review.order_item_id).exists()
        ):
            # Instância em memória pode apontar para item já apagado (SET_NULL no banco).
            review.order_item = None
        if review.status == ReviewStatus.REMOVED:
            review.status = ReviewStatus.PUBLISHED
            _clear_removal(review)
        review.save()
        recompute_product_rating(review.product)
    return review


def delete_review(review):
    product = review.product
    with transaction.atomic():
        review.delete()
        recompute_product_rating(product)


def remove_review(review, *, admin, reason, note=""):
    note = (note or "").strip()
    if reason == RemovalReason.OTHER and not note:
        raise MissingRemovalNote()
    with transaction.atomic():
        if review.status == ReviewStatus.REMOVED:
            raise InvalidTransition("Avaliação já está removida.")
        review.status = ReviewStatus.REMOVED
        review.removal_reason = reason
        review.removal_note = note
        review.removed_by = admin
        review.removed_at = timezone.now()
        review.save()
        recompute_product_rating(review.product)
    return review


def restore_review(review):
    with transaction.atomic():
        if review.status != ReviewStatus.REMOVED:
            raise InvalidTransition("Avaliação já está publicada.")
        review.status = ReviewStatus.PUBLISHED
        _clear_removal(review)
        review.save()
        recompute_product_rating(review.product)
    return review


def set_reply(review, text):
    if review.status != ReviewStatus.PUBLISHED:
        raise InvalidTransition("Só é possível responder avaliações publicadas.")
    review.admin_reply = text.strip()
    review.admin_reply_at = timezone.now()
    review.save(update_fields=["admin_reply", "admin_reply_at", "updated_at"])
    return review


def clear_reply(review):
    review.admin_reply = ""
    review.admin_reply_at = None
    review.save(update_fields=["admin_reply", "admin_reply_at", "updated_at"])
    return review


def rating_summary(product):
    published = ProductReview.objects.filter(
        product=product, status=ReviewStatus.PUBLISHED
    )
    distribution = {str(n): 0 for n in range(1, 6)}
    for row in published.values("rating").annotate(total=Count("id")).order_by():
        distribution[str(row["rating"])] = row["total"]
    fit = {choice: 0 for choice in ReviewFit.values}
    for row in (
        published.exclude(fit="").values("fit").annotate(total=Count("id")).order_by()
    ):
        fit[row["fit"]] = row["total"]
    return {
        "rating_avg": product.rating_avg,
        "rating_count": product.rating_count,
        "distribution": distribution,
        "fit": fit,
    }
