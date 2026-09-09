from django.urls import path

from .correios_views import AgencySearchView, CepLookupView, ShippingOptionsView
from .views import (
    AdminDashboardView,
    AdminOrderDetailView,
    AdminOrderListView,
    CartAPIView,
    CartItemAddAPIView,
    CartItemDetailAPIView,
    CheckoutAPIView,
    CheckoutCalculationView,
    OrderDispatchView,
    OrderTrackingView,
    PaymentSuccessRedirectView,
    UserOrderDetailView,
    UserOrderListView,
)

app_name = "orders"

urlpatterns = [
    path("dashboard/summary/", AdminDashboardView.as_view(), name="dashboard_summary"),
    path("checkout/", CheckoutAPIView.as_view(), name="checkout"),
    path(
        "checkout/calculate/",
        CheckoutCalculationView.as_view(),
        name="checkout-calculate",
    ),
    path(
        "pagamento-sucesso/",
        PaymentSuccessRedirectView.as_view(),
        name="payment-success",
    ),
    path("my-orders/", UserOrderListView.as_view(), name="user_orders_list"),
    path(
        "my-orders/<uuid:order_id>/",
        UserOrderDetailView.as_view(),
        name="user_orders_detail",
    ),
    path("admin/", AdminOrderListView.as_view(), name="admin_orders_list"),
    path(
        "admin/<uuid:order_id>/",
        AdminOrderDetailView.as_view(),
        name="admin_orders_detail",
    ),
    path("correios/cep/<str:cep>/", CepLookupView.as_view(), name="cep-lookup"),
    path("correios/frete/", ShippingOptionsView.as_view(), name="shipping-options"),
    path("correios/agencias/", AgencySearchView.as_view(), name="agency-search"),
    path(
        "correios/<uuid:order_id>/tracking/",
        OrderTrackingView.as_view(),
        name="order-tracking",
    ),
    path(
        "correios/<uuid:order_id>/despachar/",
        OrderDispatchView.as_view(),
        name="order-dispatch",
    ),
    path("cart/", CartAPIView.as_view(), name="cart"),
    path("cart/items/", CartItemAddAPIView.as_view(), name="cart_add"),
    path(
        "cart/items/<uuid:variation_id>/",
        CartItemDetailAPIView.as_view(),
        name="cart_item_detail",
    ),
]
