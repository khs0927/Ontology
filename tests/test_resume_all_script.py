"""Static safety checks for scripts/ops/resume-all.ps1 (PowerShell is not available in CI)."""
import re
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "ops" / "resume-all.ps1").read_text(encoding="utf-8")


def test_stop_gate_is_renamed_never_deleted():
    assert "Move-Item -LiteralPath $stopFile" in SCRIPT
    assert not re.search(r"Remove-Item[^\n]*stopFile", SCRIPT)
    assert "if ($gated -and $Resume)" in SCRIPT


def test_never_removes_data_or_volumes_or_prints_secrets():
    lowered = SCRIPT.lower()
    for forbidden in ("compose down", "volume rm", "docker rm", "remove-item", "postgres_password", "-v "):
        assert forbidden not in lowered


def test_never_touches_scheduled_tasks():
    for verb in ("Register-ScheduledTask", "Set-ScheduledTask", "Unregister-ScheduledTask", "Disable-ScheduledTask"):
        assert verb not in SCRIPT


def test_steps_and_worker_count_formula():
    assert "[math]::Min($configured, [int][math]::Floor($freeGb / $GbPerWorker))" in SCRIPT
    assert "'backfill-storey', '--batch-size', '5000'" in SCRIPT
    assert "if ($AfterDrain) { [void](Invoke-Cli @('kg-build', '--force')) }" in SCRIPT
    assert "@('ingest-stats')" in SCRIPT
