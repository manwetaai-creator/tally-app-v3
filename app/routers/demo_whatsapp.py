"""Restricted Meta WhatsApp Cloud API demo endpoint for App Review recording.

Enable only temporarily for the demo. It can send only the one recipient configured
in META_DEMO_RECIPIENT and only the hello_world template.
"""
import logging
import re
import threading
import time

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings

router = APIRouter(prefix="/api/v1/demo/whatsapp", tags=["WhatsApp demo"])
log = logging.getLogger("manweta.whatsapp_demo")
_send_lock = threading.Lock()
_last_send_at = 0.0
_SEND_COOLDOWN_SECONDS = 30


class SendDemoMessage(BaseModel):
    recipient: str = Field(min_length=8, max_length=20)


def normalize_recipient(recipient: str) -> str:
    value = recipient.strip()
    if not re.fullmatch(r"\+?[\d\s().-]+", value):
        return ""
    digits = re.sub(r"\D", "", value)
    return digits if 8 <= len(digits) <= 15 else ""


def mask_recipient(recipient: str) -> str:
    digits = normalize_recipient(recipient or "")
    if not digits:
        return "Not configured"
    return f"{'+' if recipient.strip().startswith('+') else ''}{'•' * (len(digits) - 4)}{digits[-4:]}"


def _meta_error_message(data: object, token: str, phone_number_id: str) -> str:
    generic_message = "Meta rejected the message request. Check the demo configuration and try again."
    if not isinstance(data, dict):
        return generic_message
    error = data.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return generic_message
    message = " ".join(error["message"].split())
    for secret in (token, phone_number_id):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:300] or generic_message


@router.post("/send")
def send_demo_message(
    body: SendDemoMessage,
):
    global _last_send_at

    s = get_settings()
    token = s.meta_demo_access_token
    phone_number_id = s.meta_demo_phone_number_id
    configured_recipient = s.meta_demo_recipient or ""
    allowed_recipient = normalize_recipient(configured_recipient)

    # Fail closed unless every demo-only setting is present.
    if not all([token, phone_number_id, allowed_recipient]):
        raise HTTPException(503, "WhatsApp demo is not configured. Set the META_DEMO_* environment variables.")
    recipient = normalize_recipient(body.recipient)
    if recipient != allowed_recipient:
        raise HTTPException(403, "For this demo, messages can only be sent to the configured META_DEMO_RECIPIENT.")

    with _send_lock:
        now = time.monotonic()
        retry_after = _SEND_COOLDOWN_SECONDS - (now - _last_send_at)
        if retry_after > 0:
            raise HTTPException(429, f"Please wait {int(retry_after) + 1} seconds before sending another demo message.")
        _last_send_at = now

    url = f"https://graph.facebook.com/{s.meta_graph_api_version}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "template",
        "template": {"name": "hello_world", "language": {"code": "en_US"}},
    }
    try:
        with httpx.Client(timeout=s.meta_http_timeout_seconds) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        if not response.is_success:
            raise HTTPException(
                response.status_code,
                _meta_error_message(data, token, phone_number_id),
            )
        messages = data.get("messages") if isinstance(data, dict) else None
        message = messages[0] if isinstance(messages, list) and messages else None
        message_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise HTTPException(502, "Meta returned success without a message ID.")
        return {
            "success": True,
            "message": "Meta accepted the test message. Confirm actual delivery in WhatsApp.",
            "message_id": message_id,
            "recipient_masked": mask_recipient(configured_recipient),
            "template": "hello_world",
        }
    except httpx.RequestError as exc:
        log.warning("Meta Graph API request failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Could not connect to Meta Graph API. Try again.")
