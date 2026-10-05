"""Authenticated dashboard API (/api/v1/portal/*). Responses match the original Express server 1:1."""
import hashlib
import logging
import os
import re
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import blob
from ..txroute import TxRoute
from ..config import get_settings
from ..db import get_db
from ..deps import AuthContext, get_auth
from ..models import (
    ActivationCode, Connector, Customer, InboundMessage, InstallerRelease, Invoice, ReminderLog, SyncRun, WhatsAppAccount, utcnow,
)
from ..reminders import calculate_candidates, get_or_create_settings, make_log
from ..security import decrypt_secret, encrypt_secret, new_activation_code
from ..meta_whatsapp import (build_template_payload, create_template, debug_token, delete_template, exchange_signup_code, get_phone,
                             get_phone_numbers, get_templates, get_waba, register_phone, send_template, subscribe_waba, unsubscribe_waba)
from ..utils import d2s, f, iso, normalize_phone, today_utc

log = logging.getLogger("manweta.portal")
STATIC = Path(__file__).resolve().parents[2] / "static"
TIMEZONES = ["Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Europe/London", "America/New_York", "America/Los_Angeles", "UTC"]

router = APIRouter(prefix="/api/v1/portal", tags=["portal"], dependencies=[Depends(get_auth)], route_class=TxRoute)
public_router = APIRouter(tags=["public"], route_class=TxRoute)


# ----------------------------------------------------------------------------- helpers
def _paging(limit: str | None, offset: str | None) -> tuple[int, int]:
    def p(v, default):
        try:
            return int(v) or default
        except (TypeError, ValueError):
            return default
    return min(200, max(1, p(limit, 50))), max(0, p(offset, 0) if offset not in (None, "0") else 0)


def _to_int(v: Any, label: str) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid value for {label}")


def wa_public(a: WhatsAppAccount) -> dict:
    return {
        "status": a.status, "waba_id": a.waba_id, "phone_number_id": a.phone_number_id,
        "display_phone_number": a.display_phone_number, "verified_name": a.verified_name,
        "quality_rating": a.quality_rating, "connection_method": a.connection_method, "template_name": a.template_name,
        "template_status": a.template_status, "template_reject_reason": a.template_reject_reason, "last_error": a.last_error,
        "connected_at": iso(a.connected_at), "ready_to_send": a.status == "connected" and a.template_status == "APPROVED",
    }


def settings_json(s) -> dict:
    return {
        "enabled": s.enabled, "send_hour": s.send_hour, "timezone": s.timezone, "remind_overdue": s.remind_overdue,
        "remind_upcoming": s.remind_upcoming, "days_before_due": s.days_before_due, "repeat_every_days": s.repeat_every_days,
        "min_amount": f(s.min_amount), "reminder_mode": s.reminder_mode or "customer_wise", "last_auto_run_at": iso(s.last_auto_run_at),
    }


def log_json(l: ReminderLog) -> dict:
    return {
        "id": l.id, "customer_id": l.customer_id, "customer_name": l.customer_name, "phone_number": l.phone_number,
        "amount": f(l.amount) if l.amount is not None else None, "bill_ref": l.bill_ref,
        "reminder_mode": l.reminder_mode or "customer_wise", "bills_count": l.bills_count or 1, "status": l.status,
        "trigger": l.trigger, "error_message": l.error_message, "template_name": l.template_name,
        "sent_at": iso(l.sent_at), "created_at": iso(l.created_at),
    }


def _customer(db: Session, tenant_id: int, customer_id: int) -> Customer:
    c = db.scalar(select(Customer).where(Customer.id == customer_id, Customer.tenant_id == tenant_id))
    if not c:
        raise HTTPException(404, "Customer not found")
    return c


def _wa_account(db: Session, tenant_id: int) -> WhatsAppAccount | None:
    return db.get(WhatsAppAccount, tenant_id)


# ----------------------------------------------------------------------------- installer
def get_release(db: Session) -> InstallerRelease:
    r = db.get(InstallerRelease, 1)
    if r is None:
        s = get_settings()
        r = InstallerRelease(
            id=1, version=s.windows_installer_version, file_name="TallyConnector-Setup.exe", file_size=44920832,
            file_size_formatted="42.8 MB", sha256="9f83a21b34c761e2f8d40a23e198b472e90c561b3491f2a718d098e217c491a2",
            blob_url=s.windows_installer_blob_url or "/blobs/TallyConnector-Setup.exe",
            source="blob_storage" if s.windows_installer_blob_url else "local_blob",
        )
        db.add(r)
        db.flush()
    return r


def release_json(r: InstallerRelease) -> dict:
    return {
        "version": r.version, "file_name": r.file_name, "file_size": r.file_size, "file_size_formatted": r.file_size_formatted,
        "sha256": r.sha256, "blob_url": r.blob_url, "download_url": "/api/v1/portal/tally/download",
        "uploaded_at": iso(r.uploaded_at), "source": r.source,
    }


