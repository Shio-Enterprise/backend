import datetime
import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncDay, TruncMonth
from django.http import Http404
from django.utils import timezone
from django.utils.decorators import method_decorator
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.permissions import IsStaffOrSuperUser

from products.availability import get_drop_sold_quantity, is_product_open_for_sale
from products.models import DropCampaign, Product, ProductVariation
from products.services import move_stock

from .correios import (
    CorreiosAuthenticationError,
    CorreiosPrePostagemError,
    CorreiosTrackingUnavailableError,
    dispatch_order_and_get_tracking_code,
    get_order_tracking_data,
)
from .metrics import (
    METRICS_TIMEZONE,
    is_recurring_customer,
    metric_orders,
    positive_sales,
    refunds,
    resolve_period,
)
from .models import (
    CustomerOrder,
    OrderStatus,
    PaymentStatus,
)
from .serializers import (
    CartItemAddSerializer,
    CartItemUpdateSerializer,
    CartRepresentationSerializer,
    CheckoutCalculationInputSerializer,
    CheckoutCalculationSerializer,
    CheckoutInputSerializer,
    DashboardLowStockSerializer,
    DashboardRecentOrderSerializer,
    OrderDetailSerializer,
    OrderStatusUpdateSerializer,
)
from .services import (
    add_item_to_cart,
    check_payment_status,
    clear_cart,
    complete_checkout_attempt,
    create_shipping_quote,
    get_cart_data,
    prepare_checkout_attempt,
    remove_item_from_cart,
    restore_order_stock,
    update_item_quantity,
    update_status,
    update_tracking_code,
)

User = get_user_model()
logger = logging.getLogger(__name__)

METRIC_PARAMETERS = [
    OpenApiParameter("period", OpenApiTypes.STR, description="monthly (30 dias, padrão) ou annual"),
    OpenApiParameter("start_date", OpenApiTypes.DATE, description="Início inclusivo em America/Sao_Paulo"),
    OpenApiParameter("end_date", OpenApiTypes.DATE, description="Fim inclusivo em America/Sao_Paulo"),
    OpenApiParameter("drop", OpenApiTypes.UUID, description="ID do drop"),
    OpenApiParameter("category", OpenApiTypes.UUID, description="ID da categoria"),
    OpenApiParameter("customer", OpenApiTypes.UUID, description="ID do cliente"),
    OpenApiParameter(
        "search",
        OpenApiTypes.STR,
        description="Busca única por cliente/e-mail, produto, drop ou categoria",
    ),
]


