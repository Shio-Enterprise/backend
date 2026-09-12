from uuid import UUID

from django.db.models import Case, Exists, Max, Min, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from rest_framework.pagination import PageNumberPagination

from orders.models import OrderItem, OrderStatus

from .models import Product, ProductVariation


def catalog_filter_options():
    """Opções do catálogo completo, sem carregar produtos na interface."""
    products = Product.objects.filter(is_active=True)
    variations = ProductVariation.objects.filter(product__is_active=True)
    return {
        **products.aggregate(min_price=Min("base_price"), max_price=Max("base_price")),
        "sizes": list(
            variations.exclude(size="")
            .order_by("size")
            .values_list("size", flat=True)
            .distinct()
        ),
        "colors": list(
            variations.exclude(color="")
            .order_by("color")
            .values_list("color", flat=True)
            .distinct()
        ),
    }


class CatalogPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 50


class RecommendationPagination(CatalogPagination):
    page_size = 4


def with_sales_count(queryset):
    """Soma vendas válidas sem multiplicá-las pelos joins das variações."""
    sales = (
        OrderItem.objects.filter(
            variation__product_id=OuterRef("pk"),
            order__status__in=(
                OrderStatus.PAID,
                OrderStatus.PREPARING,
                OrderStatus.SHIPPED,
                OrderStatus.DELIVERED,
            ),
        )
        .order_by()
        .values("variation__product_id")
        .annotate(total=Sum("quantity"))
        .values("total")
    )
    return queryset.annotate(sales_count=Coalesce(Subquery(sales), 0))


def recommend_products(product):
    """Prioriza contexto e vendas, completa a lista com o catálogo disponível."""
    available = ProductVariation.objects.filter(
        product_id=OuterRef("pk"), stock_quantity__gt=0
    )
    queryset = (
        Product.objects.filter(is_active=True)
        .exclude(pk=product.pk)
        .filter(Exists(available))
        .select_related("category", "drop")
        .prefetch_related("variations", "images")
    )

    for field in ("category", "drop"):
        related_id = getattr(product, f"{field}_id")
        priority = (
            Case(When(**{f"{field}_id": related_id}, then=Value(1)), default=Value(0))
            if related_id is not None
            else Value(0)
        )
        queryset = queryset.alias(**{f"{field}_priority": priority})
    return with_sales_count(queryset).order_by(
        "-category_priority", "-drop_priority", "-sales_count", "-created_at", "id"
    )


def filter_catalog(queryset, params):
    """Aplica parâmetros já validados; tamanho e cor devem existir na mesma variação."""
    # Mantém compatibilidade com UUIDs existentes, mas aceita slug como contrato público da categoria.
    category = params.get("category")
    if category:
        try:
            category_id = UUID(category)
        except ValueError:
            queryset = queryset.filter(category__slug=category)
        else:
            queryset = queryset.filter(category_id=category_id)

    if params.get("drop"):
        queryset = queryset.filter(drop_id=params["drop"])
    if params.get("search"):
        search = params["search"]
        queryset = queryset.filter(
            Q(name__icontains=search) | Q(description__icontains=search)
        )

    for parameter, lookup in (
        ("min_price", "base_price__gte"),
        ("max_price", "base_price__lte"),
    ):
        if parameter in params:
            queryset = queryset.filter(**{lookup: params[parameter]})

    variations = {}
    if params.get("size"):
        variations["variations__size"] = params["size"]
    if params.get("color"):
        variations["variations__color__in"] = params["color"]
    if variations:
        queryset = queryset.filter(**variations).distinct()

    ordering = params["ordering"]
    # Calcula as unidades vendidas por produto para permitir a ordenação por mais vendidos.
    if ordering == "-sales_count":
        queryset = with_sales_count(queryset)

    if ordering == "-created_at":
        return queryset.order_by("-created_at", "id")
    return queryset.order_by(ordering, "-created_at", "id")
