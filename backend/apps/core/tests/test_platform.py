"""Scaffold tests for settings, stores, IIIF, and the API error contract."""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import dj_database_url
import pytest
from config.settings.env import require_env
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from rest_framework.exceptions import NotAuthenticated, PermissionDenied, Throttled, ValidationError

from apps.core.exceptions import Conflict, api_exception_handler
from apps.core.iiif import IiifClient
from apps.core.models import ObjectReference
from apps.core.search import CatalogueIndex, SearchNotConfigured
from apps.core.storage import ObjectStorage, ObjectStorageError, StoredObject

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_DIR = BACKEND_DIR.parent
APP_NAMES = [
    "core",
    "accounts",
    "catalogue",
    "circulation",
    "archive",
    "publishing",
    "payments",
    "audit",
]


def test_required_environment_fails_closed(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        require_env("SECRET_KEY")


def test_development_settings_refuse_to_start_without_required_variables():
    env = os.environ.copy()
    env["ILMDAS_SKIP_ENV_FILE"] = "1"
    env.pop("SECRET_KEY", None)
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.development"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr + result.stdout


def test_each_app_has_models_migrations_and_tests():
    for name in APP_NAMES:
        app_dir = BACKEND_DIR / "apps" / name
        assert (app_dir / "models.py").is_file()
        assert (app_dir / "migrations").is_dir()
        assert (app_dir / "tests").is_dir()


def test_example_env_file_contains_names_only():
    text = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")
    assert "SECRET_KEY=" in text
    assert "DATABASE_URL=" in text
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        _name, value = stripped.split("=", 1)
        assert value.strip() == ""
    assert "AKIA" not in text


def test_postgres_url_selects_the_postgres_engine():
    config = dj_database_url.parse("postgres://USER:PASSWORD@localhost:5432/ilmdas")
    assert config["ENGINE"] == "django.db.backends.postgresql"


@pytest.mark.django_db
def test_migrations_apply():
    call_command("migrate", verbosity=0, interactive=False)
    record = ObjectReference.objects.create(
        object_key="manuscripts/fixture",
        reference="s3://ilmdas-files/manuscripts/fixture",
        checksum_sha256="abc",
    )
    assert record.id


def test_catalogue_index_can_be_created_and_deleted_outside_production():
    class Indices:
        def __init__(self):
            self.names: set[str] = set()

        def exists(self, index):
            return index in self.names

        def create(self, index, **_kwargs):
            self.names.add(index)

        def delete(self, index):
            self.names.remove(index)

    class Client:
        def __init__(self):
            self.indices = Indices()

        def ping(self):
            return True

    index = CatalogueIndex(client=Client(), index_name="ilmdas-catalogue")
    index.rebuild()
    assert index.client.indices.exists(index="ilmdas-catalogue")
    index.delete()
    assert not index.client.indices.exists(index="ilmdas-catalogue")


def test_private_upload_stores_a_reference_and_checksum():
    calls = {}

    class FakeClient:
        def put_object(self, **kwargs):
            calls.update(kwargs)

    storage = ObjectStorage(client=FakeClient(), bucket="ilmdas-files")
    stored = storage.put_private("manuscripts/1", b"page")
    assert calls["ACL"] == "private"
    assert stored.checksum_sha256 == hashlib.sha256(b"page").hexdigest()
    assert stored.reference == "s3://ilmdas-files/manuscripts/1"
    assert "http://" not in stored.reference
    assert "https://" not in stored.reference


@pytest.mark.django_db
def test_object_upload_is_not_anonymously_readable(api_client, django_user_model):
    user = django_user_model.objects.create_user(username="staff", password="local-only")
    body = b"page"
    stored = StoredObject(
        reference="s3://ilmdas-files/manuscripts/1",
        checksum_sha256=hashlib.sha256(body).hexdigest(),
        object_key="manuscripts/1",
    )
    api_client.force_authenticate(user)
    with patch("apps.core.views.ObjectStorage.put_private", return_value=stored):
        created = api_client.post(
            "/api/v1/storage/objects/",
            {"objectKey": "manuscripts/1", "bodyBase64": base64.b64encode(body).decode()},
            format="json",
        )
    assert created.status_code == 201
    assert created.data["checksumSha256"] == stored.checksum_sha256
    api_client.force_authenticate(None)
    denied = api_client.get(f"/api/v1/storage/objects/{created.data['id']}/")
    assert denied.status_code == 401
    assert denied.data["code"] == "authentication_error"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("target", "check"),
    [
        ("apps.core.views.database_health", "database"),
        ("apps.core.views.CatalogueIndex.health", "elasticsearch"),
        ("apps.core.views.ObjectStorage.health", "objectStorage"),
    ],
)
def test_health_fails_when_a_store_is_unreachable(api_client, django_user_model, target, check):
    user = django_user_model.objects.create_user(username=f"staff_{check}", password="local-only")
    api_client.force_authenticate(user)
    with patch(target, return_value={"ok": False, "detail": "unreachable"}):
        response = api_client.get("/api/v1/health/")
    assert response.status_code == 503
    assert response.data["status"] == "failed"
    assert response.data["checks"][check] == {"ok": False, "detail": "unreachable"}

    api_client.force_authenticate(None)
    with patch(target, return_value={"ok": False, "detail": "unreachable"}):
        anon_response = api_client.get("/api/v1/health/")
    assert anon_response.status_code == 503
    assert anon_response.data == {"status": "failed"}
    assert "checks" not in anon_response.data


