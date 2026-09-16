"""Agent 4 — inbound webhook receiver.

Legal authorization on file: MONG-ACK-2026-09-11

Two endpoints:

  POST /webhooks/ghl/inbound-sms
        GoHighLevel calls this when a lead replies to an SMS.
        Expected payload (GHL workflow "Send Webhook" action):
          {
            "contactId": "...",
            "body": "...",
            "phone": "+1...",
            "direction": "inbound",
            "messageType": "SMS",
            ...
          }
        We classify the body (STOP / POSITIVE / OTHER) and update
        attempts.json for every opp tied to this contact_id.

  POST /webhooks/retell/call-ended
        Retell calls this when a Michelle call ends.
        Expected payload:
          {
            "call": {
              "call_status": "ended",
              "disconnection_reason": "user_hangup" | "dial_no_answer" | ...,
              "metadata": {
                "ghl_opp_id": "...",
                "ghl_contact_id": "...",
                "legal_auth": "MONG-ACK-2026-09-11"
              }
            }
          }
        We map disconnection_reason -> ANSWERED / VOICEMAIL / NO_ANSWER /
        BUSY / FAILED and record it against the opp.

Both endpoints are idempotent — receiving the same event twice does not
double-count.

The scanner reads the resulting attempts.json on its next scan cycle and
decides the next action from the updated state. The webhook receiver itself
does NOT trigger any GHL writes or dial dispatches — that separation is what
lets the kill switch work.

DEPLOY
======
Meant to be deployed via `deploy_website` to a durable Perplexity URL and
have that URL configured in:
  - GHL: Location Settings -> Webhooks -> inbound SMS
  - Retell: Michelle agent settings -> post_call_webhook_url

For local sandbox testing:
  python -m agent4.backend.webhook_receiver
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from agent4.backend.sms_copy import classify_reply
    from agent4.backend.state_machine import (
        record_call_outcome, record_reply, new_attempt_record,
    )
    from agent4.backend.reply_router import route_reply
    from agent4.lib.ghl_client import GhlClient
except ImportError:  # pragma: no cover
    sys.path.insert(0, "/home/user/workspace")
    from agent4.backend.sms_copy import classify_reply  # noqa
    from agent4.backend.state_machine import (  # noqa
        record_call_outcome, record_reply, new_attempt_record,
    )
    from agent4.backend.reply_router import route_reply  # noqa
    from agent4.lib.ghl_client import GhlClient  # noqa

try:
    from flask import Flask, request, jsonify
except ImportError:
    Flask = None  # type: ignore

ROOT = Path(os.environ.get("AGENT4_ROOT") or Path(__file__).resolve().parents[1])
CONFIG_PATH = ROOT / "config.json"
ATTEMPTS_PATH = ROOT / "logs" / "attempts.json"
WEBHOOK_LOG = ROOT / "logs" / "webhook_events.jsonl"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook_receiver")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def _save_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(path)


def _append_event(kind: str, payload: dict, result: dict) -> None:
    WEBHOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with WEBHOOK_LOG.open("a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "payload": payload,
            "result": result,
        }) + "\n")


# ---------------------------------------------------------------------------
# Retell disconnect_reason -> our outcome enum
# ---------------------------------------------------------------------------

RETELL_OUTCOME_MAP = {
    # Human answered and had a conversation (any hangup side)
    "user_hangup":               "ANSWERED",
    "agent_hangup":               "ANSWERED",
    "call_transfer":              "ANSWERED",

    # Machine / voicemail
    "voicemail_reached":          "VOICEMAIL",
    "machine_detected":           "VOICEMAIL",

    # No-connect variants (trigger double-tap)
    "dial_no_answer":             "NO_ANSWER",
    "dial_busy":                  "BUSY",
    "dial_failed":                "FAILED",
    "concurrency_limit_reached":  "FAILED",
    "no_valid_payment":           "FAILED",
    "error_inbound_webhook":      "FAILED",
    "error_llm_websocket_open":   "FAILED",
    "error_frontend_corrupted_payload": "FAILED",
    "error_twilio":               "FAILED",
    "error_no_audio_received":    "FAILED",
    "error_asr":                  "FAILED",
    "error_retell":               "FAILED",
    "error_unknown":              "FAILED",
    "error_user_not_joined":      "NO_ANSWER",
    "registered_call_timeout":    "FAILED",
}


def map_retell_outcome(disconnect_reason: str) -> str:
    return RETELL_OUTCOME_MAP.get(disconnect_reason, "FAILED")


# ---------------------------------------------------------------------------
# Handlers (pure functions so we can unit-test without a running server)
# ---------------------------------------------------------------------------


def handle_ghl_inbound_sms(payload: dict,
                           client: Optional[Any] = None) -> dict:
    contact_id = payload.get("contactId") or payload.get("contact_id")
    body = payload.get("body") or payload.get("message") or ""
    if not contact_id:
        return {"ok": False, "error": "missing_contact_id"}

    cls = classify_reply(body)
    now = datetime.now(timezone.utc)

    # Load attempts, find every opp for this contact.
    attempts = _load_json(ATTEMPTS_PATH, {})
    matched: list[str] = []
    for opp_id, rec in attempts.items():
        if rec.get("contact_id") == contact_id:
            record_reply(rec, cls, body, now)
            matched.append(opp_id)

    # STOP is handled by the scanner's state-machine (SUPPRESS -> DNC).
    # Router only runs for POSITIVE and OTHER.
    router_result = None
    if cls in ("POSITIVE", "OTHER") and matched:
        try:
            router_result = route_reply(
                contact_id=contact_id,
                reply_body=body,
                reply_class=cls,
                opp_ids=matched,
                attempts=attempts,
                client=client,
                dry_run=(client is None),
            )
        except Exception as e:  # noqa - webhook must never crash the receiver
            log.exception("reply_router failed for contact %s", contact_id)
            router_result = {"error": str(e)}

    if matched:
        _save_atomic(ATTEMPTS_PATH, attempts)

    return {"ok": True, "reply_class": cls, "matched_opps": matched,
            "matched_count": len(matched),
            "router_result": router_result}


def handle_retell_call_ended(payload: dict) -> dict:
    call = payload.get("call") or payload
    meta = call.get("metadata") or {}
    opp_id = meta.get("ghl_opp_id")
    disconnect_reason = call.get("disconnection_reason") or "error_unknown"
    if not opp_id:
        return {"ok": False, "error": "missing_ghl_opp_id_in_metadata"}

    outcome = map_retell_outcome(disconnect_reason)
    now = datetime.now(timezone.utc)

    attempts = _load_json(ATTEMPTS_PATH, {})
    rec = attempts.get(opp_id)
    if not rec:
        # Race: scanner hasn't created a record for this opp yet.
        # Create a minimal shell so we don't lose the outcome.
        contact_id = meta.get("ghl_contact_id")
        rec = new_attempt_record(opp_id, contact_id, current_stage_id="")
        attempts[opp_id] = rec

    record_call_outcome(rec, outcome, now)
    _save_atomic(ATTEMPTS_PATH, attempts)

    return {"ok": True, "outcome": outcome, "opp_id": opp_id,
            "disconnect_reason": disconnect_reason}


# ---------------------------------------------------------------------------
# Flask app (thin adapter)
# ---------------------------------------------------------------------------


def create_app():  # pragma: no cover
    if Flask is None:
        raise RuntimeError("Flask not installed in this environment")
    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"ok": True, "service": "agent4-webhook-receiver"})

    @app.route("/webhooks/ghl/inbound-sms", methods=["POST"])
    def ghl_sms():
        payload = request.get_json(force=True, silent=True) or {}
        # In prod, the Flask app has GHL credentials via the sandbox
        # environment. In tests, we construct a client per-call to avoid
        # long-lived sockets.
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            client = GhlClient(cfg["location_id"])
        except Exception:
            client = None
        result = handle_ghl_inbound_sms(payload, client=client)
        _append_event("ghl_inbound_sms", payload, result)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/webhooks/retell/call-ended", methods=["POST"])
    def retell_call_ended():
        payload = request.get_json(force=True, silent=True) or {}
        result = handle_retell_call_ended(payload)
        _append_event("retell_call_ended", payload, result)
        return jsonify(result), (200 if result.get("ok") else 400)

    return app


if __name__ == "__main__":  # pragma: no cover
    port = int(os.environ.get("PORT", 8001))
    app = create_app()
    app.run(host="0.0.0.0", port=port)
