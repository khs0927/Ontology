from datetime import datetime, timezone
import pytest
from context_fabric.contracts import SourceRevision, verify_bound_live_candidate


def inputs():
    candidate = {'id':'c', 'locator':dict(source_id='s', layout='Model', handle='A', instance_path=[]),
                 'source':dict(revision='1', sha256='a'*64, format='dwg', units='mm')}
    live = dict(candidate['locator'], revision='1', sha256='a'*64, units='mm',
            native_mapping_verified=True, document_dirty=False, fingerprint='native',
            document_id='session-1', state_digest='b'*64, observed_at='2026-10-03T10:00:00Z')
    return candidate, live, dict(document_id='session-1', state_digest='b'*64)


def check(patch=None, binding=None):
    c, live, b = inputs()
    return verify_bound_live_candidate(c, {**live, **(patch or {})}, b if binding is None else binding,
                                      now=datetime(2026,10,3,10,0,5,tzinfo=timezone.utc))


def test_fresh_binding_is_review_only():
    assert check()['status'] == 'VERIFIED_FOR_REVIEW'
    assert check()['may_execute_mutation'] is False


@pytest.mark.parametrize('patch', [dict(document_id='reopened'), dict(state_digest='c'*64),
    dict(document_dirty=True), dict(observed_at='2026-10-03T09:59:00Z'),
    dict(observed_at='2026-10-03T10:01:00Z'), dict(observed_at='2026-10-03T10:00:00'),
    dict(observed_at=None), dict(sha256='c'*64), dict(instance_path=['different'])])
def test_stale_wrong_session_or_wrong_source_rejected(patch):
    assert check(patch)['status'] == 'REQUIRES_REVIEW'
    assert check(patch)['fingerprint'] is None


def test_missing_binding_rejected():
    assert check(binding={})['status'] == 'REQUIRES_REVIEW'


def test_content_revision_independent_of_parser():
    fields = dict(account='a', corpus='c', file_id='f', project_id='p', revision='1',
                  sha256='a'*64, name='drawing', format='dwg', parser='oda', parser_version='1', units='mm')
    first = SourceRevision(**fields)
    second = SourceRevision(**{**fields, 'parser_version':'2'})
    assert first.content_revision_id == second.content_revision_id
    assert first.revision_id != second.revision_id


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), 0, -1, 31, True])
def test_invalid_lifetime_rejected(ttl):
    c, live, binding = inputs()
    with pytest.raises(ValueError):
        verify_bound_live_candidate(c, live, binding,
            now=datetime(2026,10,3,10,0,5,tzinfo=timezone.utc), max_age_seconds=ttl)