class AdminDashboardView(APIView):
    """
    Endpoint consolidado para o Dashboard Administrativo (UC05).
    GET /api/orders/dashboard/summary/
    Restrito a is_staff ou is_superuser.
    """

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        description=(
            "Dashboard comercial. Venda positiva exige pagamento PAID e pedido "
            "DELIVERED; reembolsos subtraem CustomerOrder.total_amount. A competência "
            "é payment.paid_at em America/Sao_Paulo."
        ),
        parameters=METRIC_PARAMETERS,
        responses={
            200: inline_serializer(
                name="DashboardSummaryResponse",
                fields={
                    "sales_summary": inline_serializer(
                        name="DashboardSalesSummary",
                        fields={
                            "period_days": serializers.IntegerField(),
                            "total_revenue": serializers.DecimalField(
                                max_digits=10, decimal_places=2
                            ),
                            "gross_revenue": serializers.DecimalField(
                                max_digits=10, decimal_places=2
                            ),
                            "refunds": serializers.DecimalField(
                                max_digits=10, decimal_places=2
                            ),
                            "total_orders": serializers.IntegerField(),
                            "average_ticket": serializers.DecimalField(
                                max_digits=10, decimal_places=2
                            ),
                        },
                    ),
                    "customers_summary": inline_serializer(
                        name="DashboardCustomersSummary",
                        fields={
                            "total_registered": serializers.IntegerField(),
                            "new_in_period": serializers.IntegerField(),
                            "recurring_customers": serializers.IntegerField(),
                        },
                    ),
                    "period": serializers.DictField(),
                    "series": serializers.ListField(child=serializers.DictField()),
                    "recent_orders": DashboardRecentOrderSerializer(many=True),
                    "low_stock_alerts": DashboardLowStockSerializer(many=True),
                },
            )
        },
    )
    def get(self, request):
        start, end, granularity = resolve_period(request.query_params)
        orders = metric_orders(request.query_params, start, end)
        sales = positive_sales(orders)
        refunded = refunds(orders)

        gross_revenue = sales.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        refunded_revenue = refunded.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        total_revenue = gross_revenue - refunded_revenue
        total_orders = sales.count()
        average_ticket = total_revenue / total_orders if total_orders else Decimal("0")

        customer_filter = {"is_staff": False, "is_superuser": False}
        customers = User.objects.filter(**customer_filter)
        if request.query_params.get("search"):
            customers = customers.filter(id__in=orders.values("user_id"))
        if request.query_params.get("customer"):
            customers = customers.filter(id=request.query_params["customer"])
        total_clientes = customers.count()
        novos_clientes = customers.filter(created_at__gte=start, created_at__lt=end).count()

        recurring_clientes = sum(
            1
            for customer in customers
            if is_recurring_customer(customer, request.query_params)
        )

        recent_orders_qs = orders.select_related("user", "payment").order_by("-payment__paid_at")[:10]
        recent_orders = DashboardRecentOrderSerializer(recent_orders_qs, many=True).data

        trunc = TruncMonth("payment__paid_at", tzinfo=METRICS_TIMEZONE) if granularity == "month" else TruncDay("payment__paid_at", tzinfo=METRICS_TIMEZONE)
        positive_points = {
            row["bucket"]: row
            for row in sales.annotate(bucket=trunc).values("bucket").annotate(
                total_orders=Count("id", distinct=True), revenue=Sum("total_amount")
            )
        }
        refund_points = {
            row["bucket"]: row["revenue"]
            for row in refunded.annotate(bucket=trunc).values("bucket").annotate(revenue=Sum("total_amount"))
        }
        series = []
        for bucket in sorted(set(positive_points) | set(refund_points)):
            positive = positive_points.get(bucket, {})
            net = (positive.get("revenue") or Decimal("0")) - (refund_points.get(bucket) or Decimal("0"))
            series.append({
                "period": bucket.date().isoformat(),
                "total_orders": positive.get("total_orders", 0),
                "total_revenue": f"{net:.2f}",
            })

        low_stock_qs = (
            ProductVariation.objects.filter(stock_quantity__lt=10)
            .select_related("product")
            .order_by("stock_quantity")
        )
        search = request.query_params.get("search", "").strip()
        if search:
            low_stock_qs = low_stock_qs.filter(
                Q(product__name__icontains=search)
                | Q(product__drop__name__icontains=search)
                | Q(product__category__name__icontains=search)
            )
        low_stock_qs = low_stock_qs[:50]
        low_stock_alerts = DashboardLowStockSerializer(low_stock_qs, many=True).data

        return Response(
            {
                "sales_summary": {
                    "period_days": (end.date() - start.date()).days,
                    "gross_revenue": gross_revenue,
                    "refunds": refunded_revenue,
                    "total_revenue": round(total_revenue, 2),
                    "total_orders": total_orders,
                    "average_ticket": round(average_ticket, 2),
                },
                "customers_summary": {
                    "total_registered": total_clientes,
                    "new_in_period": novos_clientes,
                    "recurring_customers": recurring_clientes,
                },
                "period": {
                    "start_date": start.date(),
                    "end_date": (end - datetime.timedelta(microseconds=1)).date(),
                    "granularity": granularity,
                    "timezone": "America/Sao_Paulo",
                },
                "series": series,
                "recent_orders": recent_orders,
                "low_stock_alerts": low_stock_alerts,
            },
            status=status.HTTP_200_OK,
        )


