"""GHL LeadConnector v2 client — portable, requests-backed.

Authenticates with a Private Integration Token (PIT) from the sub-account
level (Location Settings -> Private Integrations in GHL). Set the token in
the environment as GHL_PIT before instantiating the client.

    Authorization: Bearer <PIT>
    Version: 2021-07-28
    Accept: application/json

Docs:
- https://marketplace.gohighlevel.com/docs/oauth/SandboxPIT/
- https://help.gohighlevel.com/support/solutions/articles/155000003054
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://services.leadconnectorhq.com"
VERSION = "2021-07-28"
DEFAULT_TIMEOUT = 30

log = logging.getLogger("ghl_client")


class GhlError(RuntimeError):
    def __init__(self, status: int, body: Any, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"GHL {status} on {url}: {str(body)[:400]}")


class GhlAuthError(RuntimeError):
    """Raised when the client is instantiated without a token."""


def _build_session() -> requests.Session:
    """Requests session with modest retry on transient failures."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10,
                          pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class GhlClient:
    def __init__(self, location_id: str, *,
                 token: Optional[str] = None,
                 timeout: int = DEFAULT_TIMEOUT):
        self.location_id = location_id
        self.timeout = timeout
        self.token = token or os.environ.get("GHL_PIT")
        if not self.token:
            raise GhlAuthError(
                "GHL_PIT environment variable is not set. "
                "Create a Private Integration Token in GHL "
                "(Sub-account Settings -> Private Integrations) "
                "and set it as GHL_PIT.")
        self._session = _build_session()

    # ---- low-level ------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Version": VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *,
                 params: dict | None = None,
                 json_body: dict | None = None) -> Any:
        url = BASE + path
        try:
            resp = self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise GhlError(-1, str(e), path) from e

        body_text = resp.text
        if resp.status_code >= 400:
            raise GhlError(resp.status_code, _parse(body_text), path)
        return _parse(body_text)

    # ---- pipelines & opportunities -------------------------------------

    def list_pipelines(self) -> List[dict]:
        d = self._request("GET", "/opportunities/pipelines",
                          params={"locationId": self.location_id})
        return d.get("pipelines", []) if isinstance(d, dict) else []

    def search_opportunities(self, *, pipeline_id: str,
                             pipeline_stage_id: Optional[str] = None,
                             limit: int = 100) -> Iterator[dict]:
        params: Dict[str, Any] = {
            "location_id": self.location_id,
            "pipeline_id": pipeline_id,
            "limit": limit,
        }
        if pipeline_stage_id:
            params["pipeline_stage_id"] = pipeline_stage_id

        next_start_after: Optional[int] = None
        next_start_after_id: Optional[str] = None
        page = 0
        while True:
            page += 1
            page_params = dict(params)
            if next_start_after is not None:
                page_params["startAfter"] = next_start_after
                page_params["startAfterId"] = next_start_after_id
            d = self._request("GET", "/opportunities/search",
                              params=page_params)
            if not isinstance(d, dict):
                break
            opps = d.get("opportunities", [])
            for o in opps:
                yield o
            meta = d.get("meta", {}) or {}
            log.info("page %s: %s opps, total=%s",
                     page, len(opps), meta.get("total"))
            if not meta.get("nextPageUrl"):
                break
            next_start_after = meta.get("startAfter")
            next_start_after_id = meta.get("startAfterId")
            time.sleep(0.15)

    def update_opportunity(self, opp_id: str, *, pipeline_id: str,
                            pipeline_stage_id: str,
                            status: Optional[str] = None,
                            name: Optional[str] = None) -> dict:
        body: Dict[str, Any] = {
            "pipelineId": pipeline_id,
            "pipelineStageId": pipeline_stage_id,
        }
        if status:
            body["status"] = status
        if name:
            body["name"] = name
        return self._request("PUT", f"/opportunities/{opp_id}",
                             json_body=body)

    # ---- contacts, notes, messaging ------------------------------------

    def get_contact(self, contact_id: str) -> dict:
        return self._request("GET", f"/contacts/{contact_id}")

    def update_contact(self, contact_id: str, body: dict) -> dict:
        body = dict(body)
        body.setdefault("locationId", self.location_id)
        return self._request("PUT", f"/contacts/{contact_id}",
                             json_body=body)

    def add_note(self, contact_id: str, body_text: str,
                 user_id: Optional[str] = None) -> dict:
        payload: Dict[str, Any] = {"body": body_text}
        if user_id:
            payload["userId"] = user_id
        return self._request("POST",
                             f"/contacts/{contact_id}/notes",
                             json_body=payload)

    def send_sms(self, contact_id: str, message: str) -> dict:
        payload = {
            "type": "SMS",
            "contactId": contact_id,
            "message": message,
        }
        return self._request("POST", "/conversations/messages",
                             json_body=payload)

    # ---- custom-field helpers ------------------------------------------

    def get_contact_custom_field(self, contact_id: str,
                                 field_id: str) -> Optional[Any]:
        """Read a single custom-field value from a GHL contact by field ID.

        Returns None if the field is missing, empty, or the contact record
        doesn't include it. Callers should treat None as "not filled out".
        """
        if not field_id or field_id.startswith("TODO_"):
            return None
        contact = self.get_contact(contact_id).get("contact", {})
        for cf in contact.get("customFields", []) or []:
            if cf.get("id") == field_id:
                v = cf.get("value") or cf.get("fieldValue")
                if v in (None, "", []):
                    return None
                return v
        return None

    def update_contact_custom_field(self, contact_id: str,
                                    field_id: str,
                                    value: Any) -> dict:
        """Set a single custom-field value on a GHL contact.

        Returns the raw update response, or a stub dict if the field ID
        is a TODO placeholder (so tests can run without real IDs).
        """
        if not field_id or field_id.startswith("TODO_"):
            return {"skipped": True,
                    "reason": f"placeholder_field_id:{field_id}"}
        body = {"customFields": [{"id": field_id, "value": value}]}
        return self.update_contact(contact_id, body)


def _parse(body: str) -> Any:
    body = body.strip()
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body
