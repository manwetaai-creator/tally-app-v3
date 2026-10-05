"""Small, server-side wrapper around the Meta Graph API used by WhatsApp onboarding and messaging."""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .config import get_settings

log = logging.getLogger("manweta.meta")


def _settings():
    return get_settings()


def graph_url(path: str) -> str:
    s = _settings()
    return f"https://graph.facebook.com/{s.meta_graph_api_version}/{path.lstrip('/')}"


def _raise_graph(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": {"message": response.text[:500]}}
    err = payload.get("error") or {}
    message = err.get("message") or f"Meta Graph API returned HTTP {response.status_code}"
    code = err.get("code")
    subcode = err.get("error_subcode")
    details = f" (code={code}, subcode={subcode})" if code or subcode else ""
    raise RuntimeError(message + details)


def exchange_signup_code(code: str) -> str:
    """Exchange Embedded Signup's one-time code for the customer's business token."""
    s = _settings()
    if not s.meta_app_id or not s.meta_app_secret:
        raise RuntimeError("META_APP_ID and META_APP_SECRET must be configured")
    with httpx.Client(timeout=s.meta_http_timeout_seconds) as client:
        r = client.get(
            graph_url("oauth/access_token"),
            params={"client_id": s.meta_app_id, "client_secret": s.meta_app_secret, "code": code},
        )
    _raise_graph(r)
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError("Meta did not return an access token for Embedded Signup")
    return token


def debug_token(token: str) -> dict[str, Any]:
    s = _settings()
    app_token = f"{s.meta_app_id}|{s.meta_app_secret}"
    with httpx.Client(timeout=s.meta_http_timeout_seconds) as client:
        r = client.get(graph_url("debug_token"), params={"input_token": token, "access_token": app_token})
    _raise_graph(r)
    return r.json().get("data") or {}


def get_waba(token: str, waba_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.get(graph_url(waba_id), params={"fields": "id,name"}, headers={"Authorization": f"Bearer {token}"})
    _raise_graph(r)
    return r.json()


def get_phone_numbers(token: str, waba_id: str) -> list[dict[str, Any]]:
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.get(
            graph_url(f"{waba_id}/phone_numbers"),
            params={"fields": "id,display_phone_number,verified_name,quality_rating,name_status"},
            headers={"Authorization": f"Bearer {token}"},
        )
    _raise_graph(r)
    return r.json().get("data") or []


def get_phone(token: str, phone_number_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.get(
            graph_url(phone_number_id),
            params={"fields": "id,display_phone_number,verified_name,quality_rating,name_status"},
            headers={"Authorization": f"Bearer {token}"},
        )
    _raise_graph(r)
    return r.json()


def register_phone(token: str, phone_number_id: str, pin: str) -> None:
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError("WhatsApp registration PIN must be exactly 6 digits")
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.post(
            graph_url(f"{phone_number_id}/register"),
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product": "whatsapp", "pin": pin},
        )
    _raise_graph(r)


def subscribe_waba(token: str, waba_id: str) -> None:
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.post(graph_url(f"{waba_id}/subscribed_apps"), headers={"Authorization": f"Bearer {token}"})
    _raise_graph(r)


def unsubscribe_waba(token: str, waba_id: str) -> None:
    """Stop webhook delivery for a WABA (called when the customer disconnects)."""
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.request("DELETE", graph_url(f"{waba_id}/subscribed_apps"), headers={"Authorization": f"Bearer {token}"})
    _raise_graph(r)


def get_templates(token: str, waba_id: str, name: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, str] = {"fields": "id,name,language,category,status,components,rejected_reason", "limit": "100"}
    if name:
        params["name"] = name
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.get(
            graph_url(f"{waba_id}/message_templates"),
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    _raise_graph(r)
    return r.json().get("data") or []


def send_template(
    token: str,
    phone_number_id: str,
    to: str,
    template_name: str,
    language: str,
    body_params: list[str],
) -> str:
    template: dict[str, Any] = {"name": template_name, "language": {"code": language}}
    if body_params:
        template["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(value)} for value in body_params],
        }]
    payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.post(
            graph_url(f"{phone_number_id}/messages"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    _raise_graph(r)
    data = r.json()
    messages = data.get("messages") or []
    message_id = messages[0].get("id") if messages else None
    if not message_id:
        raise RuntimeError("Meta accepted the request but did not return a WhatsApp message ID")
    return message_id


# --------------------------------------------------------------------------- template management
_TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
_VAR_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
TEMPLATE_CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")


def build_template_payload(name: str, category: str, language: str, body_text: str, examples: list[str]) -> dict[str, Any]:
    """Validate user input and build the body of POST /{waba_id}/message_templates. Raises ValueError with a readable message."""
    name = (name or "").strip()
    category = (category or "").strip().upper()
    language = (language or "").strip()
    body_text = (body_text or "").strip()
    examples = [e.strip() for e in examples if e and e.strip()]

    if not _TEMPLATE_NAME_RE.match(name):
        raise ValueError("Template name may only contain lowercase letters, numbers and underscores")
    if category not in ("UTILITY", "MARKETING"):
        raise ValueError("Category must be UTILITY or MARKETING")
    if not _LANG_RE.match(language):
        raise ValueError("Language must look like en_US or hi")
    if not body_text:
        raise ValueError("Message text is required")
    if len(body_text) > 1024:
        raise ValueError("Message text can be at most 1024 characters")

    numbers = [int(n) for n in _VAR_RE.findall(body_text)]
    expected = list(range(1, len(set(numbers)) + 1))
    if sorted(set(numbers)) != expected:
        raise ValueError("Variables must be numbered {{1}}, {{2}}, {{3}} ... without gaps")
    if numbers and (body_text.startswith("{{") or body_text.endswith("}}")):
        raise ValueError("Message text cannot start or end with a variable - add some words before or after it")
    if len(set(numbers)) != len(examples):
        raise ValueError(f"Provide exactly {len(set(numbers))} sample value(s), one per line (you gave {len(examples)})")

    body: dict[str, Any] = {"type": "BODY", "text": body_text}
    if examples:
        body["example"] = {"body_text": [examples]}
    return {"name": name, "category": category, "language": language, "components": [body]}


def create_template(token: str, waba_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.post(
            graph_url(f"{waba_id}/message_templates"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    _raise_graph(r)
    return r.json()


def delete_template(token: str, waba_id: str, name: str) -> None:
    with httpx.Client(timeout=_settings().meta_http_timeout_seconds) as client:
        r = client.request("DELETE", graph_url(f"{waba_id}/message_templates"),
                           headers={"Authorization": f"Bearer {token}"}, params={"name": name})
    _raise_graph(r)
