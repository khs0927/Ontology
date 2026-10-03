from copy import deepcopy
from datetime import datetime, timezone
import pytest
from capability_registry import import_candidates, evidence_record, project_evidence, license_projection


def sample():
    return dict(capability_id='read_context', provider_id='powercad', upstream_commit='a'*40,
                host='AutoCAD', host_version='2027', adapter_version='preview',
                fixture_sha256='b'*64, verification_kind='native-live', outcome='PASS',
                run_url='https://example.test/run/1', run_timestamp='2026-10-03T00:00:00Z',
                valid_until='2026-10-04T00:00:00Z')


def test_import_does_not_launder_synthetic_scores_or_launch_commands():
    source = {'providers': [dict(id='p', upstream_url='owner/repo', commit_sha='a'*40,
                                license='MIT', score=5, pass_rate=100, command='unsafe')]}
    before = deepcopy(source)
    row = import_candidates(source, source_repo='owner/registry', source_commit='c'*40,
                            source_path='registry/providers.json', source_file_sha256='d'*64)[0]
    assert source == before
    assert not {'score', 'pass_rate', 'command'} & row.keys()
    assert row['evidence'] == [] and row['execution_allowed'] is False


def test_exact_scope_and_expiry_never_grant_execution():
    record = evidence_record(**sample())
    scope = sample()
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    assert project_evidence(record, scope=scope, now=now)['evidence_applicable']
    for key in ('upstream_commit', 'host_version', 'adapter_version', 'fixture_sha256', 'verification_kind'):
        changed = {**scope, key: 'different'}
        assert not project_evidence(record, scope=changed, now=now)['evidence_applicable']
    assert not project_evidence(record, scope=scope, now=now)['execution_allowed']
    expired = datetime(2026, 10, 4, tzinfo=timezone.utc)
    assert project_evidence(record, scope=scope, now=expired)['freshness'] == 'STALE'
    assert project_evidence({**record, 'revoked_by':'review'}, scope=scope, now=now)['freshness'] == 'REVOKED'


@pytest.mark.parametrize('outcome', ['FAIL', 'ERROR', 'SKIPPED', 'NOT_RUN'])
def test_outcomes_preserved(outcome):
    assert evidence_record(**{**sample(), 'outcome': outcome})['outcome'] == outcome


def test_license_conflict_is_not_silently_overwritten():
    observed = dict(license='MIT', upstream_commit='a'*40, source_path='LICENSE', license_file_sha256='b'*64)
    assert license_projection(['MIT','Custom'], observed)['status'] == 'CONFLICTED'
    assert license_projection(['MIT'], observed)['status'] == 'CONFIRMED'
    assert license_projection(['MIT'])['status'] == 'UNKNOWN'


def test_reject_invalid_evidence():
    with pytest.raises(ValueError):
        evidence_record(**{**sample(), 'fixture_sha256': 'bad'})
    with pytest.raises(ValueError):
        evidence_record(**{**sample(), 'run_timestamp':'2026-10-03T00:00:00'})


def test_snapshot_hash_binds_exact_bytes_without_promoting_declarations():
    import hashlib
    from capability_registry import import_snapshot
    raw = b'{"providers": [{"repo": "owner/repo", "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "score": 5}]}'
    args = dict(source_repo='owner/registry', source_commit='c'*40, source_path='providers.json')
    row = import_snapshot(raw, **args)[0]
    changed = import_snapshot(raw + b'\n', **args)[0]
    assert row['provenance']['source_file_sha256'] == hashlib.sha256(raw).hexdigest()
    assert row['provenance'] != changed['provenance']
    assert row['evidence'] == [] and row['execution_allowed'] is False
    assert 'score' not in row


@pytest.mark.parametrize('raw', [b'[]', b'{}', b'{"providers": "bad"}',
    b'{"providers": [null]}', b'{"providers": [], "providers": []}',
    b'{"providers": [{"repo": "a", "repo": "b"}]}', b'\xff', b'{', '{}'])
def test_snapshot_rejects_ambiguous_or_invalid_input(raw):
    from capability_registry import import_snapshot
    with pytest.raises(ValueError):
        import_snapshot(raw, source_repo='owner/registry', source_commit='c'*40,
                        source_path='providers.json')
