"""Generic REST publish target: POST the dataset as batches of JSON records.

Each request body is ``{"records": [...]}`` with dates and datetimes as ISO
strings. Authentication is a bearer token or an API key header, resolved
from the environment or OS keyring — never from configuration. Transient
failures are retried with exponential backoff and jitter, and 429 replies
are honoured (see :mod:`muster.targets.http`).

Idempotency is the endpoint's half of the contract: every record carries
the configured key columns, and a retried batch is resent byte-for-byte, so
an endpoint that upserts on those keys can deduplicate safely. This is
documented in docs/CONNECTORS.md.

If a batch still fails after retries, its rows are recorded as failures,
remaining batches are not attempted (the failure is unlikely to be
batch-specific), and the publish is reported as partial or failed.
"""

from __future__ import annotations

import json
import logging
from typing import Sequence

import polars as pl

from muster.config import RestTarget
from muster.credentials import resolve_secret
from muster.targets import http
from muster.targets.base import (
    PublishOutcome,
    RecordFailure,
    Target,
    TargetError,
    batched,
    iter_records,
    key_of,
)

logger = logging.getLogger(__name__)


class RestRuntime(Target):
    def __init__(self, name: str, spec: RestTarget, keys: Sequence[str]) -> None:
        super().__init__(name, keys)
        self.spec = spec

    def describe(self) -> str:
        return f"REST endpoint {self.spec.url} ({self.spec.auth} auth)"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.spec.auth == "bearer":
            token = resolve_secret(self.spec.token_env, f"rest target '{self.name}'")
            headers["Authorization"] = f"Bearer {token}"
        elif self.spec.auth == "api_key":
            token = resolve_secret(self.spec.token_env, f"rest target '{self.name}'")
            headers[self.spec.api_key_header] = token
        return headers

    def plan(self, frame: pl.DataFrame) -> list[str]:
        batches = -(-frame.height // self.spec.batch_size) if frame.height else 0
        auth = {
            "bearer": f"a bearer token from {self.spec.token_env}",
            "api_key": f"an API key from {self.spec.token_env} in '{self.spec.api_key_header}'",
            "none": "no authentication",
        }[self.spec.auth]
        return [
            f"POST to {self.spec.url} with {auth}",
            f"send {frame.height} record(s) in {batches} batch(es) of up to "
            f"{self.spec.batch_size}, retrying transient failures up to "
            f"{self.spec.max_retries} time(s) with backoff",
        ]

    def publish(self, frame: pl.DataFrame) -> PublishOutcome:
        headers = self._headers()
        records = list(iter_records(frame))
        outcome = PublishOutcome(destination=self.describe())
        batches = list(batched(records, self.spec.batch_size))
        for index, batch in enumerate(batches):
            body = json.dumps({"records": batch}, ensure_ascii=False).encode("utf-8")
            try:
                http.request_json(
                    self.spec.url,
                    headers=headers,
                    data=body,
                    timeout=self.spec.timeout_seconds,
                    max_retries=self.spec.max_retries,
                    description=f"rest target '{self.name}' batch {index + 1} of {len(batches)}",
                )
            except TargetError as exc:
                reason = str(exc)
                logger.error("rest target %s stopped: %s", self.name, reason)
                for unsent in batches[index:]:
                    for record in unsent:
                        outcome.failures.append(
                            RecordFailure(key=key_of(record, self.keys), code="http", message=reason)
                        )
                return outcome
            outcome.rows_sent += len(batch)
        return outcome
