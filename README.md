# Manweta AI - FastAPI + Neon + Azure

Same product, same UI, new backend. The dashboard (`static/`) is the original `public/assets` **byte-for-byte**;
only `server.ts` (Express, in-memory) was replaced by FastAPI + Neon Postgres + Azure Blob.

```
app/            FastAPI (routers/auth|portal|connector|webhook, models, blob, reminders, seed)
static/         original UI (index.html, assets/app.js, styles.css, logos)
Dockerfile      python:3.12-slim + gunicorn/uvicorn on :8000
deploy/azure-deploy.sh, .github/workflows/deploy-azure.yml
tests/test_smoke.py
```

## Run locally
```bash
cp .env.example .env            # set ENCRYPTION_KEY (command inside the file)
docker compose up --build       # app :8000 + a throw-away Postgres
# or: pip install -r requirements.txt && DATABASE_URL=postgresql://... uvicorn app.main:app --reload
```
Log in with the demo account `demo@manweta.ai` / `password123` (seeded when `SEED_DEMO_DATA=true`).
Tables are created on startup. Tests: `DATABASE_URL=postgresql://... python -m pytest tests -q`.

## Neon
Create a project, copy the **pooled** connection string (host contains `-pooler`) into `DATABASE_URL`
(`?sslmode=require` included). The app forces the psycopg3 driver, disables server-side prepared statements for PgBouncer,
and uses `pool_pre_ping` so Neon's scale-to-zero wake-up doesn't surface as errors.

## Deploy to Azure
```bash
az login
export DATABASE_URL='postgresql://...neon.tech/neondb?sslmode=require'
./deploy/azure-deploy.sh
```
Creates: resource group, Container Registry (image built in Azure with `az acr build`), private Storage container
`installers`, Linux App Service plan (B1) + Web App for Containers, system-assigned identity with `AcrPull`,
`Storage Blob Data Contributor` and `Storage Blob Delegator` (so no keys or SAS secrets live in app settings),
Always On, health check on `/health`. Secrets are written once to `deploy/.secrets.env` (git-ignored) - **back it up**;
losing `ENCRYPTION_KEY` means every client must reconnect WhatsApp.
Then point Meta's webhook at `https://<app>.azurewebsites.net/api/v1/whatsapp/webhook`.
For CI/CD set repo variables `ACR_NAME`, `WEBAPP_NAME` and secrets `AZURE_CLIENT_ID/TENANT_ID/SUBSCRIPTION_ID` (OIDC).

## What stays identical
Every endpoint, request/response shape and error message the UI uses; demo data; reminder rules (overdue / due-soon,
repeat gap, minimum amount, customer-wise vs bill-wise); STOP/START opt-out; activation code -> API key flow;
installer download/upload/config screens.

## What changed on purpose
| Area | Before | Now |
|---|---|---|
| Storage | in-memory (lost on restart) | Neon Postgres |
| Sessions | in-memory | `sessions` table (token stored hashed) |
| Passwords | SHA-256 + fixed salt | argon2 |
| WhatsApp token | plaintext | Fernet-encrypted (`ENCRYPTION_KEY`); never returned by the API |
| Installer | local disk (`public/blobs`) | Azure Blob, private, served via 15-min read-only SAS redirect |
| Webhook | routed by "first tenant" fallback | routed by `phone_number_id`; unattributable messages dropped; out-of-order statuses can't downgrade `read`; signature enforced when `META_APP_SECRET` is set |

## Known gaps carried over from the original (decide before real customers)
1. **Password reset is open.** The UI sends only email + new password, so anyone can reset any account. Kept for UI parity
   behind `INSECURE_PASSWORD_RESET=true` (logged as a warning at boot). Setting it to `false` disables the screen until an emailed-link flow is built.
2. **Any logged-in user can replace the Windows installer** that every tenant downloads. Set `INSTALLER_ADMIN_EMAILS` to lock it down.
3. **WhatsApp integration is live-mode capable**: Embedded Signup exchanges the one-time code for a customer-scoped business token, validates the WABA/phone, subscribes the WABA to webhooks, optionally registers the phone, reads template approval state, and sends approved templates through the Graph API. Delivery/read states come from the webhook.
4. **No scheduler** - "automatic reminders" settings are stored but nothing runs them daily (also true of the original).
5. **Sync never closes paid bills** - an invoice that disappears from Tally stays "outstanding" and keeps getting reminders.
6. Demo seed uses dates relative to first boot; set `SEED_DEMO_DATA=false` in production.