class DashboardDrillDownView(APIView):
    """Pedidos que compõem cards e pontos do gráfico, usando a consulta das métricas."""

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        description=(
            "Lista paginada dos pedidos das métricas. Aceita period, start_date, "
            "end_date e busca textual. Ordena por paid_at decrescente."
        ),
        parameters=METRIC_PARAMETERS,
        responses={200: DashboardRecentOrderSerializer(many=True)},
    )
    def get(self, request):
        queryset = metric_orders(request.query_params).select_related("user", "payment").order_by("-payment__paid_at")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(DashboardRecentOrderSerializer(page, many=True).data)


class DashboardDropRevenueView(APIView):
    """Receita de itens por drop, deliberadamente sem frete e desconto."""

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        description=(
            "Soma quantity × unit_price dos OrderItem de vendas válidas. Frete e "
            "desconto pertencem apenas à receita total e não são rateados por drop."
        ),
        parameters=METRIC_PARAMETERS,
        responses={
            200: inline_serializer(
                name="DashboardDropRevenue",
                many=True,
                fields={
                    "drop_id": serializers.UUIDField(),
                    "drop_name": serializers.CharField(),
                    "revenue": serializers.DecimalField(max_digits=14, decimal_places=2),
                },
            )
        },
    )
    def get(self, request):
        start, end, _ = resolve_period(request.query_params)
        sales = positive_sales(metric_orders(request.query_params, start, end))
        item_total = ExpressionWrapper(
            F("items__quantity") * F("items__unit_price"),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )
        rows = (
            sales.exclude(items__variation__product__drop__isnull=True)
            .values("items__variation__product__drop_id", "items__variation__product__drop__name")
            .annotate(revenue=Sum(item_total))
            .order_by("items__variation__product__drop__name")
        )
        return Response([
            {"drop_id": row["items__variation__product__drop_id"],
             "drop_name": row["items__variation__product__drop__name"],
             "revenue": row["revenue"]}
            for row in rows
        ])


class UserOrderListView(APIView):
    """
    GET /api/orders/my-orders/
    Lista os pedidos do utilizador autenticado.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            CustomerOrder.objects.filter(user=request.user)
            .select_related("user")
            .order_by("-created_at")
        )
        data = DashboardRecentOrderSerializer(qs, many=True).data
        return Response(data, status=status.HTTP_200_OK)


class UserOrderDetailView(APIView):
    """
    GET /api/orders/my-orders/<order_id>/
    Detalha um pedido pertencente ao utilizador autenticado.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        try:
            order = (
                CustomerOrder.objects.select_related("user", "address", "payment")
                .prefetch_related("items__variation__product")
                .get(id=order_id, user=request.user)
            )
        except CustomerOrder.DoesNotExist:
            return Response({"message": "Pedido não encontrado."}, status=404)
        return Response(OrderDetailSerializer(order).data, status=status.HTTP_200_OK)


class AdminOrderListView(APIView):
    """
    GET /api/orders/admin/
    Lista pedidos para o painel admin com filtro por `status`.
    """

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="status",
                type=OpenApiTypes.STR,
                description="Filtra pedidos pelo status (ex: PAID, SHIPPED)",
                location=OpenApiParameter.QUERY,
            ),
        ],
        responses={200: DashboardRecentOrderSerializer(many=True)},
    )
    def get(self, request):
        status_q = request.query_params.get("status")
        qs = CustomerOrder.objects.select_related("user").order_by("-created_at")
        if status_q:
            qs = qs.filter(status=status_q)

        data = DashboardRecentOrderSerializer(qs, many=True).data
        return Response(data, status=status.HTTP_200_OK)


