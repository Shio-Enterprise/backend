from django.urls import path

from .views import (
    AdminReviewListView,
    AdminReviewRemoveView,
    AdminReviewReplyView,
    AdminReviewRestoreView,
    MyReviewsView,
    ProductReviewEligibilityView,
    ProductReviewListCreateView,
    ProductReviewSummaryView,
    ReviewDetailView,
)

app_name = "reviews"

urlpatterns = [
    # GET (público) - Lista publicadas / POST (cliente) - Cria avaliação
    path(
        "products/<uuid:product_id>/",
        ProductReviewListCreateView.as_view(),
        name="product-reviews",
    ),
    # GET (público) - Média, total e distribuições
    path(
        "products/<uuid:product_id>/summary/",
        ProductReviewSummaryView.as_view(),
        name="product-summary",
    ),
    # GET (cliente) - Pode avaliar? Já avaliou?
    path(
        "products/<uuid:product_id>/eligibility/",
        ProductReviewEligibilityView.as_view(),
        name="product-eligibility",
    ),
    # GET (cliente) - Minhas avaliações
    path("mine/", MyReviewsView.as_view(), name="mine"),
    # GET (admin) - Todas as avaliações, com filtros
    path("admin/", AdminReviewListView.as_view(), name="admin-list"),
    # POST (admin) - Remove com motivo
    path(
        "admin/<uuid:review_id>/remove/",
        AdminReviewRemoveView.as_view(),
        name="admin-remove",
    ),
    # POST (admin) - Desfaz remoção
    path(
        "admin/<uuid:review_id>/restore/",
        AdminReviewRestoreView.as_view(),
        name="admin-restore",
    ),
    # PUT, DELETE (admin) - Resposta da Shio
    path(
        "admin/<uuid:review_id>/reply/",
        AdminReviewReplyView.as_view(),
        name="admin-reply",
    ),
    # PATCH, DELETE (autor) - Edita/exclui a própria avaliação
    path("<uuid:review_id>/", ReviewDetailView.as_view(), name="review-detail"),
]
