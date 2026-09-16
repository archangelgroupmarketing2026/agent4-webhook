# Agent 4 Webhook Receiver — Render.com Deploy Guide

**What this does:** exposes two HTTPS endpoints (`/webhooks/ghl/inbound-sms` and `/webhooks/retell/call-ended`) that receive webhook POSTs from GoHighLevel and Retell, classify replies, drive the reply router (State 1 / 2 / 3 or Human Review), move opportunity stages, stamp custom fields, and record call outcomes.

**Legal authorization on file:** `MONG-ACK-2026-09-11`

---

## Before you begin — 15-minute prep checklist

You need three things ready:

1. **A GitHub account** (free) with a new empty repo — call it `agent4-webhook`
2. **A Render.com account** (free to sign up; the service itself costs $7/mo — see cost section)
3. **A GHL Private Integration Token (PIT)** for the AI Autopilot SMC location. See "Generating the GHL PIT" below.

---

## Choosing a Render plan

**Do not use Render's Free tier for this service.** Free plans spin down after 15 minutes of no traffic. When a lead texts back later, the first cold request takes ~60 seconds to wake the service back up. GHL will time out waiting, and the reply router will never fire.

| Plan        | Cost          | Idle behavior             | Fits this workload? |
| ----------- | ------------- | ------------------------- | ------------------- |
| Free        | $0            | Sleeps after 15 min idle  | No                  |
| **Starter** | **$7/mo**     | **Always on**             | **Yes** — pick this |
| Standard    | $25/mo        | Always on + 2GB RAM       | Overkill for now    |

Starter is enough headroom for tens of thousands of inbound webhooks per month. Only upgrade if you outgrow it.

---

## Generating the GHL Private Integration Token (PIT)

GHL's legacy v1 API keys are deprecated. PIT is the current path.

1. In GHL, switch into the **AI Autopilot SMC** sub-account (Location ID `l1d7nflxbC8vLMR1KuMa`)
2. Go to **Settings → Business Profile → Private Integrations** (may also be labeled just "Private Integrations")
3. Click **Create New Integration**
4. Name: `Agent 4 Webhook Receiver`. Description: `Reply router + Retell call sync`
5. **Select these scopes** (least privilege — do not tick everything):
   - `contacts.readonly` — to look up contacts by ID
   - `contacts.write` — to update custom fields and add notes
   - `conversations/message.write` — to send auto-reply SMS
   - `opportunities.readonly` — to search opportunities by contact
   - `opportunities.write` — to move stages
6. Click **Create**
7. **COPY THE TOKEN IMMEDIATELY** into a password manager. GHL will not show it again in full.
8. **Rotate every 90 days.** GHL gives you a 7-day grace period where both the old and new tokens work.

**Docs:** https://help.gohighlevel.com/support/solutions/articles/155000003054-private-integrations-everything-you-need-to-know

---

## Deploy in 6 steps

### 1. Push this project to GitHub

From this project directory:

```bash
git init
git add .
git commit -m "Initial Agent 4 webhook receiver"
git branch -M main
git remote add origin git@github.com:YOUR_GITHUB/agent4-webhook.git
git push -u origin main
```

If you prefer HTTPS instead of SSH, use `https://github.com/YOUR_GITHUB/agent4-webhook.git`.

### 2. Create the Render service

1. Log in at https://dashboard.render.com
2. Click **New +** → **Blueprint**
3. Connect your GitHub account if not already connected
4. Select the `agent4-webhook` repo
5. Render reads `render.yaml`, shows you the service plan. Click **Apply**.

### 3. Set the two secret environment variables

Render will prompt for `GHL_PIT` and `GHL_LOCATION_ID` because they are marked `sync: false`. If it doesn't, go to the service → **Environment** tab and add them:

| Key               | Value                              |
| ----------------- | ---------------------------------- |
| `GHL_PIT`         | *(paste your PIT from step 7 above)* |
| `GHL_LOCATION_ID` | `l1d7nflxbC8vLMR1KuMa`             |

Click **Save Changes**. Render will redeploy automatically.

### 4. Confirm the service is live

Wait ~2 minutes for the first build. Then visit:

```
https://agent4-webhook-receiver.onrender.com/health
```

*(your exact URL is shown at the top of the Render service dashboard)*

You should see JSON:

```json
{
  "ok": true,
  "service": "agent4-webhook-receiver",
  "location_id_set": true,
  "ghl_token_set": true,
  "endpoints": ["POST /webhooks/ghl/inbound-sms", "POST /webhooks/retell/call-ended"],
  "legal_auth_id": "MONG-ACK-2026-09-11"
}
```

If `ghl_token_set` is `false`, the env var didn't save — go back to step 3.

### 5. Configure GHL to POST to the webhook

