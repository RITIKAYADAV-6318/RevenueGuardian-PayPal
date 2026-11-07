# Deploying the API to Render

The API is the only piece that must be public: PayPal sends webhooks to it. It uses `requirements-api.txt`
(five small packages), not the full `requirements.txt`.

## 1. Create the service

1. Push this repo (the branch you want to deploy) to GitHub.
2. In Render: **New → Blueprint**, connect GitHub, choose this repo and branch. Render reads `render.yaml`.
3. Render asks for the secrets marked `sync: false`. Enter:

| Variable | Value |
|---|---|
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | From your PayPal **sandbox** app (developer.paypal.com → Apps & Credentials → Sandbox) |
| `PAYPAL_WEBHOOK_ID` | Enter a placeholder now; replace it after step 3 (the ID only exists once the webhook is registered) |
| `DEMO_ADMIN_PASSWORD`, `DEMO_CFO_PASSWORD`, `DEMO_SALES_PASSWORD` | Passwords you choose; put them in the Devpost testing notes |

`JWT_SECRET_KEY` is generated automatically. `SECURE_MODE=true` means the app **refuses to start** if any secret or demo password is missing.

## 2. Check it is alive

Open `https://<your-service>.onrender.com/health`. Expect `"status": "ok"`, `"paypal_mode": "sandbox"` and
`"webhook_verification_configured": true` (false until the real webhook id is set).

## 3. Register the webhook in PayPal

1. developer.paypal.com → your sandbox app → **Webhooks → Add Webhook**.
2. URL: `https://<your-service>.onrender.com/webhooks/paypal`
3. Select events. At minimum the invoicing events (for example `INVOICING.INVOICE.PAID`); selecting all sandbox events is fine while testing.
4. Copy the **Webhook ID** PayPal shows and set it as `PAYPAL_WEBHOOK_ID` in Render (Environment tab). Render redeploys.

## 4. Test with a real event

Pay a sandbox invoice as the sandbox buyer. Then sign in (`POST /auth/login` with a demo user) and call
`GET /api/events` with the bearer token: the event should show `"provenance": "REAL_VERIFIED"`.
Events from PayPal's dashboard *simulator* usually fail signature verification; that is expected.

## Data persistence (important)

On Render's free plan the filesystem is wiped on every restart and redeploy. The database is recreated empty (demo CRM is
re-seeded automatically), but **approvals, audit history and webhook events are lost**. A paid-invoice webhook that arrives after a
restart then refers to a proposal that no longer exists.

To keep data, use a paid instance with a disk: in `render.yaml` change `plan: free` to a paid plan, add

```yaml
    disk:
      name: data
      mountPath: /var/data
      sizeGB: 1
```

and add the environment variable `DATABASE_URL=sqlite:////var/data/crm.db`. Check Render's current pricing first.

## Cold starts

Free services sleep after about 15 minutes without traffic and take roughly a minute to wake. PayPal retries webhooks that fail,
so events are not lost, but the first request after a sleep is slow. Before recording the video, open `/health` a few minutes
earlier. During judging (Dec 1-15), a free uptime monitor pinging `/health` every 10 minutes keeps it awake; check Render's
free-tier instance-hour limit first.

## After submission

Do not push to the deployed branch after Nov 12. Create a branch (for example `submission`) from the final commit, point Render
at it, and tag `submission-v1`.