def test_iiif_fixture_and_missing_endpoint():
    missing = IiifClient(endpoint="")
    assert missing.configuration_status()["status"] == "not_configured"
    assert missing.image_info("fixture-image")["status"] == "not_configured"

    seen = {}

    def fake_get(url):
        seen["url"] = url
        return {"@id": url, "width": 10, "height": 10}

    configured = IiifClient(endpoint="http://iiif.internal/iiif/2", http_get=fake_get)
    info = configured.image_info("fixture-image")
    assert info["status"] == "ok"
    assert seen["url"] == "http://iiif.internal/iiif/2/fixture-image/info.json"
    assert "/public/" not in seen["url"]


def test_public_routes_do_not_serve_iiif_images():
    from django.urls import get_resolver

    patterns = " ".join(str(pattern.pattern) for pattern in get_resolver().url_patterns)
    assert "info.json" not in patterns
    assert "iiif" not in patterns


@pytest.mark.django_db
def test_validation_and_authentication_errors_use_different_codes(api_client):
    validation = api_client.post("/api/v1/contract/validation/", {}, format="json")
    authentication = api_client.get("/api/v1/contract/session/")
    assert validation.status_code == 400
    assert authentication.status_code == 401
    assert validation.data["code"] == "validation_error"
    assert authentication.data["code"] == "authentication_error"
    assert validation.data["code"] != authentication.data["code"]
    for response in (validation, authentication):
        assert response.data["correlationId"]
        rendered = str(response.data)
        assert "Traceback" not in rendered
        assert "SECRET" not in rendered
        assert response.headers["X-Correlation-ID"] == response.data["correlationId"]


def test_documented_error_codes_include_correlation_and_hide_internals():
    request = type("Request", (), {"correlation_id": "cid-123"})()
    cases = [
        (ValidationError({"name": ["This field is required."]}), "validation_error", 400),
        (NotAuthenticated(), "authentication_error", 401),
        (PermissionDenied(), "authorization_error", 403),
        (Conflict(), "conflict", 409),
        (Throttled(wait=1), "rate_limit", 429),
        (RuntimeError("database password=super-secret"), "unavailable", 503),
    ]
    for exc, code, status in cases:
        response = api_exception_handler(exc, {"request": request})
        assert response.status_code == status
        assert response.data["code"] == code
        assert response.data["correlationId"] == "cid-123"
        assert "super-secret" not in str(response.data)
        assert "Traceback" not in str(response.data)


def test_ci_workflow_runs_both_test_suites_and_fails_on_failure():
    workflow = (REPO_DIR / ".github" / "workflows" / "ci-cd.yml").read_text(encoding="utf-8")
    assert "pytest" in workflow
    assert "--cov-fail-under=80" in workflow
    assert "npm run test:coverage" in workflow
    assert "npm run build" in workflow
    assert "backend-smoke:" in workflow
    assert "Backend smoke" in workflow
    assert "pytest -m smoke" in workflow
    assert "frontend-e2e:" in workflow
    assert "Frontend smoke" in workflow
    assert "npm run test:e2e" in workflow
    assert workflow.count("if: false") >= 2
    assert "continue-on-error: true" not in workflow