Two places in GHL need updating. **Do these while Dax is watching so he can approve each one.**

**A. Inbound SMS webhook (fires on every reply)**

1. In GHL → Location Settings → **Webhooks** (or **Automation → Webhooks**)
2. Create webhook: **Trigger =** `Inbound SMS` (or `SMS Received`)
3. **URL:** `https://agent4-webhook-receiver.onrender.com/webhooks/ghl/inbound-sms`
4. **Method:** POST
5. **Payload:** the default GHL payload works. It must include `contactId` and `body` (or `message`).
6. Save.

**B. Retell call-ended webhook**

1. In Retell dashboard → Agent → **Webhooks** or **Events**
2. Subscribe to `call.ended` (or the equivalent event name)
3. **URL:** `https://agent4-webhook-receiver.onrender.com/webhooks/retell/call-ended`
4. Save.

### 6. Send a test SMS to verify end-to-end

1. Text your own cell phone: pretend to be a lead. Text `YES` to the Twilio number `+18136095500`.
2. Within 5-10 seconds you should:
   - Receive the State 1 auto-reply pointing you to the VA questionnaire
   - See the opportunity move to `Intake Form Sent` in GHL (only if Dax has created that stage — see "Pre-Monday Dax checklist")
3. In Render → your service → **Logs** — you should see:
   - `POST /webhooks/ghl/inbound-sms 200`
   - A line from `reply_router` about the state decision

---

## Pre-Monday Dax checklist (must be done in GHL before this is useful)

The webhook receiver will run in "safe fallback" mode until Dax completes these. It will not crash — TODO placeholder fields simply no-op with a log line.

1. **Create 4 custom fields** in Location Settings → Custom Fields:
   - `intake_completed_at` (DATE)
   - `rating_decision_uploaded_at` (DATE)
   - `intake_status` (dropdown: Not Started / Sent / Completed)
   - `rating_decision_status` (dropdown: Not Requested / Requested / Uploaded)
   - Send Ricardo the four field IDs.

2. **Create 3 new pipeline stages** in `AI Autopilot SMC Meta Leads 2026-New`:
   - `Intake Form Sent` (after `SMC Lead - SMS Responded (High Intent)`)
   - `Rating Decision Requested` (after `Intake Form Sent`)
   - `SMS Reply - Human Review` (parallel branch off `Contact Attempt X`)
   - Send Ricardo the three stage IDs.

3. **Build 2 GHL workflows** that stamp the timestamps:
   - When form `qedI4Yz2L6dQAycOORd9` OR `j8aIS21kKrX6Wbowjklm` is submitted → set `intake_completed_at = now`
   - When form `a3VxDFJV8OodEJ23T8BB` is submitted → set `rating_decision_uploaded_at = now`

Once Dax sends the IDs, Ricardo updates `agent4/config.json`, commits, pushes. Render auto-redeploys in ~2 minutes.

---

## Observability

- **Render Logs tab** — every request, every router decision, every stage-move attempt. Kept for 7 days on Starter.
- **`agent4/logs/webhook_events.jsonl`** — every payload and response, appended in-process. This is on the ephemeral disk and clears on every deploy or restart. For durable audit logs, forward Render logs to Papertrail or Better Stack ($5/mo).

---

## Kill switch

Two ways to stop the service if it misbehaves:

**Fast (Ricardo has access):**
- Render dashboard → your service → **Suspend Service**. Immediate. All webhooks return 503.

**Slow (edit + push):**
- Edit `agent4/logs/agent_state.json`: set `kill_switch_engaged` to `true`, commit, push. Router refuses to send SMS or move stages. Retell webhook still records outcomes.

---

## Cost summary

| Item                         | Cost           |
| ---------------------------- | -------------- |
| Render Starter web service   | **$7/mo**      |
| GitHub (private repo)        | $0             |
| GHL PIT                      | $0             |
| Retell webhook               | $0 (already paying for the agent) |
| **Total additional infra**   | **$7/mo**      |

---

## When to upgrade this deploy

- **Traffic > 100k requests/day** → move to Render Standard ($25/mo)
- **Need multiple regions / HA** → move to Fly.io or Render Pro
- **Need durable state across restarts** → add Render's managed Postgres ($7/mo more) and migrate `attempts.json` reads/writes into it
- **Compliance requires a private tenant** → move to a dedicated VPS or AWS/GCP with a signed BAA

---

## Something broke?

1. Check Render Logs tab — most issues show up there in the last 10 lines
2. Hit `/health` — if it returns 200 but `ghl_token_set: false`, the env var isn't set
3. Grep the logs for `reply_router` or `GHL` to trace a specific reply
4. If `curl` from your laptop works but GHL doesn't, the webhook URL in GHL is wrong — verify exact spelling, no trailing slash