class AdminOrderDetailView(APIView):
    """
    GET / PATCH /api/orders/admin/{order_id}/
    Recupera detalhes do pedido e permite atualização de status/tracking.
    """

    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        responses={200: OrderDetailSerializer},
    )
    def get(self, request, order_id):
        try:
            order = (
                CustomerOrder.objects.select_related("user", "address", "payment")
                .prefetch_related("items__variation__product")
                .get(id=order_id)
            )
        except CustomerOrder.DoesNotExist:
            return Response({"message": "Pedido não encontrado."}, status=404)

        return Response(OrderDetailSerializer(order).data, status=status.HTTP_200_OK)

    @extend_schema(
        request=OrderStatusUpdateSerializer,
        responses={
            200: OrderDetailSerializer,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    def patch(self, request, order_id):
        try:
            order = CustomerOrder.objects.select_related("payment").get(id=order_id)
        except CustomerOrder.DoesNotExist:
            return Response({"message": "Pedido não encontrado."}, status=404)

        serializer = OrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        status_value = serializer.validated_data.get("status")
        tracking_code = serializer.validated_data.get("tracking_code")
        comment = serializer.validated_data.get("comment")

        # Rules: cannot cancel if payment already PAID
        if status_value == OrderStatus.CANCELED:
            if (
                hasattr(order, "payment")
                and order.payment
                and order.payment.status == PaymentStatus.PAID
            ):
                return Response(
                    {
                        "message": "Não é possível cancelar um pedido com pagamento confirmado."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # If shipping, require tracking code
        if status_value == OrderStatus.SHIPPED and not (
            tracking_code or order.tracking_code
        ):
            return Response(
                {"message": "Tracking code obrigatório ao enviar pedido."}, status=400
            )

        if serializer.validated_data.get("physical_return_confirmed") and not (
            order.status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED)
            or order.status_logs.filter(
                new_status__in=[OrderStatus.SHIPPED, OrderStatus.DELIVERED]
            ).exists()
        ):
            return Response(
                {"message": "Retorno físico exige expedição anterior."}, status=400
            )

        if (order.status == OrderStatus.SHIPPED and status_value == OrderStatus.SHIPPED):
            update_tracking_code(
                order=order,
                tracking_code=tracking_code,
                changed_by=request.user,
                comment=comment,
            )
        else:
            update_status(
                order=order,
                new_status=status_value,
                tracking_code=tracking_code,
                changed_by=request.user,
                comment=comment,
            )

        if (
            status_value == OrderStatus.CANCELED
            and hasattr(order, "payment")
            and order.payment
        ):
            if order.payment.status != PaymentStatus.PAID:
                order.payment.status = PaymentStatus.FAILED
                order.payment.save()

        if serializer.validated_data.get("physical_return_confirmed"):
            restore_order_stock(order, changed_by=request.user, physical_return=True)

        return Response(OrderDetailSerializer(order).data, status=status.HTTP_200_OK)


class CheckoutCalculationView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Calcular valores atuais da compra",
        description="Cria uma cotação com preços, frete e validade, sem criar pedido ou reservar estoque.",
        request=CheckoutCalculationInputSerializer,
        responses={
            200: CheckoutCalculationSerializer,
            400: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        serializer = CheckoutCalculationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        calculation = create_shipping_quote(
            request.user, serializer.validated_data["address_id"]
        )
        return Response(CheckoutCalculationSerializer(calculation).data)


@method_decorator(transaction.non_atomic_requests, name="dispatch")
class CheckoutAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Finalizar Compra e Gerar Link InfinitePay",
        description=(
            "Lê o carrinho ativo do utilizador autenticado e verifica o estoque disponível. "
            "Gera o pedido (CustomerOrder), faz o snapshot do endereço de entrega e debita o estoque. "
            "Por fim, comunica-se com a API da InfinitePay para gerar o link de checkout.\n\n"
            "**Fluxo:**\n"
            "1. Envie `address_id`, `shipping_quote_id` e `idempotency_key` (UUID da tentativa).\n"
            "2. O backend valida a cotação e gera a cobrança com os valores confirmados.\n"
            "3. O utilizador é redirecionado para a `checkout_url` retornada."
        ),
        request=CheckoutInputSerializer,
        responses={
            201: OpenApiTypes.OBJECT,
            202: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            409: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        serializer = CheckoutInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            attempt, result = prepare_checkout_attempt(
                request.user, **serializer.validated_data
            )
        except Exception:
            return Response(
                {
                    "success": False,
                    "message": "Não foi possível preparar o pedido. Reenvie a mesma tentativa.",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        if attempt is not None:
            result = complete_checkout_attempt(attempt, request)
        body, response_status = result
        headers = {"Retry-After": "3"} if response_status == 202 else {}
        return Response(body, status=response_status, headers=headers)


class PaymentSuccessRedirectView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Confirmação de Pagamento (Redirect InfinitePay)",
        description=(
            "Rota de fallback acessada pelo navegador do cliente após o pagamento na InfinitePay. "
            "Recebe os parâmetros via query string, consulta o status real da transação no servidor "
            "da InfinitePay e efetiva a baixa do pedido (muda status para PAID) caso aprovado.\n\n"
            "⚠️ *Não envia token JWT. O front-end deve exibir uma tela de 'Processando' ao carregar esta rota.*"
        ),
        parameters=[
            OpenApiParameter(
                name="order_nsu",
                type=str,
                location=OpenApiParameter.QUERY,
                description="UUID do pedido gerado no nosso sistema",
            ),
            OpenApiParameter(
                name="transaction_nsu",
                type=str,
                location=OpenApiParameter.QUERY,
                description="ID único da transação gerado pela InfinitePay",
            ),
            OpenApiParameter(
                name="slug",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Código da fatura gerado pela InfinitePay",
            ),
        ],
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request):
        order_nsu = request.query_params.get("order_nsu")
        transaction_nsu = request.query_params.get("transaction_nsu")
        slug = request.query_params.get("slug")

        if not all([order_nsu, transaction_nsu, slug]):
            return Response(
                {"message": "Faltam parâmetros de validação."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            order = CustomerOrder.objects.get(id=order_nsu)
        except CustomerOrder.DoesNotExist:
            return Response(
                {"message": "Pedido não encontrado."}, status=status.HTTP_404_NOT_FOUND
            )

        if order.status != OrderStatus.PAID:
            check_data = check_payment_status(order_nsu, transaction_nsu, slug)

            if check_data and check_data.get("paid") is True:
                order.payment.gateway_transaction_id = transaction_nsu
                order.payment.status = PaymentStatus.PAID
                order.payment.save()
                order.status = OrderStatus.PAID
                order.save()

        if order.status == OrderStatus.PAID:
            return Response(
                {"message": "Pagamento confirmado com sucesso!", "order_id": order_nsu}
            )

        return Response(
            {
                "message": "Pagamento pendente ou em processamento.",
                "order_id": order_nsu,
            }
        )


class OrderTrackingView(APIView):
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        # PATCH must be restricted to admin users
        if self.request.method == "PATCH":
            return [IsStaffOrSuperUser()]
        return [IsAuthenticated()]

    @extend_schema(
        tags=["Correios"],
        summary="Rastreio do Pedido via Correios",
        description=(
            "Consulta o histórico de rastreamento de um pedido via API dos Correios.\n\n"
            "- **Cliente autenticado**: pode consultar apenas os seus próprios pedidos.\n"
            "- **Admin**: pode consultar qualquer pedido.\n\n"
            "Retorna `status: not_shipped` se o pedido ainda não possui código de rastreio."
        ),
        responses={
            200: OpenApiTypes.OBJECT,
            403: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def get(self, request, order_id):
        order = self.get_order_if_user_has_permission(request.user, order_id)
        if order is None:
            return Response(
                {"message": "Pedido não encontrado."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            tracking_data = get_order_tracking_data(order.tracking_code)
            return Response(tracking_data, status=status.HTTP_200_OK)
        except (CorreiosAuthenticationError, CorreiosTrackingUnavailableError) as e:
            logger.warning(
                "Falha conhecida nos Correios para o pedido %s: %s", order_id, e
            )
            return Response(
                {"message": "Serviço de rastreio temporariamente indisponível."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception(
                "Erro inesperado ao consultar rastreio para pedido %s", order_id
            )
            return Response(
                {"message": "Erro interno ao processar rastreio."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    @extend_schema(
        tags=["Correios"],
        summary="Registrar Código de Rastreio (Admin)",
        description=(
            "Vincula um código de rastreio dos Correios a um pedido e atualiza o status para `SHIPPED`.\n\n"
            "Restrito a administradores."
        ),
        request={
            "application/json": {
                "type": "object",
                "required": ["tracking_code"],
                "properties": {
                    "tracking_code": {
                        "type": "string",
                        "description": "Código de rastreio gerado pelos Correios (ex: BR123456789BR)",
                    }
                },
            }
        },
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            403: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        },
    )
    @transaction.atomic
    def patch(self, request, order_id):
        order = CustomerOrder.objects.filter(id=order_id).first()
        if order is None:
            return Response(
                {"message": "Pedido não encontrado."},
                status=status.HTTP_404_NOT_FOUND,
            )

        tracking_code = request.data.get("tracking_code", "").strip()
        if not tracking_code:
            return Response(
                {"message": "O campo tracking_code é obrigatório."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        unshippable_statuses = [OrderStatus.DELIVERED, OrderStatus.CANCELED]
        if order.status in unshippable_statuses:
            return Response(
                {
                    "message": f"Não é possível registrar rastreio em pedido com status '{order.status}'."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if order.status == OrderStatus.PREPARING:
            update_status(
                order=order,
                new_status=OrderStatus.SHIPPED,
                tracking_code=tracking_code,
                changed_by=request.user,
            )

        elif order.status == OrderStatus.SHIPPED:
            update_tracking_code(
                order=order,
                tracking_code=tracking_code,
                changed_by=request.user,
            )

        return Response(
            {
                "message": "Código de rastreio registrado com sucesso.",
                "order_id": str(order.id),
                "tracking_code": order.tracking_code,
                "status": order.status,
            },
            status=status.HTTP_200_OK,
        )

    def get_order_if_user_has_permission(self, user, order_id):
        if getattr(user, "is_admin", False):
            return CustomerOrder.objects.filter(id=order_id).first()
        return CustomerOrder.objects.filter(id=order_id, user=user).first()


class OrderDispatchView(APIView):
    permission_classes = [IsStaffOrSuperUser]

    @extend_schema(
        tags=["Correios"],
        summary="Despachar Pedido via Pré-Postagem Correios (Admin)",
        description=(
            "Cria automaticamente um objeto postal nos Correios via API de Pré-Postagem, "
            "obtém o código de rastreio gerado e atualiza o status do pedido para `SHIPPED`.\n\n"
            "Restrito a administradores. Use este endpoint em vez do PATCH de rastreio manual "
            "quando quiser que o sistema gere o código automaticamente."
        ),
        request=None,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            403: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request, order_id):
        order = (
            CustomerOrder.objects.select_related("user", "user__profile", "payment")
            .filter(id=order_id)
            .first()
        )
        if order is None:
            return Response(
                {"message": "Pedido não encontrado."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if order.status != OrderStatus.PREPARING:
            return Response(
                {
                    "message": f"Pedido com status '{order.status}' não pode ser despachado."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not hasattr(order, "payment") or order.payment.status != PaymentStatus.PAID:
            return Response(
                {
                    "message": (
                        "Pedido sem pagamento confirmado não pode ser despachado."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            tracking_code = dispatch_order_and_get_tracking_code(order)
        except (CorreiosAuthenticationError, CorreiosPrePostagemError) as e:
            logger.warning(
                "Falha conhecida nos Correios ao criar pré-postagem para pedido %s: %s",
                order_id,
                e,
            )
            return Response(
                {
                    "message": (
                        "Falha ao registrar envio nos Correios. "
                        "Tente novamente ou registre o código manualmente."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception(
                "Falha ao criar pré-postagem nos Correios para o pedido %s",
                order_id,
            )
            return Response(
                {
                    "message": (
                        "Falha ao registrar envio nos Correios. "
                        "Tente novamente ou registre o código manualmente."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        update_status(
            order=order,
            new_status=OrderStatus.SHIPPED,
            changed_by=request.user,
            tracking_code=tracking_code,
            comment=f"Pedido despachado automaticamente via Correios. Código de rastreio: {tracking_code}",
        )

        return Response(
            {
                "message": "Pedido despachado com sucesso via Correios.",
                "order_id": str(order.id),
                "tracking_code": tracking_code,
                "status": order.status,
            },
            status=status.HTTP_200_OK,
        )


class CartAPIView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Recuperar Carrinho",
        description="Retorna o carrinho de compras atual do utilizador (autenticado ou anónimo).",
        responses={200: CartRepresentationSerializer},
    )
    def get(self, request):
        cart_data = get_cart_data(request)
        serializer = CartRepresentationSerializer(cart_data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["Cart"],
        summary="Limpar Carrinho",
        description="Remove todos os itens do carrinho de compras.",
        responses={200: CartRepresentationSerializer},
    )
    def delete(self, request):
        clear_cart(request)
        cart_data = get_cart_data(request)
        serializer = CartRepresentationSerializer(cart_data)
        return Response(serializer.data, status=status.HTTP_200_OK)


class CartItemAddAPIView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Adicionar Item ao Carrinho",
        description="Adiciona uma variação de produto ao carrinho. Se já existir, incrementa a quantidade.",
        request=CartItemAddSerializer,
        responses={
            200: CartRepresentationSerializer,
            400: inline_serializer(
                name="CartErrorResponse", fields={"message": serializers.CharField()}
            ),
            404: inline_serializer(
                name="CartNotFoundResponse", fields={"detail": serializers.CharField()}
            ),
        },
    )
    def post(self, request):
        serializer = CartItemAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        variation_id = serializer.validated_data["variation_id"]
        quantity = serializer.validated_data["quantity"]

        try:
            add_item_to_cart(request, variation_id, quantity)
        except ValueError as e:
            return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        cart_data = get_cart_data(request)
        return Response(
            CartRepresentationSerializer(cart_data).data, status=status.HTTP_200_OK
        )


class CartItemDetailAPIView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Cart"],
        summary="Atualizar Quantidade do Item",
        description="Atualiza a quantidade absoluta de um item no carrinho.",
        request=CartItemUpdateSerializer,
        responses={
            200: CartRepresentationSerializer,
            400: inline_serializer(
                name="CartUpdateErrorResponse",
                fields={"message": serializers.CharField()},
            ),
            404: inline_serializer(
                name="CartUpdateNotFoundResponse",
                fields={"detail": serializers.CharField()},
            ),
        },
    )
    def patch(self, request, variation_id):
        serializer = CartItemUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        quantity = serializer.validated_data["quantity"]

        try:
            update_item_quantity(request, variation_id, quantity)
        except ValueError as e:
            return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except (KeyError, Http404):
            return Response(
                {"detail": "Item não encontrado no carrinho."},
                status=status.HTTP_404_NOT_FOUND,
            )

        cart_data = get_cart_data(request)
        return Response(
            CartRepresentationSerializer(cart_data).data, status=status.HTTP_200_OK
        )

    @extend_schema(
        tags=["Cart"],
        summary="Remover Item do Carrinho",
        description="Remove um item (variação de produto) do carrinho.",
        responses={
            200: CartRepresentationSerializer,
            404: inline_serializer(
                name="CartRemoveNotFoundResponse",
                fields={"detail": serializers.CharField()},
            ),
        },
    )
    def delete(self, request, variation_id):
        try:
            remove_item_from_cart(request, variation_id)
        except (KeyError, Http404):
            return Response(
                {"detail": "Item não encontrado no carrinho."},
                status=status.HTTP_404_NOT_FOUND,
            )

        cart_data = get_cart_data(request)
        return Response(
            CartRepresentationSerializer(cart_data).data, status=status.HTTP_200_OK
        )
