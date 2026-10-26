from django.urls import path

from .correios_views import AgencySearchView, CepLookupView, ShippingOptionsView
from .views import (
    AdminDashboardView,
    AdminOrderDetailView,
    AdminOrderExportView,
    AdminOrderListView,
    AdminRefundApproveView,
    AdminRefundRejectView,
    AdminRefundView,
    CartAPIView,
    CartItemAddAPIView,
    CartItemDetailAPIView,
    CheckoutAPIView,
    CheckoutCalculationView,
    CouponDetailView,
    CouponEmailPreviewView,
    CouponEmailSendView,
    CouponEmailSummaryView,
    CouponEmailTestView,
    CouponListCreateView,
    DashboardDetailView,
    DashboardDrillDownView,
    DashboardDropRevenueView,
    InfinitePayWebhookView,
    OrderDispatchView,
    OrderTrackingView,
    PaymentSuccessRedirectView,
    UserOrderDetailView,
    UserOrderListView,
    UserRefundRequestView,
)

app_name = "orders"

urlpatterns = [
    path("admin/coupons/", CouponListCreateView.as_view(), name="admin-coupons-list"),
    path(
        "admin/coupons/<uuid:uuid>/",
        CouponDetailView.as_view(),
        name="admin-coupons-detail",
    ),
    path(
        "admin/coupons/<uuid:uuid>/email/",
        CouponEmailSummaryView.as_view(),
        name="admin-coupon-email",
    ),
    path(
        "admin/coupons/<uuid:uuid>/email/preview/",
        CouponEmailPreviewView.as_view(),
        name="admin-coupon-email-preview",
    ),
    path(
        "admin/coupons/<uuid:uuid>/email/test/",
        CouponEmailTestView.as_view(),
        name="admin-coupon-email-test",
    ),
    path(
        "admin/coupons/<uuid:uuid>/email/send/",
        CouponEmailSendView.as_view(),
        name="admin-coupon-email-send",
    ),
    path(
        "infinitepay/webhook/",
        InfinitePayWebhookView.as_view(),
        name="infinitepay-webhook",
    ),
    path("dashboard/summary/", AdminDashboardView.as_view(), name="dashboard_summary"),
    path("dashboard/detail/", DashboardDetailView.as_view(), name="dashboard_detail"),
    path(
        "dashboard/orders/", DashboardDrillDownView.as_view(), name="dashboard_orders"
    ),
    path(
        "dashboard/drop-revenue/",
        DashboardDropRevenueView.as_view(),
        name="dashboard_drop_revenue",
    ),
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
    path(
        "my-orders/<uuid:order_id>/refund-request/",
        UserRefundRequestView.as_view(),
        name="user_order_refund_request",
    ),
    path("admin/", AdminOrderListView.as_view(), name="admin_orders_list"),
    path("admin/export/", AdminOrderExportView.as_view(), name="admin_orders_export"),
    path(
        "admin/<uuid:order_id>/",
        AdminOrderDetailView.as_view(),
        name="admin_orders_detail",
    ),
    path(
        "admin/<uuid:order_id>/refund/",
        AdminRefundView.as_view(),
        name="admin_order_refund",
    ),
    path(
        "admin/<uuid:order_id>/refund/approve/",
        AdminRefundApproveView.as_view(),
        name="admin_order_refund_approve",
    ),
    path(
        "admin/<uuid:order_id>/refund/reject/",
        AdminRefundRejectView.as_view(),
        name="admin_order_refund_reject",
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
