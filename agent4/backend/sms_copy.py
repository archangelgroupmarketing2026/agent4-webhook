"""Agent 4 — SMS copy pack.

Legal authorization on file: MONG-ACK-2026-09-11

Every message includes:
  - Explicit sender identification ("Lonetto Law" or "Dax Lonetto's legal team")
  - {{contact.first_name}} personalization token (rendered from GHL contact.firstName)
  - "Reply STOP to opt out" suffix on sequence entry points (Touch 1, 4, 5) only

Body variants are keyed by touch identifier. The scanner renders these with
`render(touch, contact)` and never edits the base strings.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Base copy — do not edit inline; changes must go through review.
# ---------------------------------------------------------------------------

_TOUCH_1_BODY = (
    "Hello {first_name}, following up from Lonetto Law. The VA rarely grants "
    "Special Monthly Compensation automatically, leaving veterans missing out "
    "on anywhere from $135 to thousands in extra monthly benefits. Are you "
    "available for a quick 5-minute call today to see what tier your medical "
    "records support? Reply STOP to opt out."
)

_TOUCH_4_BODY = (
    "Hello {first_name}, Dax Lonetto's legal team here. If your "
    "service-connected conditions keep you housebound or require daily help "
    "with tasks like dressing or bathing, SMC can pay an extra $400 to "
    "$5,000+ per month beyond standard 100% pay. Reply YES if you'd like a "
    "quick, free file review. Reply STOP to opt out."
)

_TOUCH_5_BODY = (
    "Hello {first_name}, this is our last message from Lonetto Law regarding "
    "your VA benefits. If SMC eligibility is something you'd like us to "
    "review at no cost, just reply YES and we'll take it from there. "
    "Otherwise, no further messages. Reply STOP to opt out."
)

# ---------------------------------------------------------------------------
# Reply-router bodies. Sent by the webhook receiver in response to a
# POSITIVE reply to any touch. State chosen by inspecting the contact's
# intake_completed_at and rating_decision_uploaded_at custom fields in GHL.
# ---------------------------------------------------------------------------

_REPLY_STATE_1_INTAKE = (
    "Thanks {first_name}. To move forward, please fill out our quick VA "
    "questionnaire so Dax's team can review your case: "
    "https://tinyurl.com/bdhvvxbb - takes about 5 minutes. Once you "
    "submit, we'll follow up with next steps. Reply STOP to opt out."
)

_REPLY_STATE_2_RATING = (
    "Thanks {first_name}. We have your intake on file. Next step: upload "
    "your most recent VA rating decision letter here - "
    "https://tinyurl.com/fe4jxsm6 - so Dax's team can see what SMC tier "
    "your record supports. Reply STOP to opt out."
)

_REPLY_STATE_3_READY = (
    "Thanks {first_name}. Your file is complete and you're at the top of "
    "Dax's review queue. Someone from our team will reach out to walk "
    "through your options. Reply STOP to opt out."
)


# ---------------------------------------------------------------------------
# Reply classification — used by the inbound-SMS webhook.
# ---------------------------------------------------------------------------

# TCPA / carrier standard stop keywords. Match case-insensitively on whole-word.
STOP_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit",
                 "revoke", "optout", "opt out"}

# Positive-intent keywords that should route the lead to
# "SMC Lead - SMS Responded (High Intent)" without waiting for a human read.
POSITIVE_KEYWORDS = {"yes", "y", "yeah", "yep", "sure", "ok", "okay",
                     "please", "interested", "call me", "info"}


_ALL_BODIES = {
    "TOUCH_1": _TOUCH_1_BODY,
    "TOUCH_4": _TOUCH_4_BODY,
    "TOUCH_5": _TOUCH_5_BODY,
    "REPLY_STATE_1_INTAKE": _REPLY_STATE_1_INTAKE,
    "REPLY_STATE_2_RATING": _REPLY_STATE_2_RATING,
    "REPLY_STATE_3_READY": _REPLY_STATE_3_READY,
}


def render(touch: str, first_name: Optional[str]) -> str:
    """Return the rendered SMS body for the given touch identifier.

    Supports both outbound sequence touches (TOUCH_1/4/5) and reply-router
    responses (REPLY_STATE_1_INTAKE, REPLY_STATE_2_RATING, REPLY_STATE_3_READY).

    first_name falls back to "there" if blank, so we never send
    "Hello ," to a real veteran.
    """
    fn = (first_name or "").strip() or "there"
    if touch not in _ALL_BODIES:
        raise KeyError(f"unknown touch: {touch!r}")
    return _ALL_BODIES[touch].format(first_name=fn)


def classify_reply(body: str) -> str:
    """Classify a raw inbound SMS body.

    Returns one of: "STOP", "POSITIVE", "OTHER".
    STOP takes precedence over POSITIVE if both keywords appear.
    """
    if not body:
        return "OTHER"
    tokens = {t.strip(".,!?;:\"'") for t in body.lower().split()}
    if tokens & STOP_KEYWORDS or any(k in body.lower() for k in ("stop", "unsubscribe", "opt out")):
        return "STOP"
    if tokens & POSITIVE_KEYWORDS:
        return "POSITIVE"
    return "OTHER"


if __name__ == "__main__":
    # Quick self-check when run directly.
    for t in ("TOUCH_1", "TOUCH_4", "TOUCH_5",
              "REPLY_STATE_1_INTAKE", "REPLY_STATE_2_RATING",
              "REPLY_STATE_3_READY"):
        print(f"--- {t} ---")
        print(render(t, "Ricardo"))
        print()
    print("--- reply classification ---")
    for msg in ("YES", "Stop", "yeah please call me", "who is this?",
                "stop calling me", "please stop"):
        print(f"  {msg!r:30} -> {classify_reply(msg)}")
