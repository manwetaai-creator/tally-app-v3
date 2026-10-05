# Meta App Review (Tech Provider, Step 2) - submission kit

Permissions to request (Advanced Access): `whatsapp_business_management`, `whatsapp_business_messaging`.
One separate screen recording per permission. Recordings must be videos, not screenshots, and show the business-facing dashboard.

## App settings checklist ("Review your app settings")
| Field | Value |
|---|---|
| Privacy Policy URL | `https://<your-domain>/privacy` |
| Terms of Service URL | `https://<your-domain>/terms` |
| User data deletion | Data deletion instructions URL: `https://<your-domain>/data-deletion` |
| App icon / category | 1024x1024 icon (`static/assets/logo.png`), category "Business and Pages" |
| Webhook callback | `https://<your-domain>/api/v1/whatsapp/webhook`, verify token = `META_WEBHOOK_VERIFY_TOKEN` (printed by `deploy/azure-deploy.sh`) |
| Webhook fields | `messages`, `message_template_status_update`, `phone_number_quality_update`, `account_update` |
| Allowed domains / redirect | your HTTPS domain, in Facebook Login for Business settings |

## Before you record
1. Deploy, then sign up in the dashboard with a fresh account (set `SEED_DEMO_DATA=false`).
2. Connect WhatsApp with Embedded Signup using a Meta test business / test WABA.
3. Create a template named `manweta_payment_reminder` from **WhatsApp -> Message templates** and wait for APPROVED
   (until then, "Send test" uses Meta's built-in `hello_world`).
4. Have a real phone with WhatsApp next to you to show the message arriving.

## Video 1 - whatsapp_business_management (about 2 min)
1. Show the dashboard and click **WhatsApp -> Connect WhatsApp**; complete Embedded Signup (shows the permission being granted).
2. Show the connected number, verified name and quality rating (phone number management).
3. Scroll to **Message templates**, click **Create a new template**, submit it, and show it appear as PENDING then APPROVED (Refresh status).
4. Optionally show deleting a throw-away template.

Use-case text: *"Manweta AI is a Tech Provider. Our business customers connect their own WhatsApp Business Account through Embedded Signup. We use whatsapp_business_management to read the customer's WABA and phone numbers, register and subscribe the number to our webhook, and to create, list and delete the message templates (payment reminders) the customer uses, all from our dashboard on the customer's behalf."*

## Video 2 - whatsapp_business_messaging (about 2 min)
1. In the dashboard, open **WhatsApp -> Send a test message**, enter the phone number of the real phone, click **Send test**.
2. Switch to the phone and show the message arriving in WhatsApp.
3. Back in the dashboard, open **Reminders -> Logs** and show the status moving to delivered / read (from the webhook).
4. Reply STOP (or any text) from the phone and show it under **Replies**.

Use-case text: *"Manweta AI is a Tech Provider. We use whatsapp_business_messaging to send approved payment-reminder template messages from our customers' WhatsApp Business numbers to their own debtors, to receive delivery/read statuses and inbound replies (including STOP opt-outs) through our webhook, and to show them in the customer's dashboard."*

## Notes
- Review the legal pages in `static/pages/` and the deletion promises in `/data-deletion` before submitting; they must match what you actually do.
- Keep the Meta app in Development mode while recording with test accounts; switch to Live after approval.
