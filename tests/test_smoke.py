"""End-to-end smoke test against a real Postgres (a local container or a Neon branch).

    DATABASE_URL=postgresql://... python -m pytest tests -q
"""
import json
import uuid

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_health():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/").status_code == 200  # UI index.html


def test_demo_login_and_overview():
    with TestClient(app) as c:  # runs lifespan: creates tables + seeds demo data
        r = c.post("/api/v1/auth/login", json={"email": "demo@manweta.ai", "password": "password123"})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        assert c.get("/api/v1/auth/me", headers=auth(token)).json()["tenant"]["name"].startswith("ABC Traders")
        ov = c.get("/api/v1/portal/overview", headers=auth(token)).json()
        assert ov["receivables"]["open_bills"] == 8
        assert ov["onboarding_complete"] is True
        assert c.post("/api/v1/auth/login", json={"email": "demo@manweta.ai", "password": "wrong"}).status_code == 401
        assert c.get("/api/v1/portal/overview").status_code == 401


def test_full_flow_new_tenant():
    with TestClient(app) as c:
        email = f"t-{uuid.uuid4().hex[:10]}@example.com"
        s = c.post("/api/v1/auth/signup", json={"company_name": "Test Co", "email": email, "password": "supersecret1"})
        assert s.status_code == 200, s.text
        h = auth(s.json()["access_token"])

        code = c.get("/api/v1/portal/tally", headers=h).json()["activation_code"]
        act = c.post("/api/v1/connector/activate", json={"activation_code": code, "machine_name": "PC"})
        assert act.status_code == 200, act.text
        ch = auth(act.json()["api_key"])

        payload = {"customers": [{"name": "Acme", "mobile": "98765 43210", "total_pending": 1500, "bills": [
            {"bill_ref": "B1", "due_date": "2020-01-01", "pending_amount": 1500, "opening_amount": 1500}]}]}
        sy = c.post("/api/v1/connector/sync", json=payload, headers=ch)
        assert sy.status_code == 200 and sy.json()["records_count"] == 1
        assert c.post("/api/v1/connector/sync", json=payload, headers=ch).status_code == 200  # idempotent upsert

        items = c.get("/api/v1/portal/customers", headers=h).json()["items"]
        assert len(items) == 1 and items[0]["phone_number"] == "919876543210" and items[0]["bills_count"] == 1

        prev = c.get("/api/v1/portal/reminders/preview", headers=h).json()
        assert prev["count"] == 1

        assert c.post("/api/v1/portal/reminders/run", headers=h, json={}).status_code == 409  # WhatsApp not connected
        pnid = str(uuid.uuid4().int)[:15]  # unique per run: webhooks route by phone_number_id
        wa = c.post("/api/v1/portal/whatsapp/manual", headers=h, json={"waba_id": "1", "phone_number_id": pnid, "access_token": "tok"})
        assert wa.json()["account"]["ready_to_send"] is True and "access_token" not in json.dumps(wa.json())

        run = c.post("/api/v1/portal/reminders/run", headers=h, json={}).json()
        assert run["sent"] == 1
        assert c.get("/api/v1/portal/reminders/preview", headers=h).json()["skipped"]["recently_reminded"] == 1

        hook = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid},
                "messages": [{"from": "919876543210", "id": "wamid.x", "type": "text", "text": {"body": "STOP"}}]}}]}]}
        assert c.post("/api/v1/whatsapp/webhook", json=hook).status_code == 200
        assert c.get("/api/v1/portal/customers", headers=h).json()["items"][0]["whatsapp_opt_out"] is True
        assert c.get("/api/v1/portal/replies", headers=h).json()["items"][0]["body"] == "STOP"


# ---- App Review additions ---------------------------------------------------------------
def test_public_legal_pages_are_plain_html():
    for path in ("/privacy", "/terms", "/data-deletion"):
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "<h1>" in r.text and "support@manweta.com" in r.text


