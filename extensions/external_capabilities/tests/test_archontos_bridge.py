import json
from copy import deepcopy
import hashlib
import pytest
from archontos_bridge import project_law_evidence


def sample():
    return dict(source_repo='khs0927/ArchOntos', source_commit='a'*40,
                source_path='evidence/law.json', source_file_sha256='b'*64,
                law_id='123', law_mst='456', effective_from='2026-10-01',
                observed_at='2026-10-03T12:00:00Z', verification_kind='mock-fixture')


def payload(**changes):
    return json.dumps(dict(law_id='123', law_mst='456', effective_from='2026-10-01',
                           articles=[dict(text='synthetic fixture article')], **changes)).encode()


def test_missing_response_is_not_run_even_with_official_label():
    row = project_law_evidence(dict(sample(), verification_kind='official-api-response'))
    assert row['status'] == 'NOT_RUN' and row['response_sha256'] is None


@pytest.mark.parametrize('kind,status', [('mock-fixture','FIXTURE_TESTED'), ('official-api-response','RESPONSE_CAPTURED')])
def test_capture_never_approves_compliance_or_authentication(kind, status):
    meta = dict(sample(), verification_kind=kind, canonical_allowed=True, execution_allowed=True)
    before = deepcopy(meta)
    row = project_law_evidence(meta, response_bytes=payload())
    assert meta == before and row['status'] == status
    assert row['response_sha256'] == hashlib.sha256(payload()).hexdigest()
    assert not row['canonical_allowed'] and not row['execution_allowed']
    assert not row['transport_authenticated'] and row['currently_applicable'] is None
    assert row['compliance_decision'] == 'UNDETERMINED'


@pytest.mark.parametrize('key', ['law_id', 'law_mst', 'effective_from'])
def test_rejects_wrong_observed_identity(key):
    doc = json.loads(payload()); doc[key] = 'wrong'
    with pytest.raises(ValueError):
        project_law_evidence(sample(), response_bytes=json.dumps(doc).encode())


@pytest.mark.parametrize('key,value', [('source_commit','a'), ('source_file_sha256','b'), ('source_repo',''), ('observed_at','2026-10-03T12:00:00'), ('effective_from','2026-13-01'), ('verification_kind','VERIFIED')])
def test_rejects_missing_or_invalid_provenance(key, value):
    with pytest.raises(ValueError):
        project_law_evidence(dict(sample(), **{key:value}))


@pytest.mark.parametrize('body', [b'', b'{}', b'[]', b'{"law_id":"123","law_id":"123"}', b'not-json'])
def test_rejects_absent_or_ambiguous_response(body):
    with pytest.raises(ValueError):
        project_law_evidence(sample(), response_bytes=body)
