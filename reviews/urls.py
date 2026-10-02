from django.urls import path

from .views import (
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
    # PATCH, DELETE (autor) - Edita/exclui a própria avaliação
    path("<uuid:review_id>/", ReviewDetailView.as_view(), name="review-detail"),
]
