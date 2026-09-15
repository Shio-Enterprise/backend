from django.urls import path

from .views import (
    CatalogFilterOptionsView,
    CategoryDetailView,
    CategoryListCreateView,
    DropCampaignDetailView,
    DropCampaignListCreateView,
    DropProductManageView,
    ProductDetailView,
    ProductDuplicateView,
    ProductImageCreateView,
    ProductImageDeleteView,
    ProductImageUpdateView,
    ProductListCreateView,
    ProductRecommendationsView,
    ProductVariationCreateView,
    ProductVariationDetailView,
    StockMovementListCreateView,
    inventory_summary,
)

app_name = "products"

urlpatterns = [
    path(
        "products/<uuid:pk>/duplicate/",
        ProductDuplicateView.as_view(),
        name="product-duplicate",
    ),
    path(
        "products/filter-options/",
        CatalogFilterOptionsView.as_view(),
        name="catalog-filter-options",
    ),
    # GET (público) - Lista categorias / POST (admin) - Cria categoria
    path(
        "categories/",
        CategoryListCreateView.as_view(),
        name="category-list-create",
    ),
    # GET (público) - Detalhe / PUT, DELETE (admin) - Atualiza/remove categoria
    path(
        "categories/<uuid:pk>/",
        CategoryDetailView.as_view(),
        name="category-detail",
    ),
    # GET (público) - Lista drops / POST (admin) - Cria drop
    path(
        "drops/",
        DropCampaignListCreateView.as_view(),
        name="drop-list-create",
    ),
    # GET (público) - Detalhe com produtos / PUT, DELETE (admin)
    path(
        "drops/<uuid:pk>/",
        DropCampaignDetailView.as_view(),
        name="drop-detail",
    ),
    # POST (admin) - Vincula produto ao drop / DELETE (admin) - Desvincula
    path(
        "drops/<uuid:drop_id>/products/<uuid:product_id>/",
        DropProductManageView.as_view(),
        name="drop-product-manage",
    ),
    # GET (público) - Lista produtos / POST (admin) - Cria produto
    path(
        "products/",
        ProductListCreateView.as_view(),
        name="product-list-create",
    ),
    # GET (público) - Detalhe / PUT, DELETE (admin)
    path(
        "products/<uuid:pk>/",
        ProductDetailView.as_view(),
        name="product-detail",
    ),
    path(
        "products/<uuid:pk>/recommendations/",
        ProductRecommendationsView.as_view(),
        name="product-recommendations",
    ),
    # POST (admin) - Cria variação no produto
    path(
        "products/<uuid:pk>/variations/",
        ProductVariationCreateView.as_view(),
        name="product-variation-create",
    ),
    # PUT, DELETE (admin) - Atualiza/remove variação
    path(
        "variations/<uuid:pk>/",
        ProductVariationDetailView.as_view(),
        name="variation-detail",
    ),
    # POST (admin, multipart) - Cria imagem (display_order auto)
    path(
        "products/<uuid:product_id>/images/",
        ProductImageCreateView.as_view(),
        name="product-image-create",
    ),
    # PUT (admin, multipart) - Substitui binário da imagem
    path(
        "products/<uuid:product_id>/images/<uuid:image_id>/",
        ProductImageUpdateView.as_view(),
        name="product-image-update",
    ),
    # DELETE (admin) - Remove imagem
    path(
        "images/<uuid:pk>/",
        ProductImageDeleteView.as_view(),
        name="image-delete",
    ),
    # GET, POST (admin) - Histórico e registro de movimentação de estoque
    path(
        "variations/<uuid:variation_id>/stock-movements/",
        StockMovementListCreateView.as_view(),
        name="variation-stock-movements",
    ),
    # GET - Resumo de stock (legado)
    path("inventory/", inventory_summary, name="inventory_summary"),
]
