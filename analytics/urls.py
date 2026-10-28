from django.urls import path

from .views import (
    LinkAnonymousEventsView,
    SiteEventCreateView,
    SiteEventOverviewView,
    UserSearchView,
    UserSiteEventTimelineView,
)

app_name = "analytics"

urlpatterns = [
    path("events/", SiteEventCreateView.as_view(), name="event-create"),
    path("link/", LinkAnonymousEventsView.as_view(), name="link-anonymous"),
    path("overview/", SiteEventOverviewView.as_view(), name="overview"),
    path("users/search/", UserSearchView.as_view(), name="user-search"),
    path(
        "users/<int:user_id>/events/",
        UserSiteEventTimelineView.as_view(),
        name="user-timeline",
    ),
]