## Meta Tech Provider / Embedded Signup deployment

Set these Azure App Service application settings (do not put them in the frontend or commit them):

```text
META_APP_ID=<your Meta app id>
META_APP_SECRET=<your Meta app secret>
META_EMBEDDED_SIGNUP_CONFIG_ID=<your Facebook Login for Business / Embedded Signup v4 config id>
META_EMBEDDED_SIGNUP_VERSION=v4
META_GRAPH_API_VERSION=v25.0
META_WEBHOOK_VERIFY_TOKEN=<random secret>
META_TEMPLATE_NAME=manweta_payment_reminder
META_TEMPLATE_LANGUAGE=en_US
META_AUTO_REGISTER_PHONE=true
META_PHONE_REGISTRATION_PIN=
META_HTTP_TIMEOUT_SECONDS=20
ENCRYPTION_KEY=<stable Fernet key>
SEED_DEMO_DATA=false
INSECURE_PASSWORD_RESET=false
```

The exact Graph API version should be kept aligned with the version currently supported by the Meta app. The code exposes it to the browser so the Facebook JavaScript SDK and server-side Graph calls use the same configured version.

### Required Meta configuration

1. Complete Business Verification for the provider business.
2. Use a Business-type Meta app and configure WhatsApp Business Platform.
3. Create/use the current Embedded Signup v4 configuration. The v4 flow should select the WhatsApp Cloud API assets and the permissions required by this product.
4. Add the Azure/custom domain to the Facebook Login / Embedded Signup allowed domains and use HTTPS.
5. Configure the WhatsApp Webhooks product with:
   `https://<your-domain>/api/v1/whatsapp/webhook`
   and the same `META_WEBHOOK_VERIFY_TOKEN`.
6. Request Advanced Access/App Review for the WhatsApp permissions required by the app. For this product the important permissions are `whatsapp_business_management` and `whatsapp_business_messaging`; Meta's current Embedded Signup documentation may also request `business_management` as part of the provider release checklist.
7. Create an approved customer-facing message template named `manweta_payment_reminder` in the test WABA used for App Review. The implementation sends four BODY text parameters in this order:
   `customer name`, `amount`, `bill reference`, `due date`.
8. Test Embedded Signup with a Meta sandbox/test business before switching the app to Live.

### What happens after a customer clicks Connect WhatsApp

```text
Browser
  -> Meta Embedded Signup v4
  -> authorization code + WA_EMBEDDED_SIGNUP postMessage
  -> POST /api/v1/portal/whatsapp/embedded-signup
  -> server exchanges code for customer business token
  -> validates WABA + phone
  -> registers phone (if configured)
  -> subscribes WABA to the app webhook
  -> reads template approval state
  -> stores encrypted customer business token
```

The access token is never returned to the browser. The customer's WABA remains owned by the customer; Manweta stores the scoped business token needed to operate the connection.

### Message lifecycle

`POST /{phone_number_id}/messages` returns the real Meta `wamid` and the dashboard records it as `sent`. Meta then updates that log through the webhook (`sent`, `delivered`, `read`, or `failed`). The application no longer invents delivery/read status in production.

### Production security

- Never commit `.env`, `deploy/.secrets.env`, Meta app secrets, access tokens, or the Fernet key.
- Keep `ENCRYPTION_KEY` stable across Azure deployments; changing it makes stored customer WhatsApp tokens undecryptable.
- Set `SEED_DEMO_DATA=false`.
- Set `INSECURE_PASSWORD_RESET=false` until a real email-token reset flow is enabled.
- Set `INSTALLER_ADMIN_EMAILS` to the small set of Manweta administrators who can replace the installer.

## Meta App Review (Step 2)
See `APP_REVIEW.md` for the app-settings checklist, the exact recording steps for both permissions and the use-case text.
New in this build: **WhatsApp -> Message templates** (create/list/delete via the Graph API), `hello_world` fallback for "Send test"
while the custom template is under review, plain-HTML `/privacy`, `/terms`, `/data-deletion` pages, and a mandatory webhook verify token.
`deploy/azure-deploy.sh` now generates (and keeps) `META_WEBHOOK_VERIFY_TOKEN` and defaults `INSECURE_PASSWORD_RESET` to `false`.
