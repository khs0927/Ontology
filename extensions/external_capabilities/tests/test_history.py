from datetime import datetime, timezone
import sqlite3

import pytest
from capability_registry import evidence_record
from capability_registry.history import EvidenceHistory


def record(outcome='PASS', **changes):
    fields = dict(capability_id='read', provider_id='cad', upstream_commit='a'*40,
                  host='headless', host_version='1', adapter_version='1',
                  fixture_sha256='b'*64, verification_kind='headless', outcome=outcome,
                  run_url='https://example.test/run/1', run_timestamp='2026-10-03T00:00:00Z',
                  valid_until='2026-10-05T00:00:00Z')
    fields.update(changes)
    return evidence_record(**fields)


NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def test_persistence_conflict_supersession_and_revoke(tmp_path):
    path = tmp_path / 'evidence.db'
    history = EvidenceHistory(path)
    history.add('one', record())
    history.add('two', record('FAIL'))
    assert history.project(scope=record(), now=NOW)['status'] == 'CONFLICTED'
    history.add('three', record(), supersedes='two')
    history.close()
    history = EvidenceHistory(path)
    projection = history.project(scope=record(), now=NOW)
    assert projection['status'] == 'TESTED'
    assert projection['execution_allowed'] is False
    assert projection['canonical_allowed'] is False
    history.revoke('one', reason='fixture invalid')
    history.revoke('three', reason='fixture invalid')
    assert history.project(scope=record(), now=NOW)['status'] == 'REVOKED'
    history.close()


def test_stale_future_exact_scope(tmp_path):
    history = EvidenceHistory(tmp_path / 'evidence.db')
    history.add('one', record())
    assert history.project(scope=record(), now=datetime(2026, 10, 5, tzinfo=timezone.utc))['status'] == 'STALE'
    assert history.project(scope=record(), now=datetime(2026, 10, 2, tzinfo=timezone.utc))['status'] == 'UNKNOWN'
    assert history.project(scope=record(host_version='2'), now=NOW)['observations'] == []
    history.close()


def test_reject_invalid_scope_and_cross_scope_supersession_atomically(tmp_path):
    history = EvidenceHistory(tmp_path / 'evidence.db')
    history.add('one', record())
    head = history.project(scope=record(), now=NOW)['head_digest']
    for identifier, value, target in [('two', record(host_version='2'), 'one'),
                                      ('two', record(host_version=['bad']), None),
                                      ('one', record(), None),
                                      ('two', record(), 'missing')]:
        with pytest.raises(ValueError):
            history.add(identifier, value, supersedes=target)
        assert history.project(scope=record(), now=NOW)['head_digest'] == head
    with pytest.raises(ValueError):
        history.revoke('missing', reason='bad')
    history.close()


def test_update_delete_blocked_and_tampering_detected(tmp_path):
    path = tmp_path / 'evidence.db'
    history = EvidenceHistory(path)
    history.add('one', record())
    with sqlite3.connect(path) as other:
        for statement in ('UPDATE events SET digest="bad"', 'DELETE FROM events'):
            with pytest.raises(sqlite3.IntegrityError):
                other.execute(statement)
        other.execute('DROP TRIGGER deny_update')
        other.execute('UPDATE events SET digest="bad"')
    with pytest.raises(ValueError, match='integrity'):
        history.project(scope=record(), now=NOW)
    with pytest.raises(ValueError, match='integrity'):
        history.add('two', record())
    history.close()


def test_independent_connections_preserve_append_order(tmp_path):
    path = tmp_path / 'evidence.db'
    first, second = EvidenceHistory(path), EvidenceHistory(path)
    first.add('one', record())
    second.add('two', record('FAIL'))
    assert first.project(scope=record(), now=NOW)['status'] == 'CONFLICTED'
    first.close()
    second.close()

@pytest.mark.parametrize('outcome', ['FAIL', 'ERROR', 'SKIPPED', 'NOT_RUN'])
def test_nonpass_outcomes_never_promoted(tmp_path, outcome):
    history = EvidenceHistory(tmp_path / 'evidence.db')
    history.add('one', record(outcome))
    p = history.project(scope=record(), now=NOW)
    assert p['status'] == 'UNKNOWN'
    assert p['observations'][0]['outcome'] == outcome
    history.close()


def test_revoked_cannot_be_superseded_or_revoked_again(tmp_path):
    history = EvidenceHistory(tmp_path / 'evidence.db')
    history.add('one', record())
    history.revoke('one', reason='withdrawn')
    with pytest.raises(ValueError):
        history.add('two', record(), supersedes='one')
    with pytest.raises(ValueError):
        history.revoke('one', reason='withdrawn again')
    history.close()


def test_bounded_events_and_payload(tmp_path, monkeypatch):
    import capability_registry.history as module
    history = EvidenceHistory(tmp_path / 'evidence.db')
    with pytest.raises(ValueError, match='bound'):
        history.add('one', record(note='x' * 65536))
    history.add('one', record())
    monkeypatch.setattr(module, 'MAX_EVENTS', 1)
    with pytest.raises(ValueError, match='bound'):
        history.add('two', record())
    assert len(history.project(scope=record(), now=NOW)['observations']) == 1
    history.close()


def test_empty_history_still_requires_aware_clock(tmp_path):
    history = EvidenceHistory(tmp_path / 'evidence.db')
    with pytest.raises(ValueError, match='Clock'):
        history.project(scope=record(), now=datetime(2026, 10, 4))
    history.close()