@pytest.mark.django_db
def test_health_ok_when_stores_are_reachable(api_client):
    with (
        patch("apps.core.views.database_health", return_value={"ok": True, "detail": "reachable"}),
        patch(
            "apps.core.views.CatalogueIndex.health",
            return_value={"ok": True, "detail": "reachable"},
        ),
        patch(
            "apps.core.views.ObjectStorage.health",
            return_value={"ok": True, "detail": "reachable"},
        ),
    ):
        response = api_client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.data["status"] == "ok"


@pytest.mark.django_db
def test_validation_probe_accepts_valid_payload(api_client):
    response = api_client.post(
        "/api/v1/contract/validation/",
        {"name": "record"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["name"] == "record"


@pytest.mark.django_db
def test_session_probe_reports_authenticated_user(api_client, django_user_model):
    user = django_user_model.objects.create_user(username="reader", password="local-only")
    api_client.force_authenticate(user)
    response = api_client.get("/api/v1/contract/session/")
    assert response.status_code == 200
    assert response.data["authenticated"] is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("authorization", 403, "authorization_error"),
        ("not_found", 404, "not_found"),
        ("conflict", 409, "conflict"),
        ("rate_limit", 429, "rate_limit"),
        ("unavailable", 503, "unavailable"),
        ("authentication", 401, "authentication_error"),
        ("", 400, "validation_error"),
    ],
)
def test_error_probe_kinds(api_client, kind, status, code):
    response = api_client.get("/api/v1/contract/errors/", {"kind": kind})
    assert response.status_code == status
    assert response.data["code"] == code


@pytest.mark.django_db
def test_object_upload_rejects_missing_and_invalid_payload(api_client, django_user_model):
    user = django_user_model.objects.create_user(username="uploader", password="local-only")
    api_client.force_authenticate(user)
    missing = api_client.post("/api/v1/storage/objects/", {}, format="json")
    assert missing.status_code == 400
    assert missing.data["code"] == "validation_error"
    invalid = api_client.post(
        "/api/v1/storage/objects/",
        {"objectKey": "manuscripts/1", "bodyBase64": "!!!"},
        format="json",
    )
    assert invalid.status_code == 400
    assert invalid.data["code"] == "validation_error"


@pytest.mark.django_db
def test_object_upload_maps_storage_failure(api_client, django_user_model):
    from apps.core.storage import ObjectStorageError

    user = django_user_model.objects.create_user(username="uploader2", password="local-only")
    api_client.force_authenticate(user)
    with patch(
        "apps.core.views.ObjectStorage.put_private",
        side_effect=ObjectStorageError("down"),
    ):
        response = api_client.post(
            "/api/v1/storage/objects/",
            {"objectKey": "manuscripts/1", "bodyBase64": base64.b64encode(b"page").decode()},
            format="json",
        )
    assert response.status_code == 503
    assert response.data["code"] == "unavailable"


@pytest.mark.django_db
def test_object_detail_and_not_found(api_client, django_user_model):
    user = django_user_model.objects.create_user(username="viewer", password="local-only")
    record = ObjectReference.objects.create(
        object_key="manuscripts/detail",
        reference="s3://ilmdas-files/manuscripts/detail",
        checksum_sha256="abc",
    )
    api_client.force_authenticate(user)
    found = api_client.get(f"/api/v1/storage/objects/{record.id}/")
    assert found.status_code == 200
    assert found.data["reference"] == record.reference
    missing = api_client.get("/api/v1/storage/objects/99999/")
    assert missing.status_code == 404
    assert missing.data["code"] == "not_found"


@pytest.mark.django_db
def test_iiif_status_endpoint(api_client, django_user_model):
    user = django_user_model.objects.create_user(username="iiif", password="local-only")
    api_client.force_authenticate(user)
    with patch(
        "apps.core.views.IiifClient.configuration_status",
        return_value={"status": "not_configured", "message": "IIIF endpoint is not configured"},
    ):
        missing = api_client.get("/api/v1/iiif/status/")
    assert missing.status_code == 503
    with patch(
        "apps.core.views.IiifClient.configuration_status",
        return_value={"status": "configured", "message": "IIIF endpoint is configured"},
    ):
        configured = api_client.get("/api/v1/iiif/status/")
    assert configured.status_code == 200


