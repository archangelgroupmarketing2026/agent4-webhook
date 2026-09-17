"""Agent 4 webhook receiver — Render.com entry point.

WSGI entry: gunicorn app:app --bind 0.0.0.0:$PORT

Exposes:
  GET  /health                       - liveness probe (returns JSON)
  GET  /                             - status JSON (also served as index)
  POST /webhooks/ghl/inbound-sms     - GHL inbound SMS webhook
  POST /webhooks/retell/call-ended   - Retell call-ended webhook

Required environment variables:
  GHL_PIT              - GoHighLevel Private Integration Token (sub-account)
  GHL_LOCATION_ID      - Optional override; defaults to value in config.json

Legal authorization on file: MONG-ACK-2026-09-11
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Make the packaged agent4/ importable.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("AGENT4_ROOT", str(HERE / "agent4"))

from flask import Flask, jsonify, request  # noqa: E402

from agent4.backend.webhook_receiver import (  # noqa: E402
    handle_ghl_inbound_sms,
    handle_retell_call_ended,
    _append_event,
    CONFIG_PATH,
)
from agent4.lib.ghl_client import GhlClient, GhlAuthError  # noqa: E402


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("agent4.app")

app = Flask(__name__)


def _load_location_id() -> str:
    env = os.environ.get("GHL_LOCATION_ID")
    if env:
        return env
    with open(CONFIG_PATH) as f:
        return json.load(f)["location_id"]


try:
    LOCATION_ID = _load_location_id()
except Exception as e:  # noqa: BLE001
    log.warning("could not resolve location_id at boot: %s", e)
    LOCATION_ID = None  # type: ignore


def _make_client():
    """Build a fresh GhlClient per request.

    Returns None if credentials are absent so the router will run in
    dry-run mode instead of crashing.
    """
    try:
        return GhlClient(LOCATION_ID) if LOCATION_ID else None
    except GhlAuthError as e:
        log.warning("GHL client unavailable: %s", e)
        return None
    except Exception as e:  # noqa: BLE001
        log.exception("unexpected error building GHL client: %s", e)
        return None


@app.route("/health", methods=["GET"])
def health():
    """Readiness probe. Returns 200 with service metadata."""
    return jsonify({
        "ok": True,
        "service": "agent4-webhook-receiver",
        "location_id_set": bool(LOCATION_ID),
        "ghl_token_set": bool(os.environ.get("GHL_PIT")),
        "endpoints": [
            "POST /webhooks/ghl/inbound-sms",
            "POST /webhooks/retell/call-ended",
        ],
        "legal_auth_id": "MONG-ACK-2026-09-11",
    })


@app.route("/", methods=["GET"])
def root():
    """Human-readable landing endpoint."""
    return jsonify({
        "service": "agent4-webhook-receiver",
        "status": "running",
        "docs": "POST /webhooks/ghl/inbound-sms, POST /webhooks/retell/call-ended",
    })


@app.route("/webhooks/ghl/inbound-sms", methods=["POST"])
def ghl_inbound_sms():
    payload = request.get_json(force=True, silent=True) or {}
    client = _make_client()
    result = handle_ghl_inbound_sms(payload, client=client)
    try:
        _append_event("ghl_inbound_sms", payload, result)
    except Exception:  # noqa: BLE001
        log.exception("failed to append event log")
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/webhooks/retell/call-ended", methods=["POST"])
def retell_call_ended():
    payload = request.get_json(force=True, silent=True) or {}
    client = _make_client()
    result = handle_retell_call_ended(payload, client=client)
    try:
        _append_event("retell_call_ended", payload, result)
    except Exception:  # noqa: BLE001
        log.exception("failed to append event log")
    return jsonify(result), (200 if result.get("ok") else 400)


if __name__ == "__main__":
    # Local dev. In production, gunicorn runs this via `app:app`.
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
