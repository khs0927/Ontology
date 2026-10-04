"""Exercise the real PowerShell drill without Docker or a live database."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


SHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(not SHELL, reason="PowerShell is required for the ops integration test")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "restore-drill.ps1"


def run_drill(tmp_path, case):
    dump = tmp_path / "fixture.dump"
    dump.write_bytes(b"not a real dump: Docker is mocked")
    report = tmp_path / "report.json"
    calls = tmp_path / "calls.txt"
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(
        r'''
function global:docker {
    $a = @($args)
    Add-Content -LiteralPath $env:DRILL_CALLS -Value ($a -join ' ')
    $global:LASTEXITCODE = 0
    $joined = $a -join ' '
    if ($joined -match 'pg_database') {
        if ($env:DRILL_CASE -eq 'existing') { '1' }
        return
    }
    if ($joined -match 'pg_restore') {
        if ($env:DRILL_CASE -eq 'silent_failure') { $global:LASTEXITCODE = 1 }
        return
    }
    if ($joined -match 'FROM pg_indexes') {
        $different = ($joined -notmatch '-d aec ') -and $env:DRILL_CASE -eq 'index_definition'
        if ($different) { '{"name":"idx","definition":"changed","valid":false,"ready":true}' }
        elseif ($env:DRILL_CASE -eq 'invalid_both') { '{"name":"idx","definition":"original","valid":false,"ready":true}' }
        else { '{"name":"idx","definition":"original","valid":true,"ready":true}' }
        return
    }
    if ($joined -match 'FROM pg_constraint') {
        if (($joined -notmatch '-d aec ') -and $env:DRILL_CASE -eq 'constraint') {
            'constraint:changed'
        } else { 'constraint:original' }
        'extension:age-1.6.0'
        return
    }
    if ($joined -match 'information_schema.tables') { 'aec.documents=3'; return }
}
& $env:DRILL_SCRIPT -Dump $env:DRILL_DUMP -Scratch aec_test_drill -Report $env:DRILL_REPORT -KeepScratch
exit $LASTEXITCODE
''',
        encoding="utf-8",
    )
    env = dict(os.environ, DRILL_CASE=case, DRILL_SCRIPT=str(SCRIPT), DRILL_DUMP=str(dump),
               DRILL_REPORT=str(report), DRILL_CALLS=str(calls))
    result = subprocess.run([SHELL, "-NoProfile", "-File", str(wrapper)], env=env,
                            capture_output=True, timeout=30)
    data = json.loads(report.read_text(encoding="utf-8-sig")) if report.exists() else None
    return result, data, calls.read_text(encoding="utf-8-sig")


def test_success_requires_successful_restore_and_same_structure(tmp_path):
    result, report, _ = run_drill(tmp_path, "success")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert report["result"] == "MATCH"
    assert report["restore_exit"] == 0


@pytest.mark.parametrize("case", ["silent_failure", "index_definition", "constraint", "invalid_both"])
def test_failure_cannot_be_hidden_by_matching_row_counts(tmp_path, case):
    result, report, _ = run_drill(tmp_path, case)
    assert result.returncode != 0
    assert report["result"] == "DIFF"
    assert report["differing_tables"] == []


def test_existing_scratch_is_never_dropped_or_replaced(tmp_path):
    result, report, calls = run_drill(tmp_path, "existing")
    assert result.returncode != 0
    assert report is None
    assert "dropdb" not in calls
    assert "createdb" not in calls
