"""Graceful drain of host workers (stop file / orphaned children). No DB."""

from types import SimpleNamespace

from aec_intelligence.operational import census


def test_stop_file_and_env_override(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_root=tmp_path)
    monkeypatch.delenv("AEC_WORKER_STOP_FILE", raising=False)
    assert census.worker_stop_file(settings) == tmp_path / "bulk" / "STOP-WORKERS"
    assert census.worker_stop_reason(settings) is None
    (tmp_path / "bulk").mkdir()
    (tmp_path / "bulk" / "STOP-WORKERS").write_text("x")
    assert census.worker_stop_reason(settings) == "stop file present"
    other = tmp_path / "custom.stop"
    monkeypatch.setenv("AEC_WORKER_STOP_FILE", str(other))
    assert census.worker_stop_reason(settings) is None
    other.write_text("x")
    assert census.worker_stop_reason(settings) == "stop file present"


def test_orphaned_child_stops(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_root=tmp_path)
    monkeypatch.delenv("AEC_WORKER_STOP_FILE", raising=False)
    monkeypatch.setattr(census.mp, "parent_process", lambda: SimpleNamespace(is_alive=lambda: False))
    assert census.worker_stop_reason(settings) == "parent process exited"
    monkeypatch.setattr(census.mp, "parent_process", lambda: SimpleNamespace(is_alive=lambda: True))
    assert census.worker_stop_reason(settings) is None


def test_worker_process_exits_before_claiming(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_root=tmp_path, dsn="postgresql://unused")
    (tmp_path / "bulk").mkdir()
    (tmp_path / "bulk" / "STOP-WORKERS").write_text("x")
    monkeypatch.delenv("AEC_WORKER_STOP_FILE", raising=False)
    claimed = []

    class FakeWorker:
        def __init__(self, *a, **k):
            pass

        def run_once(self):
            claimed.append(1)
            return True

    import aec_intelligence.operational.worker as worker_mod

    monkeypatch.setattr(worker_mod, "IngestionWorker", FakeWorker)
    assert census.worker_process(settings, "cad", 0.01, 0) == 0
    assert claimed == []
