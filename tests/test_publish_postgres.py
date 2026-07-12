"""Postgres target against a fake connection: SQL shape, transactionality.

No test here touches a real server or even imports psycopg; the connection
factory is patched at the module boundary.
"""

import polars as pl
import pytest

import muster.targets.postgres as postgres_module
from muster.config import PostgresTarget
from muster.credentials import REDACTED, clear_registered_secrets, redact_text
from muster.targets.base import TargetError
from muster.targets.postgres import PostgresRuntime


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.connection.statements.append(("execute", sql))

    def executemany(self, sql, rows):
        if self.connection.fail_on_write:
            raise RuntimeError("duplicate key value violates unique constraint")
        self.connection.statements.append(("executemany", sql, list(rows)))


class FakeConnection:
    def __init__(self, fail_on_write=False):
        self.statements = []
        self.fail_on_write = fail_on_write
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False

    def close(self):
        self.closed = True


FRAME = pl.DataFrame(
    {
        "_source_file": ["a.csv", "a.csv"],
        "customer_id": ["C-1", "C-2"],
        "spend": [10.5, 20.0],
    }
)


def _runtime(keys=("customer_id",)) -> PostgresRuntime:
    spec = PostgresTarget(type="postgres", table="customers")
    return PostgresRuntime("analytics", spec, list(keys))


def test_upsert_sql_is_transactional_and_dsn_stays_secret(monkeypatch):
    dsn = "postgresql://muster:hunter2pass@db.internal/warehouse"
    monkeypatch.setenv("MUSTER_PG_DSN", dsn)
    fake = FakeConnection()
    seen = {}

    def fake_connect(value):
        seen["dsn"] = value
        return fake

    monkeypatch.setattr(postgres_module, "_connect", fake_connect)

    outcome = _runtime().publish(FRAME)

    assert seen["dsn"] == dsn
    assert outcome.rows_sent == 2 and not outcome.failures
    create = next(sql for kind, *rest in fake.statements if kind == "execute" for sql in rest)
    assert 'CREATE TABLE IF NOT EXISTS "customers"' in create
    assert 'UNIQUE ("customer_id")' in create
    kind, insert, rows = next(s for s in fake.statements if s[0] == "executemany")
    assert 'ON CONFLICT ("customer_id") DO UPDATE SET' in insert
    assert '"spend" = EXCLUDED."spend"' in insert
    assert "%s" in insert and "?" not in insert
    assert rows == [("a.csv", "C-1", 10.5), ("a.csv", "C-2", 20.0)]
    assert fake.committed and not fake.rolled_back and fake.closed

    # The DSN was registered as a secret the moment it was resolved.
    assert redact_text(f"connecting with {dsn}") == f"connecting with {REDACTED}"
    assert dsn not in _runtime().describe()


def test_a_failed_write_rolls_the_whole_publish_back(monkeypatch):
    monkeypatch.setenv("MUSTER_PG_DSN", "postgresql://muster:pw123456@db/warehouse")
    fake = FakeConnection(fail_on_write=True)
    monkeypatch.setattr(postgres_module, "_connect", lambda dsn: fake)

    with pytest.raises(TargetError, match="rolled back"):
        _runtime().publish(FRAME)
    assert fake.rolled_back and not fake.committed and fake.closed


def test_no_keys_means_full_refresh_not_upsert(monkeypatch):
    monkeypatch.setenv("MUSTER_PG_DSN", "postgresql://muster:pw123456@db/warehouse")
    fake = FakeConnection()
    monkeypatch.setattr(postgres_module, "_connect", lambda dsn: fake)

    _runtime(keys=()).publish(FRAME)

    executed = [sql for kind, sql, *_ in fake.statements]
    assert any(sql.startswith('DELETE FROM "customers"') for sql in executed)
    insert = next(s[1] for s in fake.statements if s[0] == "executemany")
    assert "ON CONFLICT" not in insert


def test_missing_dsn_fails_before_any_connection(monkeypatch):
    monkeypatch.delenv("MUSTER_PG_DSN", raising=False)
    monkeypatch.setattr("muster.credentials._from_keyring", lambda name: "")
    monkeypatch.setattr(
        postgres_module,
        "_connect",
        lambda dsn: pytest.fail("must not connect without a resolved secret"),
    )
    from muster.credentials import SecretError

    with pytest.raises(SecretError, match="MUSTER_PG_DSN"):
        _runtime().publish(FRAME)