def test_webhook_verify_rejects_empty_and_wrong_token(monkeypatch):
    from app import config
    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", "")
    assert client.get("/api/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=1").status_code == 403
    get_settings.cache_clear()
    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", "s3cret")
    assert client.get("/api/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=1").status_code == 403
    ok = client.get("/api/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=s3cret&hub.challenge=42")
    assert ok.status_code == 200 and ok.text == "42"
    get_settings.cache_clear()


def test_template_payload_validation():
    import pytest
    from app.meta_whatsapp import build_template_payload
    ok = build_template_payload("manweta_payment_reminder", "utility", "en_US",
                                "Hello {{1}}, {{2}} is due on {{3}}. Reply STOP to opt out.", ["A", "B", "C"])
    assert ok["category"] == "UTILITY" and ok["components"][0]["example"]["body_text"] == [["A", "B", "C"]]
    for args in [
        ("Bad Name", "UTILITY", "en_US", "Hi", []),
        ("n", "UTILITY", "en_US", "Hi {{1}} {{3}} there", ["a", "b"]),     # gap in numbering
        ("n", "UTILITY", "en_US", "{{1}} starts with variable", ["a"]),
        ("n", "UTILITY", "en_US", "ends with variable {{1}}", ["a"]),
        ("n", "UTILITY", "en_US", "Hi {{1}} there", []),                    # missing sample
        ("n", "BOGUS", "en_US", "Hi", []),
    ]:
        with pytest.raises(ValueError):
            build_template_payload(*args)


def test_test_send_falls_back_to_hello_world_and_templates_api(monkeypatch):
    from app.db import SessionLocal
    from app.models import User, WhatsAppAccount
    from app.routers import portal

    sent = {}
    monkeypatch.setattr(portal, "send_template", lambda token, pnid, to, name, lang, params: sent.update(name=name, lang=lang, params=params) or "wamid.TEST")
    monkeypatch.setattr(portal, "get_templates", lambda token, waba, name=None: [
        {"id": "1", "name": "manweta_payment_reminder", "language": "en_US", "category": "UTILITY", "status": "approved",
         "components": [{"type": "BODY", "text": "Hello {{1}}"}]}])
    created = {}
    monkeypatch.setattr(portal, "create_template", lambda token, waba, payload: created.update(payload) or {"id": "9", "status": "PENDING", "category": "UTILITY"})
    deleted = []
    monkeypatch.setattr(portal, "delete_template", lambda token, waba, name: deleted.append(name))

    with TestClient(app) as c:
        email = f"wa-{uuid.uuid4().hex[:10]}@example.com"
        tok = c.post("/api/v1/auth/signup", json={"company_name": "WA Co", "email": email, "password": "supersecret1"}).json()["access_token"]
        h = auth(tok)
        assert c.post("/api/v1/portal/whatsapp/manual", json={"waba_id": "1", "phone_number_id": "2", "access_token": "tok"}, headers=h).status_code == 200

        with SessionLocal() as db:  # simulate "custom template still in review"
            tenant_id = db.query(User).filter(User.email == email).one().tenant_id
            a = db.get(WhatsAppAccount, tenant_id)
            a.template_status = "PENDING"
            db.commit()
        r = c.post("/api/v1/portal/whatsapp/test", json={"to": "9876543210"}, headers=h)
        assert r.status_code == 200 and r.json()["template"] == "hello_world" and sent["name"] == "hello_world" and sent["params"] == []

        lst = c.get("/api/v1/portal/whatsapp/templates", headers=h).json()
        assert lst["items"][0]["status"] == "APPROVED" and lst["items"][0]["body"] == "Hello {{1}}"

        bad = c.post("/api/v1/portal/whatsapp/templates", json={"name": "Bad Name", "body": "x"}, headers=h)
        assert bad.status_code == 400
        ok = c.post("/api/v1/portal/whatsapp/templates", headers=h, json={
            "name": "manweta_payment_reminder", "category": "UTILITY", "language": "en_US",
            "body": "Hello {{1}}, {{2}} is due. Reply STOP to opt out.", "examples": ["A", "B"]})
        assert ok.status_code == 200 and ok.json()["template"]["status"] == "PENDING" and created["components"][0]["type"] == "BODY"
        assert c.delete("/api/v1/portal/whatsapp/templates/manweta_payment_reminder", headers=h).status_code == 200 and deleted == ["manweta_payment_reminder"]