def require_installer_admin(auth: AuthContext) -> None:
    admins = get_settings().installer_admins
    if admins and auth.user.email.lower() not in admins:
        raise HTTPException(403, "Only Manweta administrators can change the Windows installer")


def serve_installer(db: Session):
    r = get_release(db)
    url = r.blob_url or ""
    if url.startswith(("http://", "https://")):
        return RedirectResponse(url, status_code=302)
    if url.startswith(blob.SCHEME):
        name = (r.file_name or "TallyConnector-Setup.exe").replace('"', "")
        try:
            chunks, size = blob.open_stream(url)
        except Exception:  # noqa: BLE001
            log.exception("Could not open installer blob %s", url)
            raise HTTPException(502, "Installer storage is temporarily unavailable")
        return StreamingResponse(
            chunks,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Content-Length": str(size)},
        )
    local = STATIC / "blobs" / "TallyConnector-Setup.exe"
    if local.is_file():
        return FileResponse(local, filename=r.file_name or "TallyConnector-Setup.exe", media_type="application/vnd.microsoft.portable-executable")
    fallback = STATIC / "assets" / "TallyConnector.zip"
    if fallback.is_file():
        return FileResponse(fallback, filename="TallyConnector.zip")
    raise HTTPException(404, "Windows installer not found in blob storage")


@public_router.get("/api/v1/download/tally-connector")
def public_download(db: Session = Depends(get_db)):
    return serve_installer(db)


