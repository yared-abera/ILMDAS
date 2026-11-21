"""Platform endpoints used by the scaffold. Feature workflows come later."""

from __future__ import annotations

import base64
import binascii
import uuid

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import NotAuthenticated, PermissionDenied, Throttled, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.serializers import CharField, Serializer
from rest_framework.views import APIView

from .exceptions import Conflict
from .iiif import IiifClient
from .models import ObjectReference
from .search import CatalogueIndex
from .storage import ObjectStorage, ObjectStorageConflictError, ObjectStorageError


def generate_object_key(prefix: str = "archive") -> str:
    """Generate a server-side object key with date prefix and UUID."""
    date_path = timezone.now().strftime("%Y/%m/%d")
    clean_prefix = prefix.strip("/") if prefix else "archive"
    return f"{clean_prefix}/{date_path}/{uuid.uuid4().hex}"


class ProbeSerializer(Serializer):
    name = CharField()


def database_health() -> dict:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return {"ok": False, "detail": "unreachable"}
    return {"ok": True, "detail": "reachable"}


def collect_health() -> dict:
    checks = {
        "database": database_health(),
        "elasticsearch": CatalogueIndex().health(),
        "objectStorage": ObjectStorage().health(),
    }
    status = "ok" if all(item["ok"] for item in checks.values()) else "failed"
    return {"status": status, "checks": checks}


class HealthView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        payload = collect_health()
        status_code = 200 if payload["status"] == "ok" else 503
        if request.user and request.user.is_authenticated:
            return Response(payload, status=status_code)
        return Response({"status": payload["status"]}, status=status_code)


class ValidationProbeView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def dispatch(self, request, *args, **kwargs):
        if getattr(settings, "ENVIRONMENT", "") == "production":
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        serializer = ProbeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"name": serializer.validated_data["name"]})


class SessionProbeView(APIView):
    permission_classes = [IsAuthenticated]

    def dispatch(self, request, *args, **kwargs):
        if getattr(settings, "ENVIRONMENT", "") == "production":
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return Response({"authenticated": True})


class ErrorProbeView(APIView):
    """Exercises the remaining error codes without a business workflow."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def dispatch(self, request, *args, **kwargs):
        if getattr(settings, "ENVIRONMENT", "") == "production":
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        kind = request.query_params.get("kind", "")
        if kind == "authorization":
            raise PermissionDenied()
        if kind == "not_found":
            raise Http404()
        if kind == "conflict":
            raise Conflict()
        if kind == "rate_limit":
            raise Throttled(wait=1)
        if kind == "unavailable":
            raise RuntimeError("internal-detail-not-for-clients")
        if kind == "authentication":
            raise NotAuthenticated()
        raise ValidationError({"kind": ["Choose a documented error kind."]})


class ObjectUploadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        encoded = request.data.get("bodyBase64", "")
        if not encoded:
            raise ValidationError({"bodyBase64": ["bodyBase64 is required."]})
        try:
            body = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError({"bodyBase64": ["bodyBase64 is not valid base64."]}) from exc

        object_key = request.data.get("objectKey", "")
        if isinstance(object_key, str):
            object_key = object_key.strip()
        if not object_key:
            prefix = request.data.get("prefix", "archive")
            object_key = generate_object_key(prefix)

        # SEC-01: Check for existing key in database and storage before writing
        storage = ObjectStorage()
        if (
            ObjectReference.objects.filter(object_key=object_key).exists()
            or storage.exists(object_key)
        ):
            raise Conflict(f"Object with key '{object_key}' already exists.")

        try:
            stored = storage.put_private(object_key, body)
        except ObjectStorageConflictError as exc:
            raise Conflict(str(exc)) from exc
        except ObjectStorageError as exc:
            raise RuntimeError("object storage is unavailable") from exc

        try:
            with transaction.atomic():
                record = ObjectReference.objects.create(
                    object_key=stored.object_key,
                    reference=stored.reference,
                    checksum_sha256=stored.checksum_sha256,
                )
        except IntegrityError as exc:
            storage.delete(stored.object_key)
            raise Conflict(f"Object with key '{stored.object_key}' already exists.") from exc

        return Response(
            {
                "id": record.id,
                "objectKey": record.object_key,
                "reference": record.reference,
                "checksumSha256": record.checksum_sha256,
            },
            status=201,
        )


class ObjectDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, object_id: int):
        try:
            record = ObjectReference.objects.get(pk=object_id)
        except ObjectReference.DoesNotExist as exc:
            raise Http404() from exc
        return Response(
            {
                "id": record.id,
                "reference": record.reference,
                "checksumSha256": record.checksum_sha256,
            }
        )


class IiifStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        status = IiifClient().configuration_status()
        http_status = 200 if status["status"] == "configured" else 503
        return Response(status, status=http_status)


class CatalogueIndexView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        CatalogueIndex().rebuild()
        return Response({"index": CatalogueIndex().index_name, "status": "rebuilt"}, status=201)

    def delete(self, request):
        CatalogueIndex().delete()
        return Response(status=204)
