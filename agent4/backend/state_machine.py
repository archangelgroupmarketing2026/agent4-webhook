"""Agent 4 — 6-touch state machine.

Legal authorization on file: MONG-ACK-2026-09-11

Determines the next action for a lead based on:
  - Current pipeline stage
  - Time since last touch
  - Reply state (from GHL inbound-SMS webhook)
  - Call outcome (from Retell post-call webhook)
  - Global guardrails (window, caps, kill switch — checked by scanner, not here)

Sequence (as approved by user 2026-09-16):

  Touch 1: SMS on entry into New Lead
           -> Contact Attempt 1 - SMS Sent
           -> Wait 2h for reply
  Touch 2a: Reply within 2h -> SMC Lead - SMS Responded (High Intent), STOP sequence
  Touch 2b: No reply after 2h -> DIAL (Michelle), inside calling window only
           -> Call Scheduled / Dialing
  Touch 3: If Dial 1 ring-no-answer/busy/no-connect (NOT voicemail),
           wait 10min -> DIAL 2 (double-tap)
  Touch 4: 24h after last call with no connect -> SMS retry
           -> Contact Attempt 2 - Retry
           -> Wait 24h for reply
  Touch 5: 48h after Touch 4 with no reply -> SMS final
           -> Contact Attempt 3 - Final
           -> Wait 48h for reply
  Touch 6: 48h after Touch 5 with no reply -> Do Not Contact / Dead

  Any STOP reply at any point -> Do Not Contact / Dead
  Any positive reply -> SMC Lead - SMS Responded (High Intent)
  Michelle-books -> Appointment Booked
  Michelle-qualifies -> SMC Qualified - Schedule Consult
  Michelle-disqualifies -> Not Qualified / Archived
  Michelle-callback -> Call Back Later (re-enters queue at scheduled time)

Attempt-tracker record shape (agent4/logs/attempts.json):

    {
      "<opp_id>": {
        "opp_id": "...",
        "contact_id": "...",
        "current_stage_id": "...",
        "last_touch": "TOUCH_1" | "DIAL_1" | "DIAL_2" | "TOUCH_4" | "TOUCH_5" | null,
        "last_touch_ts": "2026-09-16T14:30:00+00:00",
        "awaiting_reply_until": "2026-09-16T16:30:00+00:00" | null,
        "reply_state": "NONE" | "STOP" | "POSITIVE" | "OTHER",
        "reply_ts": null | "..." ,
        "last_call_outcome": null | "ANSWERED" | "VOICEMAIL" | "NO_ANSWER" | "BUSY" | "FAILED",
        "call_count": 0,
        "history": [ {ts, action, run_id, ...}, ... ]
      }
    }

Only the scanner writes to attempts.json. Webhooks post events, scanner reconciles.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Sequence timing (single source of truth)
# ---------------------------------------------------------------------------

SMS_REPLY_WAIT_HOURS = 2         # Touch 1 -> Dial 1
DOUBLE_TAP_MINUTES = 10          # Dial 1 -> Dial 2
POST_CALL_TO_RETRY_HOURS = 24    # Dial 2 -> Touch 4
TOUCH_4_REPLY_WAIT_HOURS = 24    # Touch 4 -> Touch 5
TOUCH_5_REPLY_WAIT_HOURS = 48    # Touch 5 -> Do Not Contact

# Call outcomes that trigger the double-tap. Any other outcome exits the
# double-tap branch and either routes to Michelle disposition or waits for
# Touch 4.
DOUBLE_TAP_TRIGGER_OUTCOMES = {"NO_ANSWER", "BUSY", "FAILED"}


# ---------------------------------------------------------------------------
# Action shape
# ---------------------------------------------------------------------------


@dataclass
class Action:
    """A single decision the scanner should carry out for a lead."""
    kind: str            # "SMS" | "DIAL" | "MOVE_STAGE" | "SKIP" | "SUPPRESS"
    reason: str
    sms_touch: Optional[str] = None      # TOUCH_1 | TOUCH_4 | TOUCH_5 (kind==SMS)
    target_stage: Optional[str] = None   # stage NAME (kind in SMS,DIAL,MOVE_STAGE,SUPPRESS)
    call_number: Optional[int] = None    # 1 or 2 (kind==DIAL)


# ---------------------------------------------------------------------------
# Attempt record helpers
# ---------------------------------------------------------------------------


def new_attempt_record(opp_id: str, contact_id: Optional[str],
                       current_stage_id: str) -> dict:
    return {
        "opp_id": opp_id,
        "contact_id": contact_id,
        "current_stage_id": current_stage_id,
        "last_touch": None,
        "last_touch_ts": None,
        "awaiting_reply_until": None,
        "reply_state": "NONE",
        "reply_ts": None,
        "last_call_outcome": None,
        "call_count": 0,
        "history": [],
    }


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Stage-name shortcuts (match config.target_pipeline.stages keys)
# ---------------------------------------------------------------------------

S_NEW_LEAD = "New Lead"
S_DIALING = "Call Scheduled / Dialing"
S_ATT1_SMS = "Contact Attempt 1 - SMS Sent"
S_ATT2_RETRY = "Contact Attempt 2 - Retry"
S_ATT3_FINAL = "Contact Attempt 3 - Final"
S_CALLBACK = "Call Back Later"
S_HIGH_INTENT = "SMC Lead - SMS Responded (High Intent)"
S_QUALIFIED = "SMC Qualified - Schedule Consult"
S_APPT_BOOKED = "Appointment Booked"
S_CASE_ACCEPTED = "Case Accepted"
S_CASE_REJECTED = "Case Rejected"
S_NOT_QUALIFIED = "Not Qualified / Archived"
S_DNC = "Do Not Contact / Dead"
S_BACKLOG_AGED = "Backlog Aged (>90d, Manual)"

# Stages the state machine will not touch — closed states, aged, booked, etc.
TERMINAL_STAGES = {
    S_HIGH_INTENT, S_QUALIFIED, S_APPT_BOOKED, S_CASE_ACCEPTED,
    S_CASE_REJECTED, S_NOT_QUALIFIED, S_DNC, S_BACKLOG_AGED, S_CALLBACK,
}


# ---------------------------------------------------------------------------
# Core decision
# ---------------------------------------------------------------------------


def decide_next_action(
    *,
    stage_name: str,
    attempt: dict,
    now: datetime,
    has_phone: bool,
    age_days: Optional[float],
    dial_age_cutoff_days: int,
    calling_window_open: bool,
    dialing_paused: bool,
    sms_paused: bool,
) -> Action:
    """Return the next action for a single lead based on state.

    The scanner is responsible for enforcing:
      - kill switch
      - daily caps (dial_cap, sms_cap)
      - pace between dials

    This function is responsible for the sequence logic and only the
    sequence logic — given a lead's current state, what should happen next.
    """
    # ---- Terminal / off-ramp checks (highest priority) --------------------

    if stage_name in TERMINAL_STAGES:
        return Action(kind="SKIP",
                      reason=f"terminal_stage:{stage_name}")

    if attempt.get("reply_state") == "STOP":
        return Action(kind="SUPPRESS",
                      reason="reply_STOP_received",
                      target_stage=S_DNC)

    if attempt.get("reply_state") == "POSITIVE":
        return Action(kind="MOVE_STAGE",
                      reason="reply_POSITIVE_received",
                      target_stage=S_HIGH_INTENT)

    if not has_phone:
        return Action(kind="SKIP", reason="no_phone_on_contact")

    if age_days is not None and age_days > dial_age_cutoff_days:
        return Action(kind="MOVE_STAGE",
                      reason=f"age>{dial_age_cutoff_days}d",
                      target_stage=S_BACKLOG_AGED)

    last_touch = attempt.get("last_touch")
    last_touch_ts = _parse(attempt.get("last_touch_ts"))
    awaiting_until = _parse(attempt.get("awaiting_reply_until"))
    last_outcome = attempt.get("last_call_outcome")
    call_count = int(attempt.get("call_count") or 0)

    # ---- Stage: New Lead -> send Touch 1 SMS -----------------------------

    if stage_name == S_NEW_LEAD:
        if sms_paused:
            return Action(kind="SKIP", reason="sms_paused")
        return Action(kind="SMS",
                      reason="entry:new_lead -> send TOUCH_1",
                      sms_touch="TOUCH_1",
                      target_stage=S_ATT1_SMS)

    # ---- Stage: Contact Attempt 1 - SMS Sent -----------------------------

    if stage_name == S_ATT1_SMS:
        # Waiting for reply?
        if awaiting_until and now < awaiting_until:
            return Action(kind="SKIP",
                          reason=f"awaiting_reply_until:{awaiting_until.isoformat()}")
        # Reply window expired -> dial 1
        if dialing_paused:
            return Action(kind="SKIP", reason="dialing_paused")
        if not calling_window_open:
            return Action(kind="SKIP", reason="outside_calling_window")
        return Action(kind="DIAL",
                      reason="reply_window_expired -> Dial 1",
                      call_number=1,
                      target_stage=S_DIALING)

    # ---- Stage: Call Scheduled / Dialing ---------------------------------

    if stage_name == S_DIALING:
        # Michelle just answered / disposition should have moved the lead.
        # If we see the lead still here with call_count==1 and a trigger
        # outcome, double-tap.
        if call_count == 1 and last_outcome in DOUBLE_TAP_TRIGGER_OUTCOMES:
            # Ten-minute gap
            if last_touch_ts is not None:
                gap = now - last_touch_ts
                if gap < timedelta(minutes=DOUBLE_TAP_MINUTES):
                    return Action(kind="SKIP",
                                  reason=f"double_tap_gap:{DOUBLE_TAP_MINUTES}min_not_elapsed")
            if dialing_paused:
                return Action(kind="SKIP", reason="dialing_paused")
            if not calling_window_open:
                return Action(kind="SKIP", reason="outside_calling_window")
            return Action(kind="DIAL",
                          reason=f"double_tap after {last_outcome}",
                          call_number=2,
                          target_stage=S_DIALING)

        # Voicemail on Dial 1 -> no double-tap, wait for Touch 4 window
        if call_count == 1 and last_outcome == "VOICEMAIL":
            if last_touch_ts is not None and (now - last_touch_ts) >= timedelta(hours=POST_CALL_TO_RETRY_HOURS):
                if sms_paused:
                    return Action(kind="SKIP", reason="sms_paused")
                return Action(kind="SMS",
                              reason="voicemail_left + 24h -> TOUCH_4",
                              sms_touch="TOUCH_4",
                              target_stage=S_ATT2_RETRY)
            return Action(kind="SKIP",
                          reason="voicemail_left, awaiting 24h -> TOUCH_4")

        # Both calls done, no connect -> Touch 4 after 24h
        if call_count >= 2 and last_outcome in DOUBLE_TAP_TRIGGER_OUTCOMES:
            if last_touch_ts is not None and (now - last_touch_ts) >= timedelta(hours=POST_CALL_TO_RETRY_HOURS):
                if sms_paused:
                    return Action(kind="SKIP", reason="sms_paused")
                return Action(kind="SMS",
                              reason="post_double_tap + 24h -> TOUCH_4",
                              sms_touch="TOUCH_4",
                              target_stage=S_ATT2_RETRY)
            return Action(kind="SKIP",
                          reason=f"awaiting {POST_CALL_TO_RETRY_HOURS}h post-call before TOUCH_4")

        # No outcome recorded yet (Retell webhook hasn't posted) -> wait
        return Action(kind="SKIP",
                      reason=f"awaiting_call_outcome (call_count={call_count})")

    # ---- Stage: Contact Attempt 2 - Retry --------------------------------

    if stage_name == S_ATT2_RETRY:
        if awaiting_until and now < awaiting_until:
            return Action(kind="SKIP",
                          reason=f"awaiting_reply_until:{awaiting_until.isoformat()}")
        if sms_paused:
            return Action(kind="SKIP", reason="sms_paused")
        return Action(kind="SMS",
                      reason=f"TOUCH_4 window expired -> TOUCH_5",
                      sms_touch="TOUCH_5",
                      target_stage=S_ATT3_FINAL)

    # ---- Stage: Contact Attempt 3 - Final --------------------------------

    if stage_name == S_ATT3_FINAL:
        if awaiting_until and now < awaiting_until:
            return Action(kind="SKIP",
                          reason=f"awaiting_reply_until:{awaiting_until.isoformat()}")
        return Action(kind="MOVE_STAGE",
                      reason="TOUCH_5 window expired -> Do Not Contact",
                      target_stage=S_DNC)

    # ---- Unknown stage — do not touch ------------------------------------

    return Action(kind="SKIP", reason=f"unknown_stage:{stage_name}")


# ---------------------------------------------------------------------------
# Bookkeeping helpers (called by scanner after an action is taken)
# ---------------------------------------------------------------------------


def wait_until_for_sms(touch: str, sent_at: datetime) -> Optional[datetime]:
    """Return the ISO datetime we should stop waiting for a reply for the
    given SMS touch. Returns None for Touch 5's terminal wait (still tracked,
    but the scanner treats hitting the deadline as a move to DNC).
    """
    hours = {
        "TOUCH_1": SMS_REPLY_WAIT_HOURS,
        "TOUCH_4": TOUCH_4_REPLY_WAIT_HOURS,
        "TOUCH_5": TOUCH_5_REPLY_WAIT_HOURS,
    }.get(touch)
    if hours is None:
        return None
    return sent_at + timedelta(hours=hours)


def record_sms_sent(attempt: dict, touch: str, sent_at: datetime,
                    run_id: str) -> None:
    attempt["last_touch"] = touch
    attempt["last_touch_ts"] = sent_at.isoformat()
    wu = wait_until_for_sms(touch, sent_at)
    attempt["awaiting_reply_until"] = wu.isoformat() if wu else None
    attempt.setdefault("history", []).append({
        "ts": sent_at.isoformat(), "action": "SMS_SENT",
        "touch": touch, "run_id": run_id,
    })


def record_dial_dispatched(attempt: dict, call_number: int,
                           dispatched_at: datetime, run_id: str) -> None:
    attempt["last_touch"] = f"DIAL_{call_number}"
    attempt["last_touch_ts"] = dispatched_at.isoformat()
    attempt["awaiting_reply_until"] = None
    attempt["call_count"] = call_number
    attempt.setdefault("history", []).append({
        "ts": dispatched_at.isoformat(), "action": "DIAL_DISPATCHED",
        "call_number": call_number, "run_id": run_id,
    })


def record_reply(attempt: dict, reply_class: str, body: str,
                 received_at: datetime) -> None:
    attempt["reply_state"] = reply_class
    attempt["reply_ts"] = received_at.isoformat()
    attempt["awaiting_reply_until"] = None
    attempt.setdefault("history", []).append({
        "ts": received_at.isoformat(), "action": "REPLY_RECEIVED",
        "reply_class": reply_class, "body": body[:400],
    })


def record_call_outcome(attempt: dict, outcome: str,
                        received_at: datetime) -> None:
    attempt["last_call_outcome"] = outcome
    attempt.setdefault("history", []).append({
        "ts": received_at.isoformat(), "action": "CALL_OUTCOME",
        "outcome": outcome,
    })
