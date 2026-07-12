"""Salesforce publish target: upsert via the REST API on an External ID.

The object, the External ID field and the canonical-to-Salesforce field map
are user configuration — Muster cannot know an org's schema, so nothing is
sent for fields left out of ``field_map``. Records go through the sObject
Collections endpoint (``PATCH /composite/sobjects/{object}/{externalId}``)
in batches of up to 200 with ``allOrNone: false``; Salesforce reports each
record individually, and every failure lands in the publish exceptions with
its Salesforce error code (e.g. ``REQUIRED_FIELD_MISSING``).

Authentication is OAuth2 — the client-credentials flow or the
username-password token flow — with every credential resolved from the
environment or OS keyring, registered for redaction, and never written
anywhere. The access token from the reply is likewise registered before use.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import Sequence

import polars as pl

from muster.config import SalesforceTarget
from muster.credentials import register_secret, resolve_secret
from muster.targets import http
from muster.targets.base import (
    PublishOutcome,
    RecordFailure,
    Target,
    TargetError,
    batched,
    iter_records,
)

logger = logging.getLogger(__name__)


class SalesforceRuntime(Target):
    def __init__(self, name: str, spec: SalesforceTarget, keys: Sequence[str]) -> None:
        super().__init__(name, keys)
        self.spec = spec
        # The canonical field whose values become the External ID; the config
        # validator guarantees exactly this mapping exists.
        self.external_source = next(
            canonical
            for canonical, sf_field in spec.field_map.items()
            if sf_field == spec.external_id_field
        )

    def describe(self) -> str:
        return (
            f"Salesforce object {self.spec.object} via {self.spec.login_url} "
            f"(upsert on {self.spec.external_id_field})"
        )

    def _authenticate(self) -> tuple[str, str]:
        """Run the configured OAuth2 flow; return (access_token, instance_url)."""
        purpose = f"salesforce target '{self.name}'"
        form = {
            "grant_type": "client_credentials",
            "client_id": resolve_secret(self.spec.client_id_env, purpose),
            "client_secret": resolve_secret(self.spec.client_secret_env, purpose),
        }
        if self.spec.auth_flow == "username_password":
            form["grant_type"] = "password"
            form["username"] = resolve_secret(self.spec.username_env, purpose)
            form["password"] = resolve_secret(self.spec.password_env, purpose)
        reply = http.request_json(
            f"{self.spec.login_url.rstrip('/')}/services/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=urllib.parse.urlencode(form).encode("utf-8"),
            timeout=self.spec.timeout_seconds,
            max_retries=self.spec.max_retries,
            description=f"salesforce token request for '{self.name}'",
        )
        if not isinstance(reply, dict) or not reply.get("access_token"):
            raise TargetError(
                f"salesforce token reply for '{self.name}' held no access token"
            )
        token = str(reply["access_token"])
        register_secret(token)
        instance = str(reply.get("instance_url") or "").rstrip("/")
        if not instance.startswith("https://"):
            # The reply is only semi-trusted; never send the bearer token
            # anywhere but an https Salesforce instance.
            raise TargetError(
                f"salesforce token reply for '{self.name}' held no https instance_url"
            )
        return token, instance

    def _to_salesforce(self, record: dict[str, object]) -> dict[str, object]:
        payload: dict[str, object] = {"attributes": {"type": self.spec.object}}
        for canonical, sf_field in self.spec.field_map.items():
            payload[sf_field] = record.get(canonical)
        return payload

    def plan(self, frame: pl.DataFrame) -> list[str]:
        batches = -(-frame.height // self.spec.batch_size) if frame.height else 0
        return [
            f"authenticate to {self.spec.login_url} with the "
            f"{self.spec.auth_flow.replace('_', '-')} flow "
            f"(credentials from environment/keyring, never from configuration)",
            f"upsert {frame.height} record(s) into {self.spec.object} on "
            f"{self.spec.external_id_field} in {batches} batch(es) of up to "
            f"{self.spec.batch_size} (allOrNone: false)",
            "record any per-record Salesforce errors in publish exceptions",
        ]

    def publish(self, frame: pl.DataFrame) -> PublishOutcome:
        token, instance = self._authenticate()
        url = (
            f"{instance}/services/data/{self.spec.api_version}"
            f"/composite/sobjects/{self.spec.object}/{self.spec.external_id_field}"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        outcome = PublishOutcome(destination=self.describe())
        sendable: list[dict[str, object]] = []
        for record in iter_records(frame):
            external_id = record.get(self.external_source)
            if external_id is None or str(external_id).strip() == "":
                outcome.failures.append(
                    RecordFailure(
                        key="",
                        code="MISSING_EXTERNAL_ID",
                        message=(
                            f"row has no value for '{self.external_source}' "
                            f"(mapped to {self.spec.external_id_field}); not sent"
                        ),
                    )
                )
                continue
            sendable.append(record)
        for batch in batched(sendable, self.spec.batch_size):
            body = json.dumps(
                {
                    "allOrNone": False,
                    "records": [self._to_salesforce(record) for record in batch],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            reply = http.request_json(
                url,
                method="PATCH",
                headers=headers,
                data=body,
                timeout=self.spec.timeout_seconds,
                max_retries=self.spec.max_retries,
                description=f"salesforce upsert for '{self.name}'",
            )
            if not isinstance(reply, list) or len(reply) != len(batch):
                raise TargetError(
                    f"salesforce reply for '{self.name}' did not report one "
                    f"result per record ({len(batch)} sent)"
                )
            for record, result in zip(batch, reply, strict=True):
                if isinstance(result, dict) and result.get("success"):
                    outcome.rows_sent += 1
                    continue
                errors = result.get("errors", []) if isinstance(result, dict) else []
                first = errors[0] if errors and isinstance(errors[0], dict) else {}
                outcome.failures.append(
                    RecordFailure(
                        key=str(record.get(self.external_source, "")),
                        code=str(first.get("statusCode", "UNKNOWN")),
                        message=str(first.get("message", "Salesforce rejected the record")),
                    )
                )
        logger.info(
            "published target=%s sent=%d failed=%d object=%s",
            self.name,
            outcome.rows_sent,
            len(outcome.failures),
            self.spec.object,
        )
        return outcome
