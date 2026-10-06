from django.urls import path

from .views import SiteEventCreateView, SiteEventOverviewView, UserSiteEventTimelineView

app_name = "analytics"

urlpatterns = [
    path("events/", SiteEventCreateView.as_view(), name="event-create"),
    path("overview/", SiteEventOverviewView.as_view(), name="overview"),
    path(
        "users/<int:user_id>/events/",
        UserSiteEventTimelineView.as_view(),
        name="user-timeline",
    ),
]
