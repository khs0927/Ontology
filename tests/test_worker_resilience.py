"""Worker lease/finalization and ingestion timeout regression tests."""

from __future__ import annotations

import logging
from pathlib import Path

from aec_intelligence.operational import db as db_module
from aec_intelligence.operational.config import Settings
from aec_intelligence.operational.db import Database
from aec_intelligence.operational.worker import IngestionWorker


class _FakeConn:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return self


def _settings(tmp_path: Path, **kwargs):
    values = dict(dsn="dummy", data_root=tmp_path, import_roots=(tmp_path,))
    values.update(kwargs)
    return Settings(**values)


def test_database_connect_can_raise_timeout_only_for_ingest(monkeypatch):
    fake = _FakeConn()
    monkeypatch.setattr(db_module.psycopg, "connect", lambda *args, **kwargs: fake)
    db = Database("postgresql://example")

    with db.connect(statement_timeout_seconds=300):
        pass

    sql = [statement for statement, _ in fake.statements]
    assert "SET statement_timeout = '300s'" in sql
    assert "SET statement_timeout = '30s'" not in sql


def test_settings_reads_ingest_timeout_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AEC_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AEC_IMPORT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AEC_INGEST_STATEMENT_TIMEOUT_SECONDS", "420")
    assert Settings.from_env().ingest_statement_timeout_seconds == 420


class _HeartbeatDB:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def heartbeat(self, job_id, owner, lease_seconds):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_renew_lease_retries_transient_database_errors(tmp_path):
    db = _HeartbeatDB([RuntimeError("temporary"), RuntimeError("temporary"), True])
    worker = IngestionWorker(db, _settings(tmp_path), worker_id="worker-test")
    assert worker._renew_lease("job-1", retries=3, retry_delay=0)
    assert db.calls == 3


class _FinalizeDB:
    def __init__(self):
        self.finish_calls = []

    def claim(self, **kwargs):
        return {"id": "job-1", "payload": {}}

    def heartbeat(self, *args, **kwargs):
        return True

    def finish(self, job_id, owner, result=None, error=None):
        self.finish_calls.append({"job_id": job_id, "owner": owner, "result": result, "error": error})
        return False


class _FastWorker(IngestionWorker):
    def process_job(self, job):
        return {"status": "SUCCESS"}


def test_finish_failure_is_not_logged_as_job_success(tmp_path, caplog):
    db = _FinalizeDB()
    worker = _FastWorker(db, _settings(tmp_path), worker_id="worker-test")
    caplog.set_level(logging.INFO)

    assert worker.run_once() is True

    assert len(db.finish_calls) == 1
    assert db.finish_calls[0]["error"] is None
    assert "could not finalize its lease" in caplog.text
    assert "Job job-1 succeeded" not in caplog.text