@pytest.mark.django_db
def test_catalogue_index_endpoints(api_client, django_user_model):
    user = django_user_model.objects.create_user(username="search", password="local-only")
    api_client.force_authenticate(user)
    with (
        patch("apps.core.views.CatalogueIndex.rebuild"),
        patch("apps.core.views.CatalogueIndex.delete"),
        patch.object(CatalogueIndex, "index_name", "ilmdas-catalogue", create=True),
    ):
        rebuilt = api_client.post("/api/v1/search/catalogue-index/")
        deleted = api_client.delete("/api/v1/search/catalogue-index/")
    assert rebuilt.status_code == 201
    assert rebuilt.data["status"] == "rebuilt"
    assert deleted.status_code == 204


def test_catalogue_index_guards_and_health(settings):
    settings.ELASTICSEARCH_URL = ""
    settings.ENVIRONMENT = "production"
    with pytest.raises(PermissionError):
        CatalogueIndex(client=object(), index_name="ilmdas-catalogue").rebuild()
    settings.ENVIRONMENT = "development"
    with pytest.raises(SearchNotConfigured):
        CatalogueIndex(index_name="ilmdas-catalogue")._client()
    assert CatalogueIndex().health() == {"ok": False, "detail": "not configured"}

    class Unreachable:
        def ping(self):
            return False

    assert CatalogueIndex(client=Unreachable()).health() == {
        "ok": False,
        "detail": "unreachable",
    }

    class Boom:
        def ping(self):
            raise RuntimeError("down")

    assert CatalogueIndex(client=Boom()).health() == {"ok": False, "detail": "unreachable"}

    class Indices:
        def exists(self, index):
            return True

        def delete(self, index):
            return None

        def create(self, index, **kwargs):
            return None

    class Existing:
        def __init__(self):
            self.indices = Indices()

        def ping(self):
            return True

    CatalogueIndex(client=Existing(), index_name="ilmdas-catalogue").rebuild()


@pytest.mark.django_db
def test_database_health_success_and_failure():
    from apps.core.views import database_health

    assert database_health() == {"ok": True, "detail": "reachable"}
    with patch("apps.core.views.connection.cursor", side_effect=RuntimeError("down")):
        assert database_health() == {"ok": False, "detail": "unreachable"}


def test_object_storage_configuration_and_health(settings):
    settings.S3_ENDPOINT_URL = ""
    settings.S3_BUCKET_NAME = ""
    storage = ObjectStorage()
    with pytest.raises(ObjectStorageError):
        storage._client()
    assert storage.health() == {"ok": False, "detail": "not configured"}

    class Boom:
        def head_bucket(self, Bucket):
            raise RuntimeError("down")

    settings.S3_ENDPOINT_URL = "http://localhost:9010"
    broken = ObjectStorage(client=Boom(), bucket="ilmdas-files")
    assert broken.health() == {"ok": False, "detail": "unreachable"}

    class Ok:
        def head_bucket(self, Bucket):
            return {}

    healthy = ObjectStorage(client=Ok(), bucket="ilmdas-files")
    assert healthy.health() == {"ok": True, "detail": "reachable"}


def test_iiif_configured_status_and_default_get():
    configured = IiifClient(endpoint="http://iiif.internal/iiif/2", http_get=lambda url: {})
    assert configured.configuration_status()["status"] == "configured"
    with patch("apps.core.iiif.urlopen") as urlopen:
        response = type(
            "Resp",
            (),
            {
                "read": lambda self: b'{"width":1}',
                "__enter__": lambda self: self,
                "__exit__": lambda self, *args: False,
            },
        )()
        urlopen.return_value = response
        from apps.core.iiif import _default_get

        assert _default_get("http://example/info.json") == {"width": 1}


def test_classify_not_found_and_client_error_status():
    from django.http import Http404
    from rest_framework.exceptions import NotFound

    from apps.core.exceptions import classify

    assert classify(Http404())[0] == "not_found"
    assert classify(NotFound())[0] == "not_found"

    class ClientError(Exception):
        status_code = 418

    code, status, _errors = classify(ClientError())
    assert code == "unavailable"
    assert status == 418


