import logging

from django.db import transaction
from django.db.models import Max, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.permissions import IsStaffOrSuperUser

from .availability import (
    is_drop_visible,
    is_product_visible,
    visible_drops_queryset,
    visible_products_queryset,
)
from .models import (
    Category,
    DropCampaign,
    Product,
    ProductImage,
    ProductVariation,
    StockMovementKind,
)
from .serializers import (
    CategorySerializer,
    DropCampaignDetailSerializer,
    DropCampaignSerializer,
    ProductDetailSerializer,
    ProductImageSerializer,
    ProductListSerializer,
    ProductVariationSerializer,
    ProductWriteSerializer,
    StockMovementSerializer,
)

logger = logging.getLogger(__name__)


def inventory_summary(request):
    products = Product.objects.all()
    data = []
    for product in products:
        total_stock = (
            product.variations.aggregate(total=Sum("stock_quantity"))["total"] or 0
        )
        data.append(
            {
                "name": product.name,
                "total_stock": total_stock,
            }
        )
    return JsonResponse({"inventory": data})


# ─── Categories ───────────────────────────────────────────────────────────────


class CategoryListCreateView(APIView):
    """Listar categorias (público) e criar categoria (admin)."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsStaffOrSuperUser()]
        return [AllowAny()]

    @extend_schema(
        tags=["Categories"],
        summary="Listar categorias",
        description="Lista paginada de categorias do catálogo. Endpoint público.",
        responses={200: CategorySerializer(many=True)},
    )
    def get(self, request):
        queryset = Category.objects.all().order_by("name")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = CategorySerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        tags=["Categories"],
        summary="Criar categoria",
        description=(
            "Cria uma nova categoria. Requer perfil ADMIN.\n\n"
            "O `slug` é gerado automaticamente a partir do `name` se não for enviado."
        ),
        request=CategorySerializer,
        responses={
            201: CategorySerializer,
            400: OpenApiResponse(description="Dados inválidos (ex: nome duplicado)."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
        },
    )
    def post(self, request):
        serializer = CategorySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        category = serializer.save()
        logger.info(f"Categoria criada: {category.name} ({category.id})")
        return Response(
            CategorySerializer(category).data,
            status=status.HTTP_201_CREATED,
        )


class CategoryDetailView(APIView):
    """Detalhar (público); atualizar e remover (admin) uma categoria."""

    def get_permissions(self):
        if self.request.method == "GET":
            return [AllowAny()]
        return [IsStaffOrSuperUser()]

    def _get_object(self, pk):
        return get_object_or_404(Category, pk=pk)

    @extend_schema(
        tags=["Categories"],
        summary="Detalhe da categoria",
        responses={
            200: CategorySerializer,
            404: OpenApiResponse(description="Categoria não encontrada."),
        },
    )
    def get(self, request, pk):
        return Response(CategorySerializer(self._get_object(pk)).data)

    @extend_schema(
        tags=["Categories"],
        summary="Atualizar categoria (PUT)",
        description=(
            "Substitui os dados da categoria. Requer perfil ADMIN.\n\n"
            "Se `name` mudar e `slug` não for enviado, o slug é regenerado a partir do novo `name`."
        ),
        request=CategorySerializer,
        responses={
            200: CategorySerializer,
            400: OpenApiResponse(description="Dados inválidos."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Categoria não encontrada."),
        },
    )
    def put(self, request, pk):
        category = self._get_object(pk)
        serializer = CategorySerializer(category, data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        tags=["Categories"],
        summary="Remover categoria",
        description="Remove a categoria. Produtos relacionados ficam com `category=null`.",
        responses={
            204: OpenApiResponse(description="Categoria removida."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Categoria não encontrada."),
        },
    )
    def delete(self, request, pk):
        category = self._get_object(pk)
        logger.info(f"Categoria removida: {category.name} ({category.id})")
        category.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── DropCampaigns ────────────────────────────────────────────────────────────


class DropCampaignListCreateView(APIView):
    """Listar drops (público, visibilidade automática) e criar (admin)."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsStaffOrSuperUser()]
        return [AllowAny()]

    @extend_schema(
        tags=["Drops"],
        summary="Listar drops",
        description=(
            "Lista paginada de campanhas de drop.\n\n"
            "**Público** (não autenticado ou não-admin): aplica automaticamente a "
            "política de visibilidade da issue #6 — retorna apenas drops com "
            "`is_public=True`, `is_active=True` e dentro da janela "
            "`[launch_date, end_date]`. Não é necessário (nem possível) desativar "
            "esse filtro.\n\n"
            "**Admin**: por padrão vê todos os drops (incluindo ocultos/privados/"
            "encerrados). Use `?visible=true` para pré-visualizar exatamente o que "
            "o público vê."
        ),
        parameters=[
            OpenApiParameter(
                name="visible",
                type=OpenApiTypes.BOOL,
                required=False,
                description=(
                    "Somente para admin. Quando `true`, aplica o mesmo filtro de "
                    "visibilidade pública usado para utilizadores não-admin."
                ),
            ),
        ],
        responses={200: DropCampaignSerializer(many=True)},
    )
    def get(self, request):
        is_admin = request.user.is_authenticated and getattr(
            request.user, "is_admin", False
        )
        queryset = DropCampaign.objects.all().order_by("-created_at")

        if is_admin:
            if request.query_params.get("visible", "").lower() == "true":
                queryset = visible_drops_queryset(queryset)
        else:
            # Política da issue #6 aplicada automaticamente — sempre, sem
            # depender de um parâmetro de query.
            queryset = visible_drops_queryset(queryset)

        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = DropCampaignSerializer(
            page, many=True, context={"request": request}
        )
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        tags=["Drops"],
        summary="Criar drop",
        description=(
            "Cria uma nova campanha de drop. Requer perfil ADMIN.\n\n"
            "Aceita `multipart/form-data` com o campo `banner` (imagem)."
        ),
        request=DropCampaignSerializer,
        responses={
            201: DropCampaignSerializer,
            400: OpenApiResponse(
                description="Dados inválidos (ex: datas inconsistentes)."
            ),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
        },
    )
    def post(self, request):
        serializer = DropCampaignSerializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        drop = serializer.save()
        logger.info(f"Drop criado: {drop.name} ({drop.id})")
        return Response(
            DropCampaignSerializer(drop, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class DropCampaignDetailView(APIView):
    """Detalhar drop com produtos nested (público); atualizar e remover (admin)."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method == "GET":
            return [AllowAny()]
        return [IsStaffOrSuperUser()]

    def _get_object(self, pk):
        return get_object_or_404(DropCampaign, pk=pk)

    @extend_schema(
        tags=["Drops"],
        summary="Detalhe do drop com produtos",
        description=(
            "Retorna o drop com os produtos aninhados.\n\n"
            "Retorna 404 para utilizadores não-admin quando o drop não estiver "
            "publicamente visível (política da issue #6: `is_public`, `is_active` "
            "e janela `[launch_date, end_date]`). Admin sempre vê o drop, "
            "independente da visibilidade."
        ),
        responses={
            200: DropCampaignDetailSerializer,
            404: OpenApiResponse(
                description="Drop não encontrado ou não visível ao público."
            ),
        },
    )
    def get(self, request, pk):
        drop = self._get_object(pk)
        is_admin = request.user.is_authenticated and getattr(
            request.user, "is_admin", False
        )
        if not is_admin and not is_drop_visible(drop):
            return Response(
                {"error": "Drop não encontrado."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(
            DropCampaignDetailSerializer(drop, context={"request": request}).data
        )

    @extend_schema(
        tags=["Drops"],
        summary="Atualizar drop (PUT)",
        description=(
            "Substitui os dados da campanha. Requer perfil ADMIN.\n\n"
            "Trocar o `banner` remove o arquivo antigo do storage."
        ),
        request=DropCampaignSerializer,
        responses={
            200: DropCampaignSerializer,
            400: OpenApiResponse(description="Dados inválidos."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Drop não encontrado."),
        },
    )
    def put(self, request, pk):
        drop = self._get_object(pk)
        serializer = DropCampaignSerializer(
            drop, data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        tags=["Drops"],
        summary="Remover drop",
        description=(
            "Remove a campanha e o seu banner do storage.\n\n"
            "Produtos relacionados ficam com `drop=null` (`on_delete=SET_NULL`)."
        ),
        responses={
            204: OpenApiResponse(description="Drop removido."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Drop não encontrado."),
        },
    )
    def delete(self, request, pk):
        drop = self._get_object(pk)
        logger.info(f"Drop removido: {drop.name} ({drop.id})")
        drop.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DropProductManageView(APIView):
    """Associar e desassociar um produto de um drop. Admin only."""

    permission_classes = [IsStaffOrSuperUser]
    serializer_class = DropCampaignDetailSerializer

    def _get_drop(self, drop_id):
        return get_object_or_404(DropCampaign, pk=drop_id)

    def _get_product(self, product_id):
        return get_object_or_404(Product, pk=product_id)

    @extend_schema(
        tags=["Drops"],
        summary="Vincular produto ao drop",
        description=(
            "Associa um produto a este drop. Requer perfil ADMIN.\n\n"
            "Se o produto já estava em outro drop, é movido para este."
        ),
        request=None,
        responses={
            200: DropCampaignDetailSerializer,
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Drop ou produto não encontrado."),
        },
    )
    def post(self, request, drop_id, product_id):
        drop = self._get_drop(drop_id)
        product = self._get_product(product_id)
        product.drop = drop
        product.save(update_fields=["drop", "updated_at"])
        logger.info(f"Produto {product.id} vinculado ao drop {drop.id}")
        return Response(
            DropCampaignDetailSerializer(drop, context={"request": request}).data
        )

    @extend_schema(
        tags=["Drops"],
        summary="Desvincular produto do drop",
        description=(
            "Remove a associação entre o produto e este drop (seta `drop=null`). "
            "Não apaga o produto."
        ),
        request=None,
        responses={
            204: OpenApiResponse(description="Produto desvinculado."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(
                description="Drop ou produto não encontrado, ou produto não pertence a este drop."
            ),
        },
    )
    def delete(self, request, drop_id, product_id):
        drop = self._get_drop(drop_id)
        product = self._get_product(product_id)
        if product.drop_id != drop.id:
            return Response(
                {"error": "Produto não pertence a este drop."},
                status=status.HTTP_404_NOT_FOUND,
            )
        product.drop = None
        product.save(update_fields=["drop", "updated_at"])
        logger.info(f"Produto {product.id} desvinculado do drop {drop.id}")
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Products ─────────────────────────────────────────────────────────────────


class ProductListCreateView(APIView):
    """Listar produtos (público, com filtros) e criar (admin)."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsStaffOrSuperUser()]
        return [AllowAny()]

    def get_serializer_class(self):
        return (
            ProductWriteSerializer
            if self.request.method == "POST"
            else ProductListSerializer
        )

    @extend_schema(
        tags=["Products"],
        summary="Listar produtos",
        description=(
            "Lista paginada do catálogo.\n\n"
            "**Público** (não autenticado ou não-admin): aplica automaticamente a "
            "política de visibilidade da issue #6 — oculta produtos inativos e "
            "produtos vinculados a um drop não visível (privado, inativo, futuro "
            "ou encerrado). Produtos sem drop seguem apenas `Product.is_active`.\n\n"
            "**Admin**: por padrão vê todos os produtos, incluindo inativos e "
            "vinculados a drops ocultos. `?is_active=true|false` filtra por "
            "atividade; `?visible=true` pré-visualiza exatamente o que o público vê.\n\n"
            "Outros filtros via query: `category={uuid}`, `drop={uuid}`, "
            "`search={text}` (busca em name/description).\n\n"
            "Ordenação padrão: `-created_at`."
        ),
        parameters=[
            OpenApiParameter(
                name="is_active",
                type=OpenApiTypes.BOOL,
                required=False,
                description="Somente admin. Filtra por Product.is_active.",
            ),
            OpenApiParameter(
                name="visible",
                type=OpenApiTypes.BOOL,
                required=False,
                description=(
                    "Somente admin. Quando `true`, aplica o mesmo filtro de "
                    "visibilidade pública usado para utilizadores não-admin."
                ),
            ),
        ],
        responses={200: ProductListSerializer(many=True)},
    )
    def get(self, request):
        qs = Product.objects.select_related("category", "drop").prefetch_related(
            "variations", "images"
        )

        is_admin = request.user.is_authenticated and getattr(request.user, "is_admin", False)
        is_active_param = request.query_params.get("is_active")
        if is_admin:
            if is_active_param is not None:
                qs = qs.filter(is_active=is_active_param.lower() == "true")
            if request.query_params.get("visible", "").lower() == "true":
                qs = visible_products_queryset(qs)
        else:
            qs = visible_products_queryset(qs)

        category = request.query_params.get("category")
        if category:
            qs = qs.filter(category_id=category)

        drop = request.query_params.get("drop")
        if drop:
            qs = qs.filter(drop_id=drop)

        search = request.query_params.get("search")
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))

        qs = qs.order_by("-created_at")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = ProductListSerializer(
            page, many=True, context={"request": request}
        )
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        tags=["Products"],
        summary="Criar produto",
        description=(
            "Cria um novo produto. Requer perfil ADMIN.\n\n"
            "Pode incluir `variations` aninhadas (lista de `{size, sku, stock_quantity}`). "
            "Imagens devem ser enviadas em request separado via `PUT /products/{id}/images/`."
        ),
        request=ProductWriteSerializer,
        responses={
            201: ProductDetailSerializer,
            400: OpenApiResponse(
                description="Dados inválidos (ex: SKU duplicado, base_price negativo)."
            ),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
        },
    )
    def post(self, request):
        serializer = ProductWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        product = serializer.save()
        logger.info(f"Produto criado: {product.name} ({product.id})")
        return Response(
            ProductDetailSerializer(product, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ProductDetailView(APIView):
    """Detalhe (público — 404 se inactive p/ não-admin); update e delete (admin)."""

    def get_permissions(self):
        if self.request.method == "GET":
            return [AllowAny()]
        return [IsStaffOrSuperUser()]

    def get_serializer_class(self):
        return (
            ProductWriteSerializer
            if self.request.method == "PUT"
            else ProductDetailSerializer
        )

    def _get_object(self, pk, request, allow_inactive_for_admin=True):
        product = get_object_or_404(
            Product.objects.select_related("category", "drop").prefetch_related(
                "variations", "images"
            ),
            pk=pk,
        )
        is_admin = request.user.is_authenticated and getattr(request.user, "is_admin", False)
        if is_admin and allow_inactive_for_admin:
            return product
        if not is_product_visible(product):
            raise Product.DoesNotExist
        return product

    @extend_schema(
        tags=["Products"],
        summary="Detalhe do produto",
        description=(
            "Retorna o produto com variations, images, category e drop expandidos.\n\n"
            "Retorna 404 para não-admin quando `is_active=False` ou quando o "
            "produto está vinculado a um drop não visível (política da issue #6). "
            "Admin sempre vê o produto."
        ),
        responses={
            200: ProductDetailSerializer,
            404: OpenApiResponse(description="Produto não encontrado."),
        },
    )
    def get(self, request, pk):
        try:
            product = self._get_object(pk, request)
        except Product.DoesNotExist:
            return Response(
                {"error": "Produto não encontrado."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            ProductDetailSerializer(product, context={"request": request}).data
        )

    @extend_schema(
        tags=["Products"],
        summary="Atualizar produto (PUT)",
        description=(
            "Substitui os dados do produto. Requer perfil ADMIN.\n\n"
            "Variações e imagens devem ser geridas via sub-endpoints próprios."
        ),
        request=ProductWriteSerializer,
        responses={
            200: ProductDetailSerializer,
            400: OpenApiResponse(description="Dados inválidos."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Produto não encontrado."),
        },
    )
    def put(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        serializer = ProductWriteSerializer(product, data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        product = serializer.save()
        return Response(
            ProductDetailSerializer(product, context={"request": request}).data
        )

    @extend_schema(
        tags=["Products"],
        summary="Remover produto",
        description="Remove o produto. Variações e imagens vão junto (CASCADE).",
        responses={
            204: OpenApiResponse(description="Produto removido."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Produto não encontrado."),
        },
    )
    def delete(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        logger.info(f"Produto removido: {product.name} ({product.id})")
        product.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Variations ───────────────────────────────────────────────────────────────


class ProductVariationCreateView(APIView):
    """Cria variação para um produto. Admin only."""

    permission_classes = [IsStaffOrSuperUser]
    serializer_class = ProductVariationSerializer

    @extend_schema(
        tags=["Products"],
        summary="Adicionar variação",
        description="Cria uma variação para o produto. Requer perfil ADMIN.",
        request=ProductVariationSerializer,
        responses={
            201: ProductVariationSerializer,
            400: OpenApiResponse(description="Dados inválidos (ex: SKU duplicado)."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Produto não encontrado."),
        },
    )
    def post(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        serializer = ProductVariationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        variation = serializer.save(product=product)
        logger.info(f"Variação criada: {variation.sku} no produto {product.id}")
        return Response(
            ProductVariationSerializer(variation).data,
            status=status.HTTP_201_CREATED,
        )


class ProductVariationDetailView(APIView):
    """Update e delete de variação. Admin only."""

    permission_classes = [IsStaffOrSuperUser]
    serializer_class = ProductVariationSerializer

    @extend_schema(
        tags=["Products"],
        summary="Atualizar variação (PUT)",
        request=ProductVariationSerializer,
        responses={
            200: ProductVariationSerializer,
            400: OpenApiResponse(description="Dados inválidos."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Variação não encontrada."),
        },
    )
    def put(self, request, pk):
        variation = get_object_or_404(ProductVariation, pk=pk)
        serializer = ProductVariationSerializer(variation, data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        tags=["Products"],
        summary="Remover variação",
        description=(
            "Remove a variação. ⚠️ Itens em carrinhos ativos referenciando esta variação "
            "serão removidos em CASCADE."
        ),
        responses={
            204: OpenApiResponse(description="Variação removida."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Variação não encontrada."),
        },
    )
    def delete(self, request, pk):
        variation = get_object_or_404(ProductVariation, pk=pk)
        logger.info(f"Variação removida: {variation.sku} ({variation.id})")
        variation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Images ───────────────────────────────────────────────────────────────────


class ProductImageCreateView(APIView):
    """Cria imagem com display_order automático = max+1."""

    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsStaffOrSuperUser]
    serializer_class = ProductImageSerializer

    @extend_schema(
        tags=["Products"],
        summary="Adicionar imagem ao produto",
        operation_id="catalog_product_image_create",
        description=(
            "Sobe nova imagem. O `display_order` é calculado pelo servidor "
            "(`max + 1` por produto, começando em 1).\n\n"
            "Multipart. Formatos: jpg, jpeg, png, webp. Tamanho máx: 5MB."
        ),
        request={"multipart/form-data": ProductImageSerializer},
        responses={
            201: ProductImageSerializer,
            400: OpenApiResponse(description="Imagem inválida (formato ou tamanho)."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Produto não encontrado."),
        },
    )
    def post(self, request, product_id):
        product = get_object_or_404(Product, pk=product_id)
        serializer = ProductImageSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        max_order = (
            product.images.aggregate(Max("display_order"))["display_order__max"] or 0
        )
        image = serializer.save(product=product, display_order=max_order + 1)
        logger.info(
            f"Imagem criada para produto {product.id}: ordem {image.display_order}"
        )
        return Response(
            ProductImageSerializer(image, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ProductImageUpdateView(APIView):
    """Substitui o binário de uma imagem existente. display_order é preservado."""

    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsStaffOrSuperUser]
    serializer_class = ProductImageSerializer

    @extend_schema(
        tags=["Products"],
        summary="Substituir imagem do produto",
        operation_id="catalog_product_image_update",
        description=(
            "Substitui o arquivo binário da imagem identificada por `image_id`. "
            "O `display_order` original é mantido. O arquivo antigo é apagado do storage."
        ),
        request={"multipart/form-data": ProductImageSerializer},
        responses={
            200: ProductImageSerializer,
            400: OpenApiResponse(description="Imagem inválida (formato ou tamanho)."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Produto ou imagem não encontrado."),
        },
    )
    def put(self, request, product_id, image_id):
        product = get_object_or_404(Product, pk=product_id)
        image = get_object_or_404(ProductImage, pk=image_id, product=product)
        serializer = ProductImageSerializer(image, data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Dados inválidos.", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        new_image = serializer.validated_data.get("image")
        if new_image and image.image and new_image != image.image:
            image.image.delete(save=False)
        updated = serializer.save()
        return Response(
            ProductImageSerializer(updated, context={"request": request}).data
        )


class ProductImageDeleteView(APIView):
    """Remove uma imagem de produto. Admin only."""

    permission_classes = [IsStaffOrSuperUser]
    serializer_class = ProductImageSerializer

    @extend_schema(
        tags=["Products"],
        summary="Remover imagem do produto",
        description="Remove a imagem e apaga o arquivo do storage.",
        responses={
            204: OpenApiResponse(description="Imagem removida."),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Imagem não encontrada."),
        },
    )
    def delete(self, request, pk):
        image = get_object_or_404(ProductImage, pk=pk)
        logger.info(f"Imagem removida: {image.id} (produto {image.product_id})")
        image.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ─── Stock ────────────────────────────────────────────────────────────────────


class StockMovementListCreateView(APIView):
    """Histórico e registro de movimentações de estoque de uma variação."""

    permission_classes = [IsStaffOrSuperUser]
    serializer_class = StockMovementSerializer

    def _get_variation(self, variation_id, lock=False):
        qs = ProductVariation.objects.all()
        if lock:
            qs = qs.select_for_update()
        return get_object_or_404(qs, pk=variation_id)

    @extend_schema(
        tags=["Stock"],
        summary="Listar movimentações da variação",
        description="Histórico paginado, ordenado por `-created_at`.",
        responses={
            200: StockMovementSerializer(many=True),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Variação não encontrada."),
        },
    )
    def get(self, request, variation_id):
        variation = self._get_variation(variation_id)
        qs = variation.stock_movements.all()
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = StockMovementSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        tags=["Stock"],
        summary="Registrar movimentação",
        description=(
            "Cria uma movimentação e ajusta `ProductVariation.stock_quantity` "
            "atomicamente.\n\n"
            "- `kind=ENTRADA`: soma `quantity` ao estoque.\n"
            "- `kind=SAIDA`: subtrai. Se o resultado ficaria negativo, retorna 400."
        ),
        request=StockMovementSerializer,
        responses={
            201: StockMovementSerializer,
            400: OpenApiResponse(
                description="Dados inválidos ou saída supera o estoque."
            ),
            401: OpenApiResponse(description="Não autenticado."),
            403: OpenApiResponse(description="Não autorizado — requer perfil ADMIN."),
            404: OpenApiResponse(description="Variação não encontrada."),
        },
    )
    def post(self, request, variation_id):
        with transaction.atomic():
            variation = self._get_variation(variation_id, lock=True)
            serializer = StockMovementSerializer(
                data=request.data, context={"variation": variation}
            )
            if not serializer.is_valid():
                return Response(
                    {"error": "Dados inválidos.", "details": serializer.errors},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            movement = serializer.save(variation=variation, created_by=request.user)
            if movement.kind == StockMovementKind.ENTRADA:
                variation.stock_quantity += movement.quantity
            else:
                variation.stock_quantity -= movement.quantity
            variation.save(update_fields=["stock_quantity", "updated_at"])

        logger.info(
            f"StockMovement {movement.kind} {movement.quantity} na variação {variation.id}"
        )
        return Response(
            StockMovementSerializer(movement).data,
            status=status.HTTP_201_CREATED,
        )
