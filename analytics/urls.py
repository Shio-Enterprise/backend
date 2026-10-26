from django.urls import path

from .views import (
    LinkAnonymousEventsView,
    SiteEventCreateView,
    ProductAccessView,
    ProductSearchView,
    SharedProductInterestView,
    SiteEventOverviewView,
    UserProductAccessView,
    UserSearchView,
    UserSiteEventTimelineView,
)

app_name = "analytics"

urlpatterns = [
    path("events/", SiteEventCreateView.as_view(), name="event-create"),
    path("link/", LinkAnonymousEventsView.as_view(), name="link-anonymous"),
    path("overview/", SiteEventOverviewView.as_view(), name="overview"),
    path("products/search/", ProductSearchView.as_view(), name="product-search"),
    path(
        "products/interest/",
        SharedProductInterestView.as_view(),
        name="product-interest",
    ),
    path(
        "products/<uuid:product_id>/accesses/",
        ProductAccessView.as_view(),
        name="product-access",
    ),
    path("users/search/", UserSearchView.as_view(), name="user-search"),
    path(
        "users/<int:user_id>/products/",
        UserProductAccessView.as_view(),
        name="user-products",
    ),
    path(
        "users/<int:user_id>/events/",
        UserSiteEventTimelineView.as_view(),
        name="user-timeline",
    ),
]