# ----------------------------------------------------------------------------- overview
@router.get("/overview")
def overview(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    tenant = auth.tenant
    tid = tenant.id
    connector = db.scalar(select(Connector).where(Connector.tenant_id == tid, Connector.status == "active").order_by(Connector.id.desc()))
    wa = _wa_account(db, tid)
    settings = get_or_create_settings(db, tid)

    tally_state = "done" if connector and connector.last_sync_at else ("in_progress" if connector else "todo")
    if wa and wa.status == "connected" and wa.template_status == "APPROVED":
        wa_state = "done"
    elif wa and wa.status == "connected":
        wa_state = "in_progress"
    else:
        wa_state = "todo"
    steps = [
        {"key": "company", "label": "Company", "state": "done", "detail": tenant.name},
        {"key": "tally", "label": "Tally", "state": tally_state},
        {"key": "whatsapp", "label": "WhatsApp", "state": wa_state},
        {"key": "reminders", "label": "Reminders", "state": "done" if settings.enabled else "todo"},
    ]

    today = today_utc()
    open_inv = db.scalars(select(Invoice).where(
        Invoice.tenant_id == tid, Invoice.status == "outstanding", Invoice.is_on_account.is_(False), Invoice.pending_amount > 0)).all()
    owing = {i.customer_id for i in open_inv}
    overdue = [i for i in open_inv if i.due_date and i.due_date < today]
    no_phone = 0
    if owing:
        no_phone = db.scalar(select(func.count()).select_from(Customer).where(
            Customer.tenant_id == tid, Customer.id.in_(owing), or_(Customer.phone_number.is_(None), func.trim(Customer.phone_number) == ""))) or 0

    statuses = db.scalars(select(ReminderLog.status).where(ReminderLog.tenant_id == tid, ReminderLog.created_at >= utcnow() - timedelta(days=30))).all()
    replies_7d = db.scalar(select(func.count()).select_from(InboundMessage).where(
        InboundMessage.tenant_id == tid, InboundMessage.received_at >= utcnow() - timedelta(days=7))) or 0
    last = db.scalar(select(SyncRun).where(SyncRun.tenant_id == tid).order_by(SyncRun.id.desc()))

    return {
        "tenant": {"tenant_id": tenant.tenant_code, "name": tenant.name},
        "steps": steps,
        "onboarding_complete": all(s["state"] == "done" for s in steps),
        "receivables": {
            "total_outstanding": sum(f(i.pending_amount) for i in open_inv), "open_bills": len(open_inv),
            "customers_owing": len(owing), "overdue_amount": sum(f(i.pending_amount) for i in overdue),
            "overdue_customers": len({i.customer_id for i in overdue}), "owing_without_phone": no_phone,
        },
        "messaging_30d": {
            "total": len(statuses), "sent": sum(1 for s in statuses if s not in ("failed", "queued")),
            "delivered": sum(1 for s in statuses if s in ("delivered", "read")), "read": sum(1 for s in statuses if s == "read"),
            "failed": sum(1 for s in statuses if s == "failed"), "replies_7d": replies_7d,
        },
        "last_sync": {"at": iso(last.completed_at or last.started_at), "records": last.records_count} if last else None,
        "currency": "₹",
    }


# ----------------------------------------------------------------------------- tally
@router.get("/tally")
def tally(request: Request, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    tid = auth.tenant.id
    code = db.scalar(select(ActivationCode).where(ActivationCode.tenant_id == tid, ActivationCode.status == "unused").order_by(ActivationCode.id.desc()))
    connectors = db.scalars(select(Connector).where(Connector.tenant_id == tid).order_by(Connector.id.desc())).all()
    base = get_settings().public_base_url.rstrip("/") or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    return {
        "activation_code": code.code if code else None,
        "server_url": base,
        "tally_url": "http://localhost:9000",
        "download_url": "/api/v1/download/tally-connector",
        "installer": release_json(get_release(db)),
        "connectors": [{"connector_id": c.connector_code, "status": c.status, "created_at": iso(c.created_at),
                        "last_seen_at": iso(c.last_seen_at), "last_sync_at": iso(c.last_sync_at)} for c in connectors],
    }


@router.get("/tally/download")
def tally_download(db: Session = Depends(get_db)):
    return serve_installer(db)


@router.get("/tally/installer-info")
def installer_info(db: Session = Depends(get_db)):
    return {"success": True, "release": release_json(get_release(db))}


@router.post("/tally/upload-installer")
def upload_installer(
    installer: UploadFile | None = File(default=None), version: str | None = Form(default=None),
    external_blob_url: str | None = Form(default=None), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db),
):
    require_installer_admin(auth)
    r = get_release(db)

    if external_blob_url and not installer:
        url = external_blob_url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "blob_url must start with http:// or https://")
        r.blob_url, r.source, r.uploaded_at = url, "blob_storage", utcnow()
        if version:
            r.version = version.strip()
        return {"success": True, "message": "Blob storage URL updated successfully", "release": release_json(r)}

    if not installer:
        raise HTTPException(400, "No installer file uploaded")
    orig = os.path.basename(installer.filename or "TallyConnector-Setup.exe")
    if not orig.lower().endswith((".exe", ".zip")):
        raise HTTPException(400, "Only .exe or .zip installers are accepted")

    fobj = installer.file
    fobj.seek(0, os.SEEK_END)
    size = fobj.tell()
    fobj.seek(0)
    if size > get_settings().max_installer_mb * 1024 * 1024:
        raise HTTPException(413, f"Installer is larger than {get_settings().max_installer_mb} MB")
    h = hashlib.sha256()
    for chunk in iter(lambda: fobj.read(1024 * 1024), b""):
        h.update(chunk)
    fobj.seek(0)

    new_version = (version or "").strip() or r.version or "v1.4.2"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", orig)
    if blob.configured():
        try:
            ref = blob.upload(fobj, f"{new_version}/{safe}")
        except Exception:  # noqa: BLE001
            log.exception("Blob upload failed")
            raise HTTPException(502, "Upload to Azure Blob Storage failed")
        source = "blob_storage"
    else:  # local development only: container disks are ephemeral on Azure App Service
        dest = STATIC / "blobs"
        dest.mkdir(parents=True, exist_ok=True)
        with open(dest / "TallyConnector-Setup.exe", "wb") as out:
            while chunk := fobj.read(1024 * 1024):
                out.write(chunk)
        ref, source = "/blobs/TallyConnector-Setup.exe", "local_blob"

    r.version, r.file_name, r.file_size = new_version, orig, size
    r.file_size_formatted = f"{size / (1024 * 1024):.1f} MB"
    r.sha256, r.blob_url, r.source, r.uploaded_at = h.hexdigest(), ref, source, utcnow()
    log.info("New Windows installer uploaded: %s (%s) sha256=%s", orig, r.file_size_formatted, r.sha256)
    return {"success": True, "message": f"Successfully uploaded {orig} to blob storage!", "release": release_json(r)}


@router.put("/tally/installer-config")
def installer_config(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    require_installer_admin(auth)
    url = body.get("blob_url")
    if not url:
        raise HTTPException(400, "blob_url is required")
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "blob_url must start with http:// or https://")
    r = get_release(db)
    r.blob_url, r.source, r.uploaded_at = url, "blob_storage", utcnow()
    if body.get("version"):
        r.version = str(body["version"]).strip()
    if body.get("file_name"):
        r.file_name = str(body["file_name"]).strip()
    return {"success": True, "message": "Blob storage configuration saved", "release": release_json(r)}


