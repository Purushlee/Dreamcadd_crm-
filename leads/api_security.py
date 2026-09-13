import hmac
import hashlib
import functools
import logging
from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def require_n8n_api_key(view_func):
    """
    Decorator for Django views to enforce X-API-KEY header verification against N8N_API_KEY setting.
    """
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        expected_key = getattr(settings, "N8N_API_KEY", "")
        provided_key = request.headers.get("X-API-KEY") or request.META.get("HTTP_X_API_KEY") or request.GET.get("api_key")

        if not expected_key or provided_key != expected_key:
            logger.warning("Unauthorized n8n API access attempt from %s", request.META.get("REMOTE_ADDR"))
            return JsonResponse({"error": "Unauthorized", "detail": "Invalid or missing X-API-KEY header."}, status=401)

        return view_func(request, *args, **kwargs)

    return wrapper


def verify_meta_webhook_signature(request) -> bool:
    """
    Validates Meta WhatsApp Cloud API X-Hub-Signature-256 header using META_APP_SECRET.
    Returns True if valid or if META_APP_SECRET is not configured (dev mode fallback).
    """
    app_secret = getattr(settings, "META_APP_SECRET", "")
    if not app_secret:
        return True  # Bypass in local development if secret is not set

    signature_header = request.headers.get("X-Hub-Signature-256") or request.META.get("HTTP_X_HUB_SIGNATURE_256")
    if not signature_header or not signature_header.startswith("sha256="):
        logger.warning("Missing or malformed Meta X-Hub-Signature-256 header.")
        return False

    expected_sig = signature_header.split("sha256=")[1]
    calculated_sig = hmac.new(
        app_secret.encode("utf-8"),
        request.body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_sig, calculated_sig)
