"""Production-shaped settings. Deployment to the OSTA data center is a later story."""

from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

_debug_env = os.environ.get("DEBUG")
if _debug_env is not None and _debug_env.strip().lower() not in ("false", "0", "no", "off", ""):
    raise ImproperlyConfigured("DEBUG cannot be enabled in production.")

DEBUG = False
ENVIRONMENT = "production"

# SEC-05: HTTPS and cookie hardening
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False  # enable only after review
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
