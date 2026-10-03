"""Read-only projection of ArchOntos normalized law evidence, never a legal decision."""
import hashlib
import json
import re
from datetime import date, datetime


def project_law_evidence(metadata, *, response_bytes=None):
    """Hash supplied bytes; transport authenticity must be established upstream.

    Response is a normalized envelope, not the law.go.kr wire-format parser.
    Identity and effective date are bound to bytes, not trusted metadata alone.
    """
    if not isinstance(metadata, dict):
        raise ValueError('metadata must be an object')
    for key in ('source_repo', 'source_path', 'law_id', 'law_mst'):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f'missing {key}')
    for key, length in (('source_commit', 40), ('source_file_sha256', 64)):
        if not isinstance(metadata.get(key), str) or not re.fullmatch(r'[0-9a-f]{%d}' % length, metadata[key]):
            raise ValueError(f'invalid {key}')
    kind = metadata.get('verification_kind')
    if kind not in ('mock-fixture', 'official-api-response'):
        raise ValueError('unknown verification kind')
    try:
        effective = date.fromisoformat(metadata['effective_from'])
        observed = datetime.fromisoformat(metadata['observed_at'].replace('Z', '+00:00'))
        if observed.tzinfo is None:
            raise ValueError('observation needs timezone')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError('invalid evidence dates') from exc
    result = {key: metadata[key] for key in (
        'source_repo', 'source_commit', 'source_path', 'source_file_sha256',
        'law_id', 'law_mst', 'effective_from', 'observed_at', 'verification_kind')}
    result.update(status='NOT_RUN', response_sha256=None, execution_allowed=False,
                  canonical_allowed=False, compliance_decision='UNDETERMINED',
                  transport_authenticated=False, currently_applicable=None)
    if response_bytes is None:
        return result
    if not isinstance(response_bytes, bytes) or not response_bytes:
        raise ValueError('response must be nonempty bytes')
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate response key')
            obj[key] = value
        return obj
    try:
        response = json.loads(response_bytes, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid response JSON') from exc
    if not isinstance(response, dict):
        raise ValueError('response must be an object')
    for key in ('law_id', 'law_mst', 'effective_from'):
        if response.get(key) != metadata[key]:
            raise ValueError(f'response identity mismatch: {key}')
    if not isinstance(response.get('articles'), list) or not response['articles'] or any(
            not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text'].strip()
            for row in response['articles']):
        raise ValueError('missing article evidence')
    result['response_sha256'] = hashlib.sha256(response_bytes).hexdigest()
    result['status'] = 'FIXTURE_TESTED' if kind == 'mock-fixture' else 'RESPONSE_CAPTURED'
    # Neither byte capture nor date comparison establishes legal applicability.
    return result
