# Agent 4 Webhook Receiver

Production webhook receiver for the Lonetto Law SMS-first outreach system. Runs on Render.com; talks to GoHighLevel (GHL) via a Private Integration Token and to Retell for outbound voice.

**Full deploy walkthrough:** see [DEPLOY.md](./DEPLOY.md).

## Endpoints

| Method | Path                              | Purpose                                                            |
| ------ | --------------------------------- | ------------------------------------------------------------------ |
| GET    | `/health`                         | Liveness probe. Returns JSON with token/config status.             |
| GET    | `/`                               | Root landing JSON.                                                 |
| POST   | `/webhooks/ghl/inbound-sms`       | GHL fires on every inbound SMS. Runs reply router.                 |
| POST   | `/webhooks/retell/call-ended`     | Retell fires when a call ends. Records outcome + retry decision.   |

## Reply router state machine

| Inbound reply     | Contact state                | Action                                                         |
| ----------------- | ---------------------------- | -------------------------------------------------------------- |
| Neutral / unclear | any                          | Route to `SMS Reply - Human Review`, no SMS                    |
| Positive          | no intake yet                | Send State 1 SMS (VA questionnaire link), move stage           |
| Positive          | intake done, no rating       | Send State 2 SMS (rating upload link), move stage              |
| Positive          | intake + rating both done    | Send State 3 SMS (confirmation), move to `SMC Qualified` stage |
| STOP              | any                          | Fully suppressed (handled by GHL native opt-out)               |

Auto-reply copy is centralized in `agent4/backend/sms_copy.py`.

## Project structure

```
.
├── app.py                              # Flask WSGI entry — gunicorn runs this
├── render.yaml                         # Render Blueprint spec
├── requirements.txt                    # Pinned Python deps
├── .env.example                        # Copy to .env for local dev
├── DEPLOY.md                           # Deployment walkthrough
└── agent4/
    ├── config.json                     # Pipeline / stage / field IDs
    ├── backend/
    │   ├── webhook_receiver.py         # Webhook handlers
    │   ├── reply_router.py             # State machine
    │   ├── state_machine.py            # Call/SMS attempt tracking
    │   └── sms_copy.py                 # Message templates
    ├── lib/
    │   └── ghl_client.py               # GHL LeadConnector v2 client
    └── logs/                           # Ephemeral runtime logs (gitignored)
```

## Local dev

```bash
cp .env.example .env
# fill in GHL_PIT
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py     # starts on port 8000
```

## Legal

Authorization on file: **MONG-ACK-2026-09-11** (Dax J. Lonetto Sr., Lonetto Law).
