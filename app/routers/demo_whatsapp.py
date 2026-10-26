"""Restricted Meta WhatsApp Cloud API demo endpoint for App Review recording.

Enable only temporarily for the demo. It can send only the one recipient configured
in META_DEMO_RECIPIENT and only the hello_world template.
"""
import hmac
import logging
import re

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings

router = APIRouter(prefix="/api/v1/demo/whatsapp", tags=["WhatsApp demo"])
log = logging.getLogger("manweta.whatsapp_demo")


class SendDemoMessage(BaseModel):
    recipient: str = Field(min_length=8, max_length=20)


@router.post("/send")
def send_demo_message(
    body: SendDemoMessage,
    x_demo_key: str | None = Header(default=None, alias="X-Demo-Key"),
):
    s = get_settings()
    expected_key = s.meta_demo_access_key
    token = s.meta_demo_access_token
    phone_number_id = s.meta_demo_phone_number_id
    allowed_recipient = re.sub(r"\D", "", s.meta_demo_recipient or "")

    # Fail closed unless every demo-only setting is present.
    if not all([expected_key, token, phone_number_id, allowed_recipient]):
        raise HTTPException(503, "WhatsApp demo is not configured. Set the META_DEMO_* environment variables.")
    if not x_demo_key or not hmac.compare_digest(x_demo_key, expected_key):
        raise HTTPException(401, "Invalid demo key.")
    recipient = re.sub(r"\D", "", body.recipient or "")
    if not recipient or recipient != allowed_recipient:
        raise HTTPException(403, "For this demo, messages can only be sent to the configured META_DEMO_RECIPIENT.")

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
            data = {"error": {"message": response.text[:300]}}
        if not response.is_success:
            error = data.get("error") or {}
            # Do not return request headers or access token to the browser.
            raise HTTPException(response.status_code, error.get("message", "Meta rejected the message request."))
        messages = data.get("messages") or []
        message_id = messages[0].get("id") if messages else None
        if not message_id:
            raise HTTPException(502, "Meta returned success without a message ID.")
        return {
            "success": True,
            "message": "Meta accepted the test message. Confirm actual delivery in WhatsApp.",
            "message_id": message_id,
            "recipient": recipient,
            "template": "hello_world",
        }
    except httpx.RequestError:
        log.exception("Unable to connect to Meta Graph API")
        raise HTTPException(502, "Could not connect to Meta Graph API. Try again.")
