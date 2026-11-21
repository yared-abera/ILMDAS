"""Core URL routes."""

from django.conf import settings
from django.urls import path

from .views import (
    CatalogueIndexView,
    ErrorProbeView,
    HealthView,
    IiifStatusView,
    ObjectDetailView,
    ObjectUploadView,
    SessionProbeView,
    ValidationProbeView,
)

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("storage/objects/", ObjectUploadView.as_view(), name="storage-upload"),
    path("storage/objects/<int:object_id>/", ObjectDetailView.as_view(), name="storage-detail"),
    path("iiif/status/", IiifStatusView.as_view(), name="iiif-status"),
    path("search/catalogue-index/", CatalogueIndexView.as_view(), name="catalogue-index"),
]

if getattr(settings, "ENVIRONMENT", "") != "production":
    urlpatterns += [
        path("contract/validation/", ValidationProbeView.as_view(), name="contract-validation"),
        path("contract/session/", SessionProbeView.as_view(), name="contract-session"),
        path("contract/errors/", ErrorProbeView.as_view(), name="contract-errors"),
    ]