@pytest.mark.django_db
def test_object_reference_str():
    record = ObjectReference.objects.create(
        object_key="manuscripts/str",
        reference="s3://ilmdas-files/manuscripts/str",
        checksum_sha256="abc",
    )
    assert str(record) == "manuscripts/str"


@pytest.mark.django_db
def test_upload_existing_object_key_returns_conflict_and_preserves_object(
    api_client, django_user_model
):
    user = django_user_model.objects.create_user(username="archivist", password="local-only")
    api_client.force_authenticate(user)
    initial_record = ObjectReference.objects.create(
        object_key="archive/existing/file",
        reference="s3://ilmdas-files/archive/existing/file",
        checksum_sha256="original-checksum",
    )
    with patch("apps.core.views.ObjectStorage.put_private") as mock_put:
        response = api_client.post(
            "/api/v1/storage/objects/",
            {
                "objectKey": "archive/existing/file",
                "bodyBase64": base64.b64encode(b"new-content").decode(),
            },
            format="json",
        )
        assert response.status_code == 409
        assert response.data["code"] == "conflict"
        mock_put.assert_not_called()

    refreshed = ObjectReference.objects.get(pk=initial_record.id)
    assert refreshed.checksum_sha256 == "original-checksum"


@pytest.mark.django_db
def test_upload_without_object_key_generates_server_key(api_client, django_user_model):
    user = django_user_model.objects.create_user(username="uploader-auto", password="local-only")
    api_client.force_authenticate(user)
    body = b"generated-content"
    stored = StoredObject(
        reference="s3://ilmdas-files/archive/auto/123",
        checksum_sha256=hashlib.sha256(body).hexdigest(),
        object_key="archive/auto/123",
    )
    with patch("apps.core.views.ObjectStorage.put_private", return_value=stored) as mock_put:
        response = api_client.post(
            "/api/v1/storage/objects/",
            {"bodyBase64": base64.b64encode(body).decode()},
            format="json",
        )
        assert response.status_code == 201
        assert response.data["objectKey"] == "archive/auto/123"
        called_key = mock_put.call_args[0][0]
        assert called_key.startswith("archive/")


def test_production_settings_hardening():
    from config.settings import production

    assert production.SECURE_SSL_REDIRECT is True
    assert production.SECURE_HSTS_SECONDS == 31536000
    assert production.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
    assert production.SECURE_HSTS_PRELOAD is False
    assert production.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert production.SESSION_COOKIE_SECURE is True
    assert production.CSRF_COOKIE_SECURE is True
    assert production.SESSION_COOKIE_AGE == 60 * 60 * 8
    assert production.SESSION_EXPIRE_AT_BROWSER_CLOSE is True
    assert production.DEBUG is False
    assert production.ENVIRONMENT == "production"


def test_wsgi_and_asgi_default_to_production():
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["SECRET_KEY"] = "test-secret"
    env["DATABASE_URL"] = "postgres://localhost/test"
    env["ILMDAS_SKIP_ENV_FILE"] = "1"
    for module in ("config.wsgi", "config.asgi"):
        cmd = (
            f"import os; import {module}; from django.conf import settings; "
            "print(settings.ENVIRONMENT, settings.DEBUG)"
        )
        result = subprocess.run(
            [sys.executable, "-c", cmd],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "production False" in result.stdout


def test_production_settings_reject_debug_env():
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
    env["DEBUG"] = "1"
    env["SECRET_KEY"] = "test-secret"
    env["DATABASE_URL"] = "postgres://localhost/test"
    env["ILMDAS_SKIP_ENV_FILE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "DEBUG cannot be enabled in production" in result.stderr


@pytest.mark.django_db
def test_contract_probes_return_404_in_production(api_client, settings):
    settings.ENVIRONMENT = "production"
    for path in [
        "/api/v1/contract/validation/",
        "/api/v1/contract/session/",
        "/api/v1/contract/errors/",
    ]:
        res = api_client.get(path)
        assert res.status_code == 404
    post_res = api_client.post("/api/v1/contract/validation/", {"name": "test"}, format="json")
    assert post_res.status_code == 404


@pytest.mark.django_db
def test_anonymous_health_returns_only_overall_status(api_client):
    response = api_client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.data == {"status": "ok"}
    assert "checks" not in response.data

