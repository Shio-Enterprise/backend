from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from products.models import Product

from .models import ProductReview, ReviewStatus
from .serializers import (
    EligibilitySerializer,
    RatingSummarySerializer,
    ReviewListQuerySerializer,
    ReviewMineSerializer,
    ReviewPagination,
    ReviewPublicSerializer,
    ReviewWriteSerializer,
)
from .services import (
    AlreadyReviewed,
    NotEligible,
    ReviewError,
    create_review,
    delete_review,
    get_eligible_order_item,
    rating_summary,
    update_review,
)

ERROR_STATUS = {
    NotEligible: status.HTTP_403_FORBIDDEN,
    AlreadyReviewed: status.HTTP_409_CONFLICT,
}


def review_error_response(exc: ReviewError) -> Response:
    code = ERROR_STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST)
    return Response({"message": exc.message}, status=code)


def get_active_product(product_id):
    return get_object_or_404(Product, pk=product_id, is_active=True)


def paginate(view, request, queryset, serializer_class):
    paginator = ReviewPagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response(serializer_class(page, many=True).data)


class ProductReviewListCreateView(APIView):
    """GET (público) lista avaliações publicadas / POST (cliente) cria avaliação."""

    throttle_scope = "review_create"

    def get_permissions(self):
        if self.request.method == "GET":
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_throttles(self):
        if self.request.method == "POST":
            return [ScopedRateThrottle()]
        return []

    @extend_schema(
        parameters=[ReviewListQuerySerializer],
        responses={200: ReviewPublicSerializer(many=True)},
        summary="Listar avaliações publicadas do produto",
    )
    def get(self, request, product_id):
        product = get_active_product(product_id)
        query = ReviewListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = ProductReview.objects.filter(
            product=product, status=ReviewStatus.PUBLISHED
        ).select_related("user")
        if "rating" in query.validated_data:
            queryset = queryset.filter(rating=query.validated_data["rating"])
        return paginate(
            self,
            request,
            queryset.order_by("-created_at", "id"),
            ReviewPublicSerializer,
        )

    @extend_schema(
        request=ReviewWriteSerializer,
        responses={201: ReviewMineSerializer},
        summary="Avaliar produto de pedido entregue",
    )
    def post(self, request, product_id):
        product = get_active_product(product_id)
        serializer = ReviewWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            review = create_review(request.user, product, **serializer.validated_data)
        except ReviewError as exc:
            return review_error_response(exc)
        return Response(
            ReviewMineSerializer(review).data, status=status.HTTP_201_CREATED
        )


class ProductReviewSummaryView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: RatingSummarySerializer},
        summary="Resumo das avaliações do produto",
    )
    def get(self, request, product_id):
        product = get_active_product(product_id)
        return Response(RatingSummarySerializer(rating_summary(product)).data)


class ProductReviewEligibilityView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: EligibilitySerializer},
        summary="Cliente pode avaliar o produto?",
    )
    def get(self, request, product_id):
        product = get_active_product(product_id)
        review_id = (
            ProductReview.objects.filter(product=product, user=request.user)
            .values_list("id", flat=True)
            .first()
        )
        data = {
            "can_review": get_eligible_order_item(request.user, product) is not None,
            "review_id": review_id,
        }
        return Response(EligibilitySerializer(data).data)


class ReviewDetailView(APIView):
    """PATCH/DELETE da própria avaliação; de outra pessoa -> 404."""

    permission_classes = [IsAuthenticated]

    def get_own_review(self, request, review_id):
        return get_object_or_404(
            ProductReview.objects.select_related("product", "user"),
            pk=review_id,
            user=request.user,
        )

    @extend_schema(
        request=ReviewWriteSerializer,
        responses={200: ReviewMineSerializer},
        summary="Editar minha avaliação (republica se removida)",
    )
    def patch(self, request, review_id):
        review = self.get_own_review(request, review_id)
        serializer = ReviewWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        review = update_review(review, **serializer.validated_data)
        return Response(ReviewMineSerializer(review).data)

    @extend_schema(responses={204: None}, summary="Excluir minha avaliação")
    def delete(self, request, review_id):
        review = self.get_own_review(request, review_id)
        delete_review(review)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MyReviewsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter("page", int, required=False),
            OpenApiParameter("page_size", int, required=False),
        ],
        responses={200: ReviewMineSerializer(many=True)},
        summary="Minhas avaliações (todos os status)",
    )
    def get(self, request):
        queryset = (
            ProductReview.objects.filter(user=request.user)
            .select_related("product", "user")
            .order_by("-created_at", "id")
        )
        return paginate(self, request, queryset, ReviewMineSerializer)