def test_restricted_whatsapp_demo_endpoint(monkeypatch):
    from app import main
    from types import SimpleNamespace

    from app.routers import demo_whatsapp

    monkeypatch.setattr(main, "settings", SimpleNamespace(meta_demo_recipient="+15550100"))
    demo_page = client.get("/whatsapp-demo")
    assert demo_page.status_code == 200 and "Send a WhatsApp Message" in demo_page.text
    assert "Demo Environment" in demo_page.text
    assert "+••••0100" in demo_page.text and "+15550100" not in demo_page.text
    assert demo_page.headers["cache-control"] == "no-store"
    assert "/assets/logo.png" in demo_page.text
    assert "Demo access key" not in demo_page.text
    assert client.get("/whatsapp-demo.html").status_code == 200

    settings = SimpleNamespace(
        meta_demo_access_token="meta-token",
        meta_demo_phone_number_id="123456",
        meta_demo_recipient="+15550100",
        meta_graph_api_version="v25.0",
        meta_http_timeout_seconds=20.0,
    )
    monkeypatch.setattr(demo_whatsapp, "get_settings", lambda: settings)
    monkeypatch.setattr(demo_whatsapp, "_last_send_at", 0.0)

    assert client.post(
        "/api/v1/demo/whatsapp/send",
        json={"recipient": "15550101"},
    ).status_code == 403
    assert client.post(
        "/api/v1/demo/whatsapp/send",
        json={"recipient": "call 15550100"},
    ).status_code == 403

    sent = {}

    class FakeResponse:
        is_success = True
        status_code = 200

        def json(self):
            return {"messages": [{"id": "wamid.demo"}]}

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 20.0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, url, headers, json):
            sent.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(demo_whatsapp.httpx, "Client", FakeClient)
    monkeypatch.setattr(demo_whatsapp, "_last_send_at", 0.0)
    response = client.post(
        "/api/v1/demo/whatsapp/send",
        json={"recipient": "+1 (555) 0100"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["message_id"] == "wamid.demo"
    assert response.json()["recipient_masked"] == "+••••0100"
    assert "15550100" not in response.text
    assert sent["headers"]["Authorization"] == "Bearer meta-token"
    assert sent["payload"]["to"] == "15550100"

    assert client.post(
        "/api/v1/demo/whatsapp/send",
        json={"recipient": "15550100"},
    ).status_code == 429

    settings.meta_demo_recipient = "recipient 15550100"
    assert client.post(
        "/api/v1/demo/whatsapp/send",
        json={"recipient": "15550100"},
    ).status_code == 503


def test_demo_meta_errors_are_safe(monkeypatch):
    from types import SimpleNamespace

    from app.routers import demo_whatsapp

    monkeypatch.setattr(demo_whatsapp, "_last_send_at", 0.0)
    monkeypatch.setattr(
        demo_whatsapp,
        "get_settings",
        lambda: SimpleNamespace(
            meta_demo_access_token="meta-token-secret",
            meta_demo_phone_number_id="phone-id-secret",
            meta_demo_recipient="15550100",
            meta_graph_api_version="v25.0",
            meta_http_timeout_seconds=20.0,
        ),
    )

    class FakeResponse:
        is_success = False
        status_code = 400

        def json(self):
            return {"error": {"message": "Rejected meta-token-secret phone-id-secret"}}

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, url, headers, json):
            assert headers["Authorization"] == "Bearer meta-token-secret"
            return FakeResponse()

    monkeypatch.setattr(demo_whatsapp.httpx, "Client", FakeClient)
    response = client.post(
        "/api/v1/demo/whatsapp/send",
        json={"recipient": "15550100"},
    )
    assert response.status_code == 400
    assert "[redacted]" in response.json()["detail"]
    assert "meta-token-secret" not in response.text
    assert "phone-id-secret" not in response.text
