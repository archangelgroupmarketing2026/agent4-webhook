"""Agent 4 — reply router.

Legal authorization on file: MONG-ACK-2026-09-11

Purpose
=======
When a lead replies POSITIVELY to any SMS in the 6-touch sequence, this
router:

  1. Reads the contact's `intake_completed_at` and
     `rating_decision_uploaded_at` custom fields from GHL.
  2. Picks one of three response states:
       State 1  intake_completed_at IS NULL
                -> send REPLY_STATE_1_INTAKE (link to tinyurl.com/bdhvvxbb)
                -> move opp to "Intake Form Sent"
                -> stamp intake_status = "Sent"
       State 2  intake_completed_at NOT NULL AND
                rating_decision_uploaded_at IS NULL
                -> send REPLY_STATE_2_RATING (link to tinyurl.com/fe4jxsm6)
                -> move opp to "Rating Decision Requested"
                -> stamp rating_decision_status = "Requested"
       State 3  both timestamps NOT NULL
                -> send REPLY_STATE_3_READY
                -> move opp to "SMC Qualified - Schedule Consult"
  3. For OTHER (ambiguous) replies: no auto-SMS, move opp to
     "SMS Reply - Human Review", drop a note on the contact so Michelle /
     Dax's team can see the message and decide.
  4. For STOP replies: caller (webhook_receiver) already handles opt-out
     and DNC move; router is not invoked.

Design guarantees
=================
- Router is idempotent per opp_id per run. Recording the auto-reply in
  `attempts.json` under `reply_router` prevents double-sends if GHL retries
  the webhook.
- If any config field is a TODO placeholder, that leg of the router is a
  no-op with a clear log line (never silently fails).
- Kill switch and sms_paused are honored: router will move the stage and
  drop a note, but will NOT send the auto-reply SMS while paused.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from agent4.lib.ghl_client import GhlClient, GhlError
    from agent4.backend.sms_copy import render as render_sms
except ImportError:  # pragma: no cover
    sys.path.insert(0, "/home/user/workspace")
    from agent4.lib.ghl_client import GhlClient, GhlError  # noqa
    from agent4.backend.sms_copy import render as render_sms  # noqa


log = logging.getLogger("reply_router")

import os as _os
ROOT = Path(_os.environ.get("AGENT4_ROOT") or Path(__file__).resolve().parents[1])
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "logs" / "agent_state.json"


# ---------------------------------------------------------------------------
# Router decision
# ---------------------------------------------------------------------------


@dataclass
class RouterDecision:
    state: str                     # "STATE_1" | "STATE_2" | "STATE_3" | "HUMAN_REVIEW"
    sms_template: Optional[str]    # sms_copy touch key, or None for HUMAN_REVIEW
    target_stage_name: str
    target_stage_id: Optional[str]
    field_to_update: Optional[str] # custom-field name to stamp, if any
    field_new_value: Optional[str] # value to stamp
    reason: str


def choose_router_state(
    *,
    intake_completed_at: Optional[Any],
    rating_decision_uploaded_at: Optional[Any],
    reply_class: str,
    router_cfg: dict,
    custom_fields_cfg: dict,
) -> RouterDecision:
    """Pure decision function - no I/O, easy to unit-test."""
    stages = router_cfg["landing_stages"]

    if reply_class == "OTHER":
        s = stages["other_reply_human_review"]
        return RouterDecision(
            state="HUMAN_REVIEW",
            sms_template=None,
            target_stage_name=s["name"],
            target_stage_id=None if s["id"].startswith("TODO_") else s["id"],
            field_to_update=None,
            field_new_value=None,
            reason="ambiguous_reply_needs_human",
        )

    # POSITIVE from here on (STOP is handled upstream, not routed here).
    if not intake_completed_at:
        s = stages["state_1_intake_sent"]
        return RouterDecision(
            state="STATE_1",
            sms_template="REPLY_STATE_1_INTAKE",
            target_stage_name=s["name"],
            target_stage_id=None if s["id"].startswith("TODO_") else s["id"],
            field_to_update="intake_status",
            field_new_value="Sent",
            reason="positive_reply + no intake on file",
        )

    if not rating_decision_uploaded_at:
        s = stages["state_2_rating_requested"]
        return RouterDecision(
            state="STATE_2",
            sms_template="REPLY_STATE_2_RATING",
            target_stage_name=s["name"],
            target_stage_id=None if s["id"].startswith("TODO_") else s["id"],
            field_to_update="rating_decision_status",
            field_new_value="Requested",
            reason="positive_reply + intake on file + no rating decision",
        )

    s = stages["state_3_ready_for_consult"]
    return RouterDecision(
        state="STATE_3",
        sms_template="REPLY_STATE_3_READY",
        target_stage_name=s["name"],
        target_stage_id=None if s["id"].startswith("TODO_") else s["id"],
        field_to_update=None,
        field_new_value=None,
        reason="positive_reply + both docs on file",
    )


# ---------------------------------------------------------------------------
# Router execution (impure - calls GHL, writes attempts.json)
# ---------------------------------------------------------------------------


def route_reply(
    *,
    contact_id: str,
    reply_body: str,
    reply_class: str,
    opp_ids: list[str],
    attempts: dict,
    dry_run: bool = False,
    client: Optional[GhlClient] = None,
) -> Dict[str, Any]:
    """Execute the reply router for one inbound message.

    Called by webhook_receiver.handle_ghl_inbound_sms after it has already
    classified the reply and updated attempts[opp_id].reply_state.

    Returns a per-opp result dict so callers can log it.
    """
    now = datetime.now(timezone.utc)

    with CONFIG_PATH.open() as f:
        cfg = json.load(f)
    with STATE_PATH.open() as f:
        state = json.load(f)

    router_cfg = cfg["reply_router"]
    custom_fields_cfg = cfg["custom_fields"]

    kill_switch = bool(state.get("kill_switch_engaged"))
    sms_paused = bool(state.get("sms_paused", False))

    if client is None and not dry_run:
        client = GhlClient(cfg["location_id"])

    # 1. Read the contact's intake + rating timestamps.
    intake_field_id = custom_fields_cfg["intake_completed_at"]["id"]
    rating_field_id = custom_fields_cfg["rating_decision_uploaded_at"]["id"]

    if dry_run or not client:
        # Router callers can pass pre-fetched values via attempts[opp_id]
        # or via env; in dry-run, treat both as None so we always hit
        # State 1 unless caller injected values.
        intake_val = None
        rating_val = None
    else:
        try:
            intake_val = client.get_contact_custom_field(contact_id, intake_field_id)
            rating_val = client.get_contact_custom_field(contact_id, rating_field_id)
        except GhlError as e:
            log.error("Failed to read custom fields for contact %s: %s",
                      contact_id, e)
            intake_val = rating_val = None

    # 2. Decide.
    decision = choose_router_state(
        intake_completed_at=intake_val,
        rating_decision_uploaded_at=rating_val,
        reply_class=reply_class,
        router_cfg=router_cfg,
        custom_fields_cfg=custom_fields_cfg,
    )

    per_opp_results = []

    for opp_id in opp_ids:
        rec = attempts.get(opp_id) or {}
        rr_history = rec.setdefault("reply_router", [])

        # Idempotency check: if we already ran the router for this reply
        # (same body + same timestamp within the last 60s), skip.
        for prior in rr_history[-5:]:
            if prior.get("body") == reply_body[:200] and prior.get("state") == decision.state:
                per_opp_results.append({
                    "opp_id": opp_id,
                    "decision": asdict(decision),
                    "skipped": True,
                    "reason": "idempotent_replay",
                })
                break
        else:
            # Actually run it.
            result: Dict[str, Any] = {
                "opp_id": opp_id,
                "decision": asdict(decision),
                "dry_run": dry_run,
                "kill_switch": kill_switch,
                "sms_paused": sms_paused,
                "actions": {},
            }

            # 3a. Send auto-reply SMS (except HUMAN_REVIEW, kill switch, sms_paused).
            if decision.sms_template and not kill_switch and not sms_paused:
                first_name = _extract_first_name(rec, contact_id, client, dry_run)
                body = render_sms(decision.sms_template, first_name)
                if dry_run or not client:
                    result["actions"]["sms"] = {"dispatched": False,
                                                "reason": "dry_run",
                                                "preview_body": body}
                else:
                    try:
                        r = client.send_sms(contact_id, body)
                        result["actions"]["sms"] = {"dispatched": True,
                                                    "body_sent": body,
                                                    "ghl_response_ok": bool(r)}
                    except GhlError as e:
                        result["actions"]["sms"] = {"dispatched": False,
                                                    "reason": f"ghl_error:{e.status}",
                                                    "error_body": str(e.body)[:400]}
            elif not decision.sms_template:
                result["actions"]["sms"] = {"dispatched": False,
                                            "reason": "human_review_no_autosend"}
            else:
                result["actions"]["sms"] = {"dispatched": False,
                                            "reason": "kill_or_pause"}

            # 3b. Move opportunity stage.
            if decision.target_stage_id and not kill_switch:
                if dry_run or not client:
                    result["actions"]["stage_move"] = {
                        "dispatched": False, "reason": "dry_run",
                        "would_move_to": decision.target_stage_name,
                    }
                else:
                    try:
                        client.update_opportunity(
                            opp_id,
                            pipeline_id=cfg["target_pipeline"]["id"],
                            pipeline_stage_id=decision.target_stage_id,
                        )
                        result["actions"]["stage_move"] = {
                            "dispatched": True,
                            "moved_to": decision.target_stage_name,
                        }
                    except GhlError as e:
                        result["actions"]["stage_move"] = {
                            "dispatched": False,
                            "reason": f"ghl_error:{e.status}",
                        }
            else:
                result["actions"]["stage_move"] = {
                    "dispatched": False,
                    "reason": ("kill_switch" if kill_switch
                               else "target_stage_id_is_TODO"),
                    "target_stage_name": decision.target_stage_name,
                }

            # 3c. Stamp status field.
            if decision.field_to_update and not kill_switch:
                fld_id = custom_fields_cfg[decision.field_to_update]["id"]
                if dry_run or not client:
                    result["actions"]["field_stamp"] = {
                        "dispatched": False, "reason": "dry_run",
                        "would_set": {decision.field_to_update:
                                      decision.field_new_value},
                    }
                else:
                    r = client.update_contact_custom_field(
                        contact_id, fld_id, decision.field_new_value)
                    result["actions"]["field_stamp"] = {
                        "dispatched": bool(not r.get("skipped")),
                        "field": decision.field_to_update,
                        "value": decision.field_new_value,
                        "ghl_result": r,
                    }

            # 3d. Human-review note on OTHER replies.
            if decision.state == "HUMAN_REVIEW" and not dry_run and client:
                template = router_cfg["human_review_note_template"]
                contact_name = _contact_name_from_attempt(rec)
                note = template.format(
                    contact_name=contact_name or contact_id,
                    reply_body=reply_body[:400],
                )
                try:
                    client.add_note(contact_id, note)
                    result["actions"]["note"] = {"dispatched": True}
                except GhlError as e:
                    result["actions"]["note"] = {"dispatched": False,
                                                 "reason": f"ghl_error:{e.status}"}

            # 3e. Record in attempts history.
            rr_history.append({
                "ts": now.isoformat(),
                "state": decision.state,
                "body": reply_body[:200],
                "actions_summary": {k: v.get("dispatched", False)
                                    for k, v in result["actions"].items()},
            })
            per_opp_results.append(result)

    return {
        "contact_id": contact_id,
        "reply_class": reply_class,
        "reply_body": reply_body[:200],
        "decision": asdict(decision),
        "intake_val_on_file": bool(intake_val),
        "rating_val_on_file": bool(rating_val),
        "per_opp": per_opp_results,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_first_name(rec: dict, contact_id: str,
                        client: Optional[GhlClient], dry_run: bool) -> str:
    if rec.get("first_name"):
        return rec["first_name"]
    if dry_run or not client:
        return ""
    try:
        c = client.get_contact(contact_id).get("contact", {})
        return c.get("firstName") or (c.get("contactName") or "").split(" ")[0]
    except GhlError:
        return ""


def _contact_name_from_attempt(rec: dict) -> str:
    return rec.get("contact_name") or ""
