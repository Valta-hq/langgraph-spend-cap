"""
Thin wrapper around Valta Cap's real, documented REST API.

This is NOT the official Valta SDK. The published `valta-python-sdk`
package (checked directly against the real wheel on PyPI, version 0.1.0)
has no `.cap` resource at all -- its ValtaClient only exposes
auth/agents/wallets/policies/audit/keys/sandbox. Valta's own docs
(docs-site/concepts/cap.mdx) say the same thing: Cap support in the
Python SDK lives on a separate, unmerged branch, not in the pip release.

So this module calls the documented REST endpoints directly:
https://valta.co/docs/concepts/cap#api-reference

Auth: `x-api-key: <VALTA_API_KEY>` (Authorization: Bearer is also accepted
per the docs -- x-api-key is used here since it's what the docs lead with).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import requests

DEFAULT_BASE_URL = "https://valta.co/api/v1"


class CapError(RuntimeError):
    """Raised on a real HTTP error from the Cap API (4xx/5xx) -- never
    raised for a normal deny, which is a 200 with approved: false."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Valta Cap API error {status_code}: {body}")


class CapClient:
    def __init__(self, api_key: str, base_url: Optional[str] = None):
        if not api_key:
            raise ValueError("VALTA_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url or os.environ.get("VALTA_BASE_URL", DEFAULT_BASE_URL)

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    def _post(self, path: str, body: dict) -> dict[str, Any]:
        resp = requests.post(f"{self.base_url}{path}", json=body, headers=self._headers(), timeout=15)
        if resp.status_code >= 400:
            raise CapError(resp.status_code, resp.text)
        return resp.json()

    def allow(
        self,
        agent: str,
        run_id: str,
        estimated_usd: float,
        merchant: Optional[str] = None,
        model: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /api/v1/cap/allow -- returns the real response shape:
        {approved, id, reason?, remaining: {run, day, month}, remainingPlanUsd?}
        """
        body: dict[str, Any] = {"agent": agent, "runId": run_id, "estimatedUsd": estimated_usd}
        if merchant:
            body["merchant"] = merchant
        if model:
            body["model"] = model
        if purpose:
            body["purpose"] = purpose
        return self._post("/cap/allow", body)

    def report(self, allow_id: str, actual_usd: float) -> dict[str, Any]:
        """POST /api/v1/cap/report -- trues the ledger down from the
        estimate to the real cost. Never increases it, even if the real
        cost came in higher than estimated (see the docs for why)."""
        return self._post("/cap/report", {"allowId": allow_id, "actualUsd": actual_usd})
