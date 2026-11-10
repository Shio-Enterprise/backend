from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.permissions import CanManageHomepage

from .homepage_serializers import (
    HomepageBannerSerializer,
    HomepageOrderSerializer,
    HomepageTaglineSerializer,
)
from .models import DEFAULT_HOMEPAGE_TAGLINE, HomepageBanner, HomepageSettings


def homepage_payload(request):
    settings = HomepageSettings.objects.filter(pk=1).first()
    return {
        "tagline": settings.tagline if settings else DEFAULT_HOMEPAGE_TAGLINE,
        "banners": HomepageBannerSerializer(
            HomepageBanner.objects.all(), many=True, context={"request": request}
        ).data,
    }


def no_store(response):
    response["Cache-Control"] = "no-store"
    return response


def delete_files_on_commit(files):
    for file in files:
        if file and file.name:
            storage, name = file.storage, file.name
            transaction.on_commit(lambda storage=storage, name=name: storage.delete(name))


class PublicHomepageView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return no_store(Response(homepage_payload(request)))


class AdminHomepageView(APIView):
    permission_classes = [CanManageHomepage]

    def get(self, request):
        return no_store(Response(homepage_payload(request)))


class HomepageTaglineView(APIView):
    permission_classes = [CanManageHomepage]

    def patch(self, request):
        serializer = HomepageTaglineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        settings, _ = HomepageSettings.objects.get_or_create(pk=1)
        settings.tagline = serializer.validated_data["tagline"]
        settings.save(update_fields=["tagline"])
        return no_store(Response({"tagline": settings.tagline}))


class HomepageBannerListView(APIView):
    permission_classes = [CanManageHomepage]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = HomepageBannerSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            order = HomepageBanner.objects.aggregate(max=Max("display_order"))["max"]
            banner = serializer.save(display_order=(order if order is not None else -1) + 1)
        return Response(HomepageBannerSerializer(banner, context={"request": request}).data, status=status.HTTP_201_CREATED)


class HomepageBannerDetailView(APIView):
    permission_classes = [CanManageHomepage]
    parser_classes = [MultiPartParser, FormParser]

    def patch(self, request, pk):
        banner = get_object_or_404(HomepageBanner, pk=pk)
        serializer = HomepageBannerSerializer(banner, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        old_desktop, old_mobile = banner.image_desktop, banner.image_mobile
        with transaction.atomic():
            banner = serializer.save()
            replaced = []
            if old_desktop and old_desktop.name != banner.image_desktop.name:
                replaced.append(old_desktop)
            if old_mobile and (not banner.image_mobile or old_mobile.name != banner.image_mobile.name):
                replaced.append(old_mobile)
            delete_files_on_commit(replaced)
        return Response(HomepageBannerSerializer(banner, context={"request": request}).data)

    def delete(self, request, pk):
        banner = get_object_or_404(HomepageBanner, pk=pk)
        files = (banner.image_desktop, banner.image_mobile)
        with transaction.atomic():
            banner.delete()
            delete_files_on_commit(files)
        return Response(status=status.HTTP_204_NO_CONTENT)


class HomepageBannerOrderView(APIView):
    permission_classes = [CanManageHomepage]

    def put(self, request):
        serializer = HomepageOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        with transaction.atomic():
            banners = list(HomepageBanner.objects.select_for_update())
            existing = {banner.id for banner in banners}
            if len(ids) != len(existing) or len(set(ids)) != len(ids) or set(ids) != existing:
                return Response({"ids": ["Envie todos os banners existentes uma vez cada."]}, status=status.HTTP_400_BAD_REQUEST)
            for order, banner_id in enumerate(ids):
                HomepageBanner.objects.filter(pk=banner_id).update(display_order=order)
        return no_store(Response(homepage_payload(request)))