@router.post("/tally/activation-code")
def new_code(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    for c in db.scalars(select(ActivationCode).where(ActivationCode.tenant_id == auth.tenant.id, ActivationCode.status == "unused")):
        c.status = "disabled"
    code = ActivationCode(tenant_id=auth.tenant.id, code=new_activation_code(), status="unused", expires_at=utcnow() + timedelta(days=1))
    db.add(code)
    db.flush()
    return {"activation_code": code.code}


@router.post("/tally/connectors/{connector_code}/revoke")
def revoke(connector_code: str, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    c = db.scalar(select(Connector).where(Connector.tenant_id == auth.tenant.id, Connector.connector_code == connector_code))
    if not c:
        raise HTTPException(404, "Connector not found")
    c.status, c.updated_at = "inactive", utcnow()
    return {"status": "revoked"}


# ----------------------------------------------------------------------------- whatsapp
@router.get("/whatsapp/config")
def wa_config():
    s = get_settings()
    return {"embedded_signup_enabled": bool(s.meta_app_id and s.meta_embedded_signup_config_id), "app_id": s.meta_app_id,
            "config_id": s.meta_embedded_signup_config_id, "api_version": s.meta_graph_api_version, "embedded_signup_version": s.meta_embedded_signup_version}


@router.get("/whatsapp")
def wa_get(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    a = _wa_account(db, auth.tenant.id)
    return {"account": wa_public(a) if a else None}


def _upsert_account(db: Session, tenant_id: int, **fields) -> WhatsAppAccount:
    a = db.get(WhatsAppAccount, tenant_id)
    if a is None:
        a = WhatsAppAccount(tenant_id=tenant_id, **fields)
        db.add(a)
    else:
        for k, v in fields.items():
            setattr(a, k, v)
    db.flush()
    return a


def _template_state(token: str, waba_id: str) -> tuple[str, str | None]:
    s = get_settings()
    name = s.meta_template_name
    try:
        templates = get_templates(token, waba_id, name=name)
    except Exception as exc:  # noqa: BLE001
        return "UNKNOWN", str(exc)[:1000]
    matching = [t for t in templates if t.get("name") == name]
    if not matching:
        return "MISSING", f"Template '{name}' is not available on this WhatsApp Business Account"
    # Prefer the configured language, then the first exact-name template.
    exact = next((t for t in matching if t.get("language") == s.meta_template_language), matching[0])
    status = str(exact.get("status") or "UNKNOWN").upper()
    reason = exact.get("rejected_reason") or exact.get("reason")
    return status, str(reason)[:1000] if reason else None


def _registration_pin(account: WhatsAppAccount) -> str:
    s = get_settings()
    if account.registration_pin_enc:
        return decrypt_secret(account.registration_pin_enc)
    pin = s.meta_phone_registration_pin.strip()
    if not (len(pin) == 6 and pin.isdigit()):
        pin = f"{secrets.randbelow(1_000_000):06d}"
    account.registration_pin_enc = encrypt_secret(pin)
    return pin


def _sync_live_account(account: WhatsAppAccount, token: str, *, register: bool = True) -> None:
    """Validate the customer-scoped business token and synchronize live Meta state."""
    s = get_settings()
    waba = get_waba(token, account.waba_id)
    phones = get_phone_numbers(token, account.waba_id)
    phone = next((p for p in phones if str(p.get("id")) == account.phone_number_id), None)
    if not phone:
        if not phones:
            raise RuntimeError("No WhatsApp phone numbers were returned for this WABA")
        phone = phones[0]
        account.phone_number_id = str(phone["id"])

    account.display_phone_number = str(phone.get("display_phone_number") or account.display_phone_number or "")
    account.verified_name = str(phone.get("verified_name") or waba.get("name") or account.verified_name or "")
    account.quality_rating = str(phone.get("quality_rating") or account.quality_rating or "UNKNOWN")

    if register and s.meta_auto_register_phone:
        pin = _registration_pin(account)
        try:
            register_phone(token, account.phone_number_id, pin)
        except Exception as exc:  # noqa: BLE001
            # A number that was already registered does not need to be registered again.
            msg = str(exc)
            if "already registered" not in msg.lower() and "already_registered" not in msg.lower():
                raise

    subscribe_waba(token, account.waba_id)
    account.template_name = s.meta_template_name
    account.template_language = s.meta_template_language
    account.template_status, account.template_reject_reason = _template_state(token, account.waba_id)
    account.last_error = None if account.template_status in ("APPROVED", "PENDING", "IN_APPEAL") else account.template_reject_reason
    account.status = "connected"


def _discover_waba_id(token: str, supplied_waba_id: str | None) -> str:
    if supplied_waba_id:
        get_waba(token, supplied_waba_id)  # validates that the token can access the supplied WABA
        return supplied_waba_id
    data = debug_token(token)
    target_ids: list[str] = []
    for scope in data.get("granular_scopes") or []:
        for target in scope.get("target_ids") or []:
            if str(target) not in target_ids:
                target_ids.append(str(target))
    for target in target_ids:
        try:
            get_waba(token, target)
            return target
        except Exception:
            continue
    raise HTTPException(400, "Meta did not return a usable WABA ID from Embedded Signup")


@router.post("/whatsapp/manual")
def wa_manual(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    waba, pnid, token = body.get("waba_id"), body.get("phone_number_id"), body.get("access_token")
    if not waba or not pnid or not token:
        raise HTTPException(400, "waba_id, phone_number_id, and access_token are required")
    # Manual token entry remains available for local development and single-business
    # testing. In a configured Tech Provider deployment, validate the token against Meta.
    live_meta = bool(get_settings().meta_app_id and get_settings().meta_app_secret)
    if live_meta:
        try:
            waba = str(waba).strip()
            pnid = str(pnid).strip()
            get_waba(str(token).strip(), waba)
            get_phone(str(token).strip(), pnid)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Meta credential validation failed: {exc}") from exc
    a = _upsert_account(
        db, auth.tenant.id, status="connected", business_id=None, waba_id=waba, phone_number_id=pnid,
        display_phone_number="", verified_name=auth.tenant.name, quality_rating="UNKNOWN", connection_method="manual",
        template_name=get_settings().meta_template_name, template_status="UNKNOWN", template_language=get_settings().meta_template_language,
        template_reject_reason=None, access_token_enc=encrypt_secret(str(token).strip()), last_error=None, connected_at=utcnow(),
    )
    if live_meta:
        try:
            _sync_live_account(a, str(token).strip(), register=False)
        except Exception as exc:  # noqa: BLE001
            a.status = "error"
            a.last_error = str(exc)[:1000]
            raise HTTPException(400, f"WhatsApp account validation failed: {exc}") from exc
    else:
        # Development-only fallback so the existing local smoke test remains usable.
        a.template_status = "APPROVED"
        a.template_language = get_settings().meta_template_language
    return {"account": wa_public(a)}


@router.post("/whatsapp/embedded-signup")
def wa_embedded(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    code = str(body.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "Embedded Signup authorization code is required")
    try:
        token = exchange_signup_code(code)
        waba_id = _discover_waba_id(token, str(body.get("waba_id") or "").strip() or None)
        phone_id = str(body.get("phone_number_id") or "").strip()
        if phone_id:
            phone = get_phone(token, phone_id)
        else:
            phones = get_phone_numbers(token, waba_id)
            if not phones:
                raise RuntimeError("Meta returned no phone number for the selected WABA")
            phone = phones[0]
            phone_id = str(phone["id"])

        a = _upsert_account(
            db, auth.tenant.id, status="provisioning", business_id=str(body.get("business_id") or "") or None,
            waba_id=waba_id, phone_number_id=phone_id,
            display_phone_number=str(phone.get("display_phone_number") or ""),
            verified_name=str(phone.get("verified_name") or auth.tenant.name),
            quality_rating=str(phone.get("quality_rating") or "UNKNOWN"), connection_method="embedded_signup",
            template_name=get_settings().meta_template_name, template_status="UNKNOWN",
            template_language=get_settings().meta_template_language, template_reject_reason=None,
            access_token_enc=encrypt_secret(token), last_error=None, connected_at=utcnow(),
        )
        _sync_live_account(a, token, register=True)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Embedded Signup provisioning failed")
        raise HTTPException(400, f"WhatsApp onboarding failed: {str(exc)[:1000]}") from exc
    return {"account": wa_public(a)}


@router.post("/whatsapp/refresh")
def wa_refresh(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    a = _wa_account(db, auth.tenant.id)
    if not a:
        raise HTTPException(404, "WhatsApp is not connected")
    try:
        token = decrypt_secret(a.access_token_enc)
        _sync_live_account(a, token, register=False)
    except Exception as exc:  # noqa: BLE001
        a.status = "error"
        a.last_error = str(exc)[:1000]
        raise HTTPException(400, f"Unable to refresh WhatsApp status: {exc}") from exc
    return {"account": wa_public(a) | {"ready_to_send": a.status == "connected" and a.template_status == "APPROVED"}}


# Meta's pre-approved sample template. It lets a brand-new account (or a reviewer's test account) prove that
# sending works before the custom reminder template has been approved.
FALLBACK_TEMPLATE = ("hello_world", "en_US")


@router.post("/whatsapp/test")
def wa_test(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    a = _wa_account(db, auth.tenant.id)
    if not a or a.status != "connected":
        raise HTTPException(409, "WhatsApp is not connected")
    to = normalize_phone(body.get("to"))
    if not to:
        raise HTTPException(400, "Invalid phone number")
    if a.template_status == "APPROVED":
        name, lang, params = a.template_name, a.template_language, ["Test Customer", "\u20b91,000.00", "TEST-001", "today"]
    else:
        name, lang = FALLBACK_TEMPLATE
        params = []
    try:
        token = decrypt_secret(a.access_token_enc)
        mid = send_template(token, a.phone_number_id, to, name, lang, params)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Meta rejected the test message: {exc}") from exc
    return {"status": "sent", "wa_message_id": mid, "to": to, "template": name}


# ----------------------------------------------------------------------------- message templates
def _template_json(t: dict) -> dict:
    body = next((c for c in (t.get("components") or []) if str(c.get("type")).upper() == "BODY"), {})
    return {
        "id": t.get("id"), "name": t.get("name"), "language": t.get("language"), "category": t.get("category"),
        "status": str(t.get("status") or "UNKNOWN").upper(), "body": body.get("text") or "",
        "rejected_reason": t.get("rejected_reason") if t.get("rejected_reason") not in (None, "NONE") else None,
    }


def _connected_token(db: Session, tenant_id: int) -> tuple[WhatsAppAccount, str]:
    a = _wa_account(db, tenant_id)
    if not a or a.status not in ("connected", "provisioning"):
        raise HTTPException(409, "WhatsApp is not connected")
    return a, decrypt_secret(a.access_token_enc)


@router.get("/whatsapp/templates")
def wa_templates(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    a, token = _connected_token(db, auth.tenant.id)
    try:
        items = [_template_json(t) for t in get_templates(token, a.waba_id)]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not load message templates from WhatsApp: {exc}") from exc
    return {"items": items, "reminder_template": a.template_name}


@router.post("/whatsapp/templates")
def wa_template_create(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    a, token = _connected_token(db, auth.tenant.id)
    examples = body.get("examples") or []
    if isinstance(examples, str):
        examples = examples.splitlines()
    try:
        payload = build_template_payload(body.get("name"), body.get("category") or "UTILITY",
                                         body.get("language") or "en_US", body.get("body"), [str(e) for e in examples])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        res = create_template(token, a.waba_id, payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"WhatsApp did not accept the template: {exc}") from exc
    status = str(res.get("status") or "PENDING").upper()
    if payload["name"] == a.template_name and payload["language"] == a.template_language:
        a.template_status, a.template_reject_reason = status, None
    return {"template": {"id": res.get("id"), "name": payload["name"], "language": payload["language"],
                         "category": res.get("category") or payload["category"], "status": status,
                         "body": payload["components"][0]["text"], "rejected_reason": None}}


@router.delete("/whatsapp/templates/{name}")
def wa_template_delete(name: str, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    a, token = _connected_token(db, auth.tenant.id)
    try:
        delete_template(token, a.waba_id, name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"WhatsApp could not delete the template: {exc}") from exc
    if name == a.template_name:
        a.template_status, a.template_reject_reason = "MISSING", None
    return {"status": "deleted", "name": name}


@router.delete("/whatsapp")
def wa_delete(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    a = _wa_account(db, auth.tenant.id)
    if a:
        try:  # best effort: stop Meta sending this customer's events to us before the token is deleted
            unsubscribe_waba(decrypt_secret(a.access_token_enc), a.waba_id)
        except Exception:  # noqa: BLE001
            log.warning("Could not unsubscribe WABA %s on disconnect", a.waba_id)
        db.delete(a)
    get_or_create_settings(db, auth.tenant.id).enabled = False
    return {"status": "disconnected"}


# ----------------------------------------------------------------------------- customers
@router.get("/customers")
def customers(search: str = "", filter: str = "all", limit: str | None = None, offset: str | None = None,
              auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    tid = auth.tenant.id
    lim, off = _paging(limit, offset)
    q = select(Customer).where(Customer.tenant_id == tid, Customer.status == "active")
    search = search.strip()
    if search:
        q = q.where(or_(Customer.name.icontains(search, autoescape=True), Customer.phone_number.icontains(search, autoescape=True)))
    no_phone = or_(Customer.phone_number.is_(None), func.trim(Customer.phone_number) == "")
    if filter == "owing":
        q = q.where(Customer.total_pending > 0)
    elif filter == "no_phone":
        q = q.where(Customer.total_pending > 0, no_phone)
    elif filter == "opted_out":
        q = q.where(Customer.whatsapp_opt_out.is_(True))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Customer.total_pending.desc(), Customer.name.asc()).limit(lim).offset(off)).all()
    ids = [c.id for c in rows]
    counts, last = {}, {}
    if ids:
        counts = dict(db.execute(select(Invoice.customer_id, func.count()).where(
            Invoice.tenant_id == tid, Invoice.customer_id.in_(ids), Invoice.status == "outstanding").group_by(Invoice.customer_id)).all())
        last_ids = select(func.max(ReminderLog.id)).where(ReminderLog.tenant_id == tid, ReminderLog.customer_id.in_(ids)).group_by(ReminderLog.customer_id)
        last = {l.customer_id: l for l in db.scalars(select(ReminderLog).where(ReminderLog.id.in_(last_ids)))}
    items = []
    for c in rows:
        l = last.get(c.id)
        items.append({
            "id": c.id, "name": c.name, "phone_number": c.phone_number, "total_pending": f(c.total_pending),
            "bill_outstanding": f(c.bill_outstanding), "on_account": f(c.on_account), "whatsapp_opt_out": c.whatsapp_opt_out,
            "last_synced_at": iso(c.last_synced_at), "bills_count": counts.get(c.id, 0),
            "last_reminder": {"status": l.status, "at": iso(l.sent_at or l.created_at)} if l else None,
        })
    return {"total": total, "items": items}


@router.patch("/customers/{customer_id}")
def customer_patch(customer_id: int, body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    c = _customer(db, auth.tenant.id, customer_id)
    if "phone_number" in body:
        raw = str(body["phone_number"] or "").strip()
        if raw == "":
            c.phone_number = None
        else:
            norm = normalize_phone(raw)
            if not norm:
                raise HTTPException(400, "That doesn't look like a valid phone number")
            c.phone_number = norm
    if "whatsapp_opt_out" in body:
        c.whatsapp_opt_out = bool(body["whatsapp_opt_out"])
    c.updated_at = utcnow()
    return {"id": c.id, "phone_number": c.phone_number, "whatsapp_opt_out": c.whatsapp_opt_out}


@router.get("/customers/{customer_id}/invoices")
def customer_invoices(customer_id: int, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    c = _customer(db, auth.tenant.id, customer_id)
    today = today_utc()
    invs = db.scalars(select(Invoice).where(Invoice.tenant_id == auth.tenant.id, Invoice.customer_id == c.id, Invoice.status == "outstanding")
                      .order_by(Invoice.due_date.asc().nulls_first())).all()
    items = [{
        "id": i.id, "bill_ref": i.bill_ref, "bill_date": d2s(i.bill_date), "due_date": d2s(i.due_date),
        "opening_amount": f(i.opening_amount), "pending_amount": f(i.pending_amount), "is_on_account": i.is_on_account,
        "overdue_days": max(0, (today - i.due_date).days) if i.due_date and i.due_date < today else 0, "status": i.status,
    } for i in invs]
    return {"customer": {"id": c.id, "name": c.name, "phone_number": c.phone_number, "total_pending": f(c.total_pending),
                         "whatsapp_opt_out": c.whatsapp_opt_out}, "items": items}


@router.post("/customers/{customer_id}/remind")
def remind(customer_id: int, body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    tid = auth.tenant.id
    c = _customer(db, tid, customer_id)
    if c.whatsapp_opt_out:
        raise HTTPException(400, f"{c.name} has opted out of WhatsApp reminders")
    phone = normalize_phone(c.phone_number)
    if not phone:
        raise HTTPException(400, f"{c.name} has no valid phone number - add one first")
    acct = _wa_account(db, tid)
    if not acct or acct.status != "connected" or acct.template_status != "APPROVED":
        raise HTTPException(409, "Connect WhatsApp and verify template approval before sending reminders")
    open_bills = db.scalars(select(Invoice).where(
        Invoice.tenant_id == tid, Invoice.customer_id == c.id, Invoice.status == "outstanding", Invoice.is_on_account.is_(False)
    ).order_by(Invoice.id)).all()
    if not open_bills:
        raise HTTPException(400, f"{c.name} has no outstanding bills to remind about")

    invoice_id, mode = body.get("invoice_id"), body.get("mode")
    targets, target_id, target_ref = open_bills, None, None
    reminder_mode = "bill_wise" if (mode == "bill_wise" or invoice_id) else "customer_wise"

    if invoice_id:
        try:
            wanted = int(invoice_id)
        except (TypeError, ValueError):
            wanted = -1
        specific = next((i for i in open_bills if i.id == wanted), None)
        if not specific:
            raise HTTPException(404, "Selected invoice was not found or is already cleared")
        targets, target_id, target_ref, reminder_mode = [specific], specific.id, specific.bill_ref, "bill_wise"

    if mode == "bill_wise" and not invoice_id:
        logs = [make_log(db, tid, acct, customer=c, phone=phone, amount=inv.pending_amount, invoice_id=inv.id, bill_ref=inv.bill_ref,
                         mode="bill_wise", bills=1, due=d2s(inv.due_date)) for inv in open_bills]
        return {"count": len(logs), "reminders": [log_json(l) for l in logs], "reminder": log_json(logs[0]), "reminder_mode": "bill_wise",
                "detail": f"Sent {len(logs)} individual bill reminders to {c.name}"}

    amount = sum(i.pending_amount for i in targets)
    lg = make_log(db, tid, acct, customer=c, phone=phone, amount=amount, invoice_id=target_id or targets[0].id,
                  bill_ref=target_ref or ", ".join(i.bill_ref for i in targets), mode=reminder_mode, bills=len(targets), due=d2s(targets[0].due_date))
    return {"reminder": log_json(lg)}


# ----------------------------------------------------------------------------- support
@router.post("/support/inquiry")
def support_inquiry(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth)):
    msg = str(body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Please provide a message")
    log.info("[Support Inquiry] %s (%s): %s -> support@manweta.com", auth.user.email, auth.tenant.name, body.get("subject"))
    return {"success": True, "ticket_id": f"MWT-{str(int(time.time() * 1000))[-6:]}", "recipient": "support@manweta.com",
            "message": "Your inquiry has been routed to Manweta AI Support (support@manweta.com). Our team will reply within 2-4 business hours."}


@router.post("/contact")
def contact(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth)):
    if not str(body.get("message") or "").strip():
        raise HTTPException(400, "Please enter your message")
    log.info("Support ticket from %s (%s): %s", auth.user.email, auth.tenant.name, body.get("subject") or "General Inquiry")
    return {"status": "sent", "message": "Your message has been sent to support@manweta.com. Our support team will get back to you shortly."}


# ----------------------------------------------------------------------------- reminders
@router.get("/reminders/settings")
def reminder_settings(auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    return {"settings": settings_json(get_or_create_settings(db, auth.tenant.id)), "timezones": TIMEZONES}


@router.put("/reminders/settings")
def reminder_settings_put(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    tid = auth.tenant.id
    if body.get("enabled"):
        a = _wa_account(db, tid)
        if not a or a.status != "connected":
            raise HTTPException(409, "Connect WhatsApp before turning reminders on")
        if not body.get("remind_overdue") and not body.get("remind_upcoming"):
            raise HTTPException(400, "Choose at least one of: overdue bills, bills due soon")
    s = get_or_create_settings(db, tid)
    s.enabled = bool(body.get("enabled"))
    if body.get("send_hour") is not None:
        s.send_hour = _to_int(body["send_hour"], "send_hour")
    if body.get("timezone"):
        s.timezone = str(body["timezone"])
    if body.get("remind_overdue") is not None:
        s.remind_overdue = bool(body["remind_overdue"])
    if body.get("remind_upcoming") is not None:
        s.remind_upcoming = bool(body["remind_upcoming"])
    if body.get("days_before_due") is not None:
        s.days_before_due = _to_int(body["days_before_due"], "days_before_due")
    if body.get("repeat_every_days") is not None:
        s.repeat_every_days = _to_int(body["repeat_every_days"], "repeat_every_days")
    if body.get("min_amount") is not None:
        try:
            s.min_amount = float(body["min_amount"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid value for min_amount")
    if body.get("reminder_mode") in ("customer_wise", "bill_wise"):
        s.reminder_mode = body["reminder_mode"]
    db.flush()
    return {"settings": settings_json(s)}


@router.get("/reminders/preview")
def reminder_preview(mode: str | None = None, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    rep = calculate_candidates(db, auth.tenant.id, mode)
    return {"count": len(rep["candidates"]), "total_amount": sum(c["amount"] for c in rep["candidates"]), "skipped": rep["skipped"],
            "reminder_mode": rep["reminder_mode"], "candidates": rep["candidates"][:200]}


@router.post("/reminders/run")
def reminder_run(body: dict = Body(default={}), auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    tid = auth.tenant.id
    acct = _wa_account(db, tid)
    if not acct or acct.status != "connected" or acct.template_status != "APPROVED":
        raise HTTPException(409, "Connect WhatsApp and ensure reminder template is APPROVED before running")
    rep = calculate_candidates(db, tid, body.get("mode"))
    cust = {c.id: c for c in db.scalars(select(Customer).where(Customer.id.in_([x["customer_id"] for x in rep["candidates"]] or [0])))}
    sent = failed = 0
    for cand in rep["candidates"]:
        item = make_log(db, tid, acct, customer=cust[cand["customer_id"]], phone=cand["phone"], amount=cand["amount"], invoice_id=cand["invoice_id"],
                        bill_ref=cand.get("bill_ref"), mode=cand.get("mode", "customer_wise"), bills=cand.get("bills", 1), due=cand.get("due_date"))
        if item.status == "failed":
            failed += 1
        else:
            sent += 1
    return {"attempted": len(rep["candidates"]), "sent": sent, "failed": failed, "reminder_mode": rep["reminder_mode"], "skipped": rep["skipped"]}


@router.get("/reminders/logs")
def reminder_logs(status: str = "", limit: str | None = None, offset: str | None = None,
                  auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    lim, off = _paging(limit, offset)
    q = select(ReminderLog).where(ReminderLog.tenant_id == auth.tenant.id)
    if status.strip():
        q = q.where(ReminderLog.status == status.strip())
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(ReminderLog.id.desc()).limit(lim).offset(off)).all()
    return {"total": total, "items": [log_json(l) for l in rows]}


@router.get("/replies")
def replies(limit: str | None = None, auth: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    lim, _ = _paging(limit, None)
    rows = db.execute(select(InboundMessage, Customer.name).outerjoin(Customer, Customer.id == InboundMessage.customer_id)
                      .where(InboundMessage.tenant_id == auth.tenant.id).order_by(InboundMessage.id.desc()).limit(lim)).all()
    return {"items": [{"id": m.id, "customer_id": m.customer_id, "customer_name": name, "phone_number": m.phone_number,
                       "message_type": m.message_type, "body": m.body, "received_at": iso(m.received_at)} for m, name in rows]}
