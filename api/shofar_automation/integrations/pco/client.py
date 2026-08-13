"""
integrations/pco/client.py

Thin PCO API client. Org-scoped: constructed with an org's decrypted
token, never reads global config. Encodes the known PCO-specific quirks
from the context seed as comments + guard code so they aren't
rediscovered the hard way on the next feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger("kryx.integrations.pco.client")

PCO_API_BASE = "https://api.planningcenteronline.com"

# error_subcode returned by Meta (not PCO itself, but relevant whenever
# PCO-sourced text is used to build a WhatsApp template variable) when a
# variable placeholder appears at the very start/end of body/header text.
META_VARIABLE_POSITION_ERROR_SUBCODE = 2388299


@dataclass
class PcoClient:
    token_id: str
    token_secret: str

    def _auth(self) -> tuple[str, str]:
        return (self.token_id, self.token_secret)

    def get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(f"{PCO_API_BASE}{path}", auth=self._auth(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def update_template(self, template_id: str, fields: dict) -> dict:
        """
        Template edits use POST /{template_id} with a NUMERIC id.
        Restricted to templates in APPROVED / REJECTED / PAUSED status —
        callers must check status before calling this, the API will
        reject otherwise. name and language are immutable post-creation:
        strip them from `fields` defensively rather than letting a caller
        error out downstream.
        """
        fields = {k: v for k, v in fields.items() if k not in ("name", "language")}
        resp = requests.post(
            f"{PCO_API_BASE}/{template_id}", auth=self._auth(), json=fields, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def campus_matches_folder(self, campus_name: str | None, folder_campus_name: str | None) -> bool:
        """
        PCO's `campus` relationship on folders is inconsistently
        populated, so campus-to-folder matching is case-insensitive name
        comparison, not id equality. Both sides may be None (unpopulated)
        — treat that as no match rather than a wildcard match, to avoid
        silently merging unrelated folders.
        """
        if not campus_name or not folder_campus_name:
            return False
        return campus_name.strip().lower() == folder_campus_name.strip().lower()
