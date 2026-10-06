"""Helpers para montar cenários de teste de avaliações."""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from authentication.models import UserProfile, UserRole
from orders.models import (
    CustomerOrder,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
)
from products.models import Product, ProductVariation

from .models import ProductReview, ReviewStatus

User = get_user_model()


def make_user(email=None, name="Cliente Teste", *, admin=False):
    user = User.objects.create_user(
        email=email or f"{uuid.uuid4().hex[:10]}@example.com", name=name
    )
    UserProfile.objects.create(
        user=user, role=UserRole.ADMIN if admin else UserRole.CUSTOMER
    )
    if admin:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    return user


def make_product(name="Camiseta", *, sizes=("M",), is_active=True):
    product = Product.objects.create(
        name=name,
        description="Produto de teste de avaliações",
        base_price="100.00",
        is_active=is_active,
    )
    variations = [
        ProductVariation.objects.create(
            product=product,
            size=size,
            sku=f"RV-{uuid.uuid4().hex[:12]}",
            stock_quantity=10,
        )
        for size in sizes
    ]
    return product, variations


def make_order_item(
    user,
    variation,
    *,
    order_status=OrderStatus.DELIVERED,
    payment_status=PaymentStatus.PAID,
    days_ago=0,
):
    order = CustomerOrder.objects.create(
        user=user,
        status=order_status,
        subtotal="100.00",
        total_amount="100.00",
        shipping_zip_code="70000-000",
        shipping_street="Rua Teste",
        shipping_number="1",
        shipping_neighborhood="Centro",
        shipping_city="Brasília",
        shipping_state="DF",
    )
    initial_status = (
        PaymentStatus.PAID
        if payment_status == PaymentStatus.REFUNDED
        else payment_status
    )
    payment = Payment.objects.create(
        order=order,
        method=PaymentMethod.PIX,
        status=initial_status,
        total_amount="100.00",
    )
    if payment_status == PaymentStatus.REFUNDED:
        payment.status = PaymentStatus.REFUNDED
        payment.save(update_fields=["status", "updated_at"])
    if days_ago:
        # Datas explícitas evitam empate de created_at entre pedidos do mesmo teste.
        CustomerOrder.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago)
        )
    return OrderItem.objects.create(
        order=order,
        variation=variation,
        quantity=1,
        unit_price="100.00",
        product_name=variation.product.name,
    )


def make_review(
    user,
    product,
    *,
    rating=5,
    comment="",
    fit="",
    status=ReviewStatus.PUBLISHED,
    order_item=None,
    purchased_size="M",
):
    """Cria direto no banco, sem recalcular a média (só para montar cenários)."""
    return ProductReview.objects.create(
        user=user,
        product=product,
        rating=rating,
        comment=comment,
        fit=fit,
        status=status,
        order_item=order_item,
        purchased_size=purchased_size,
    )
