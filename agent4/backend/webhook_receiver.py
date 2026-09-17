"""Agent 4 — inbound webhook receiver.

Legal authorization on file: MONG-ACK-2026-09-11

Two endpoints:

  POST /webhooks/ghl/inbound-sms
        GoHighLevel calls this when a lead replies to an SMS.

  POST /webhooks/retell/call-ended
        Retell calls this when a call ends. We:
          1. Map disconnection_reason -> outcome (ANSWERED / VOICEMAIL / NO_ANSWER / BUSY / FAILED)
          2. Record outcome in local attempts.json
          3. If call.call_analysis is present, format it as an HTML note and POST to GHL
          4. Decide the pipeline stage move based on outcome + engagement
          5. Move the GHL opportunity tile accordingly

Both endpoints are idempotent — receiving the same event twice does not
double-count, and stage moves are idempotent (setting the same stage twice
is a no-op in GHL).
"""
from __future__ import annotations

import json
import logging
import os
import re
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

# ROOT resolves to agent4/ dir alongside this file's package.
ROOT = Path(os.environ.get("AGENT4_ROOT") or Path(__file__).resolve().parents[1])
CONFIG_PATH = ROOT / "config.json"
ATTEMPTS_PATH = ROOT / "logs" / "attempts.json"
WEBHOOK_LOG = ROOT / "logs" / "webhook_events.jsonl"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook_receiver")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_config() -> Dict[str, Any]:
    """Load the shared agent4 config (pipeline + stage IDs)."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _stage_id(cfg: Dict[str, Any], stage_name: str) -> Optional[str]:
    """Look up a target-pipeline stage id by human-readable name."""
    stages = ((cfg.get("target_pipeline") or {}).get("stages") or {})
    return stages.get(stage_name)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def _save_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            "payload_summary": _payload_summary(payload),
            "result": result,
        }) + "\n")


def _payload_summary(payload: dict) -> dict:
    """Small summary — full payloads can be huge (audio transcripts)."""
    call = payload.get("call") or {}
    return {
        "call_id": call.get("call_id"),
        "call_status": call.get("call_status"),
        "duration_ms": call.get("duration_ms"),
        "disconnection_reason": call.get("disconnection_reason"),
        "has_transcript": bool(call.get("transcript")),
        "has_call_analysis": bool(call.get("call_analysis")),
        "metadata": call.get("metadata"),
    }


# ---------------------------------------------------------------------------
# Retell disconnect_reason -> our outcome enum
# ---------------------------------------------------------------------------

RETELL_OUTCOME_MAP = {
    "user_hangup":               "ANSWERED",
    "agent_hangup":               "ANSWERED",
    "call_transfer":              "ANSWERED",
    "voicemail_reached":          "VOICEMAIL",
    "machine_detected":           "VOICEMAIL",
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


def map_retell_outcome(disconnect_reason: str,
                       call_analysis: Optional[dict] = None) -> str:
    """Map Retell disconnect_reason to our outcome enum.

    Also inspects call_analysis.call_summary to catch Retell's mislabeled
    voicemails (agent_hangup on a machine pickup). Retell often emits
    disconnection_reason='agent_hangup' or 'user_hangup' with duration ~30-45s
    when Ava left a message on a machine; the summary reliably contains
    'voicemail' or 'answering machine' in that case.
    """
    base = RETELL_OUTCOME_MAP.get(disconnect_reason, "FAILED")
    if base == "ANSWERED" and call_analysis:
        summary = (call_analysis.get("call_summary") or "").lower()
        if any(kw in summary for kw in
               ("voicemail", "answering machine", "voice mail",
                "left a message", "left a brief message")):
            return "VOICEMAIL"
    return base


# ---------------------------------------------------------------------------
# Post-call stage routing (bug #3 + #7 fix)
# ---------------------------------------------------------------------------

# Duration thresholds (milliseconds) for engagement classification.
# Under 15s = never engaged past greeting.
# 15-45s = brief conversation, likely refused or immediately declined.
# 45s+ = real engagement.
DURATION_NO_ENGAGE_MS = 15_000
DURATION_BRIEF_MS = 45_000


def decide_stage_move(outcome: str,
                       duration_ms: int,
                       call_analysis: Optional[dict],
                       cfg: Dict[str, Any]) -> tuple[Optional[str], str]:
    """Return (stage_id, human_readable_reason) for this call.

    Target pipeline: AI Autopilot SMC Meta Leads 2026-New

    Rules:
      ANSWERED + declined_request == True            -> Not Qualified / Archived (highest priority)
      ANSWERED + intake_completion=='complete'       -> SMC Qualified - Schedule Consult
      ANSWERED + requested_callback == True          -> Call Back Later
      ANSWERED + intake_completion=='partial'        -> SMC Lead - SMS Responded (High Intent)
      ANSWERED + intake_completion=='refused'        -> Not Qualified / Archived
      ANSWERED + intake_completion=='transferred'    -> SMC Lead - SMS Responded (High Intent)
      ANSWERED + short/no engagement                 -> Contact Attempt 2 - Retry
      ANSWERED + engaged but no analysis available   -> SMC Lead - SMS Responded (High Intent) (safe human review path)
      VOICEMAIL / NO_ANSWER / BUSY                   -> Contact Attempt 2 - Retry
      FAILED                                         -> no move (technical error, keep current stage)
    """
    stage = lambda name: _stage_id(cfg, name)

    analysis_data = None
    if call_analysis:
        analysis_data = call_analysis.get("custom_analysis_data") or {}

    if outcome == "ANSWERED":
        if analysis_data:
            completion = (analysis_data.get("intake_completion") or "").lower()
            requested_cb = bool(analysis_data.get("requested_callback"))
            declined = bool(analysis_data.get("declined_request"))
            # Declined always wins — must not be called again.
            if declined:
                return stage("Not Qualified / Archived"), "veteran declined — do not re-contact"
            if completion == "complete":
                return stage("SMC Qualified - Schedule Consult"), "intake completed"
            if requested_cb:
                return stage("Call Back Later"), "veteran requested callback"
            if completion == "partial":
                return stage("SMC Lead - SMS Responded (High Intent)"), "partial intake — human review"
            if completion == "refused":
                return stage("Not Qualified / Archived"), "veteran refused intake"
            if completion == "transferred":
                return stage("SMC Lead - SMS Responded (High Intent)"), "transferred to human intake"
            if completion == "no_engagement":
                return stage("Contact Attempt 2 - Retry"), "no engagement past greeting — retry"
        # No analysis available yet — fall back to duration heuristics
        if duration_ms < DURATION_NO_ENGAGE_MS:
            return stage("Contact Attempt 2 - Retry"), "very short call — retry"
        if duration_ms < DURATION_BRIEF_MS:
            return stage("Contact Attempt 2 - Retry"), "brief call, no analysis — retry"
        return stage("SMC Lead - SMS Responded (High Intent)"), "engaged but no intake summary — human review"

    if outcome in ("VOICEMAIL", "NO_ANSWER", "BUSY"):
        return stage("Contact Attempt 2 - Retry"), f"call {outcome.lower()} — retry"

    # FAILED — technical error, no move
    return None, "call failed technically — no stage move"


# ---------------------------------------------------------------------------
# GHL note formatting (bug #6 fix — always post a note)
# ---------------------------------------------------------------------------

# Fields to redact/mask when writing intake notes back to GHL.
SSN_FIELD_PATTERNS = [
    re.compile(r"\bSocial Security Number\b", re.IGNORECASE),
    re.compile(r"\bSSN\b", re.IGNORECASE),
]


def _mask_ssn(value: str) -> str:
    """Return only last-4 for a 9-digit SSN. Leave short values alone."""
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 4:
        return f"XXX-XX-{digits[-4:]}"
    return "REDACTED"


def format_intake_note(call: dict, outcome: str, stage_reason: str) -> str:
    """Format the call result as an HTML note body suitable for GHL /contacts/{id}/notes."""
    dur_ms = call.get("duration_ms") or 0
    dur_s = round(dur_ms / 1000)
    disc = call.get("disconnection_reason", "unknown")
    call_id = call.get("call_id", "")
    recording_url = call.get("recording_url", "")

    analysis = call.get("call_analysis") or {}
    custom = analysis.get("custom_analysis_data") or {}
    summary = analysis.get("call_summary") or custom.get("call_outcome_summary") or ""

    lines = []
    lines.append("<b>Agent 4 AI Call Summary</b>")
    lines.append(f"<b>Outcome:</b> {outcome}")
    lines.append(f"<b>Duration:</b> {dur_s}s")
    lines.append(f"<b>Disconnect:</b> {disc}")
    lines.append(f"<b>Stage decision:</b> {stage_reason}")
    if summary:
        lines.append("")
        lines.append(f"<b>Call summary:</b> {summary}")

    if custom:
        lines.append("")
        lines.append("<b>Captured intake:</b>")
        field_labels = [
            ("veteran_full_name", "Name"),
            ("veteran_age", "Age"),
            ("date_of_birth", "DOB"),
            ("branch_of_service", "Branch"),
            ("service_start_date", "Service start"),
            ("service_end_date", "Service end"),
            ("discharge_type", "Discharge type"),
            ("previously_filed_va_claim", "Previously filed VA claim"),
            ("current_va_rating_percent", "Current VA rating %"),
            ("currently_working", "Currently working"),
            ("primary_disabilities", "Primary disabilities"),
            ("va_facility_treatment", "VA treatment facility"),
            ("how_heard", "How they heard about us"),
            ("intake_completion", "Intake completion"),
            ("requested_callback", "Requested callback"),
            ("declined_recording", "Declined recording"),
        ]
        for key, label in field_labels:
            val = custom.get(key)
            if val is None or val == "":
                continue
            # SSN is NOT in our schema anymore (bug #2 fix — SSN moved to secure form),
            # but if it ever leaks in, mask it.
            for pat in SSN_FIELD_PATTERNS:
                if pat.search(label):
                    val = _mask_ssn(str(val))
                    break
            lines.append(f"  • <b>{label}:</b> {val}")

    lines.append("")
    lines.append(f"<b>Retell call_id:</b> {call_id}")
    if recording_url:
        lines.append(f'<b>Recording:</b> <a href="{recording_url}">Listen</a>')

    return "<br/>".join(lines)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def handle_ghl_inbound_sms(payload: dict,
                           client: Optional[Any] = None) -> dict:
    contact_id = payload.get("contactId") or payload.get("contact_id")
    body = payload.get("body") or payload.get("message") or ""
    if not contact_id:
        return {"ok": False, "error": "missing_contact_id"}

    cls = classify_reply(body)
    now = datetime.now(timezone.utc)

    attempts = _load_json(ATTEMPTS_PATH, {})
    matched: list[str] = []
    for opp_id, rec in attempts.items():
        if rec.get("contact_id") == contact_id:
            record_reply(rec, cls, body, now)
            matched.append(opp_id)

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
        except Exception as e:
            log.exception("reply_router failed for contact %s", contact_id)
            router_result = {"error": str(e)}

    if matched:
        _save_atomic(ATTEMPTS_PATH, attempts)

    return {"ok": True, "reply_class": cls, "matched_opps": matched,
            "matched_count": len(matched),
            "router_result": router_result}


PROCESSED_CALLS_PATH = ROOT / "data" / "_processed_calls.json"


def handle_retell_call_ended(payload: dict,
                              client: Optional[Any] = None) -> dict:
    """Handle Retell call-ended webhook.

    Now performs the full pipeline: record outcome, format & post GHL note,
    and move the opportunity tile.

    Idempotency: Retell delivers several webhooks per call
    (call_started -> call_ended -> call_analyzed). We only act when the
    payload carries an analyzed call, and we dedupe by call_id so retries
    from Retell don't cause duplicate GHL writes.
    """
    call = payload.get("call") or payload
    meta = call.get("metadata") or {}
    opp_id = meta.get("ghl_opp_id")
    contact_id = meta.get("ghl_contact_id")
    disconnect_reason = call.get("disconnection_reason") or "error_unknown"
    duration_ms = call.get("duration_ms") or 0
    call_id = call.get("call_id") or payload.get("call_id")
    event = payload.get("event") or call.get("event") or ""
    call_status = call.get("call_status") or ""
    call_analysis = call.get("call_analysis") or {}

    if not opp_id:
        return {"ok": False, "error": "missing_ghl_opp_id_in_metadata"}

    # Idempotency + event gating.
    # Only act once per call_id, and only when the call is truly finished
    # and analysis is present (Retell emits 3 webhook events per call).
    # We accept the first webhook where both:
    #    call_status == 'ended' AND call_analysis is non-empty
    # Or event == 'call_analyzed'. Otherwise we just log and return early.
    is_final = (event == "call_analyzed") or (
        call_status == "ended" and bool(call_analysis))

    if call_id:
        try:
            processed = _load_json(PROCESSED_CALLS_PATH, {})
        except Exception:
            processed = {}

        if call_id in processed:
            return {"ok": True, "skipped": "already_processed",
                    "call_id": call_id, "opp_id": opp_id,
                    "first_processed_at": processed.get(call_id, {}).get("at")}

        if not is_final:
            return {"ok": True, "skipped": "waiting_for_final_event",
                    "call_id": call_id, "opp_id": opp_id,
                    "event": event, "call_status": call_status,
                    "has_analysis": bool(call_analysis)}

        # Reserve this call_id BEFORE doing GHL writes so a concurrent retry
        # from Retell can't double-post.
        processed[call_id] = {"at": datetime.now(timezone.utc).isoformat(),
                              "opp_id": opp_id}
        try:
            _save_atomic(PROCESSED_CALLS_PATH, processed)
        except Exception:
            log.exception("could not persist processed_calls")

    outcome = map_retell_outcome(disconnect_reason, call_analysis)
    now = datetime.now(timezone.utc)

    # 1. Update local attempts.json (existing behavior)
    attempts = _load_json(ATTEMPTS_PATH, {})
    rec = attempts.get(opp_id)
    if not rec:
        rec = new_attempt_record(opp_id, contact_id, current_stage_id="")
        attempts[opp_id] = rec

    record_call_outcome(rec, outcome, now)
    _save_atomic(ATTEMPTS_PATH, attempts)

    # 2. Load config for stage IDs
    try:
        cfg = _load_config()
    except Exception as e:
        log.exception("could not load config.json")
        return {"ok": True, "outcome": outcome, "opp_id": opp_id,
                "ghl_actions": {"error": f"config_load_failed: {e}"}}

    # 3. Build the note (always post — bug #6 fix)
    stage_id, stage_reason = decide_stage_move(outcome, duration_ms,
                                                call_analysis,
                                                cfg)
    note_body = format_intake_note(call, outcome, stage_reason)

    # 4. Actually post to GHL (only if we have a client)
    ghl_actions = {"note_posted": False, "stage_moved": False,
                    "target_stage_id": stage_id,
                    "stage_reason": stage_reason}

    if client is not None and contact_id:
        try:
            note_resp = client.add_note(contact_id, note_body)
            ghl_actions["note_posted"] = True
            ghl_actions["note_id"] = (note_resp or {}).get("note", {}).get("id")
        except Exception as e:
            log.exception("failed to post GHL note")
            ghl_actions["note_error"] = str(e)

    if client is not None and stage_id and opp_id:
        try:
            target_pipeline_id = (cfg.get("target_pipeline") or {}).get("id")
            client.update_opportunity(
                opp_id,
                pipeline_id=target_pipeline_id,
                pipeline_stage_id=stage_id,
                status="open",
            )
            ghl_actions["stage_moved"] = True
        except Exception as e:
            log.exception("failed to move GHL opportunity")
            ghl_actions["stage_error"] = str(e)

    return {"ok": True, "outcome": outcome, "opp_id": opp_id,
            "contact_id": contact_id,
            "duration_ms": duration_ms,
            "disconnect_reason": disconnect_reason,
            "ghl_actions": ghl_actions}


# ---------------------------------------------------------------------------
# Flask app (thin adapter, used only when file is run directly)
# ---------------------------------------------------------------------------


def create_app():  # pragma: no cover
    if Flask is None:
        raise RuntimeError("Flask not installed in this environment")
    app = Flask(__name__)

    def _make_client():
        try:
            cfg = _load_config()
            return GhlClient(cfg["location_id"])
        except Exception:
            return None

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"ok": True, "service": "agent4-webhook-receiver"})

    @app.route("/webhooks/ghl/inbound-sms", methods=["POST"])
    def ghl_sms():
        payload = request.get_json(force=True, silent=True) or {}
        client = _make_client()
        result = handle_ghl_inbound_sms(payload, client=client)
        _append_event("ghl_inbound_sms", payload, result)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/webhooks/retell/call-ended", methods=["POST"])
    def retell_call_ended():
        payload = request.get_json(force=True, silent=True) or {}
        client = _make_client()
        result = handle_retell_call_ended(payload, client=client)
        _append_event("retell_call_ended", payload, result)
        return jsonify(result), (200 if result.get("ok") else 400)

    return app


if __name__ == "__main__":  # pragma: no cover
    port = int(os.environ.get("PORT", 8001))
    app = create_app()
    app.run(host="0.0.0.0", port=port)
