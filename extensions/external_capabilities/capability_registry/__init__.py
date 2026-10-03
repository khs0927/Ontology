"""Derived capability records; never an executor or canonical source store."""
from copy import deepcopy
from datetime import datetime, timezone
import re
import hashlib
import json


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Evidence timestamps require timezone')
    return stamp


def _hash(value, size):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{%d}' % size, value):
        raise ValueError('Invalid pinned digest')


def import_candidates(source, *, source_repo, source_commit, source_path, source_file_sha256):
    """Import declarations only, discarding rankings, scores and launch configuration."""
    _hash(source_commit, 40)
    _hash(source_file_sha256, 64)
    rows = source.get('providers', source.get('projects', []))
    result = []
    for item in rows:
        repo = item.get('upstream_url', item.get('repo'))
        commit = item.get('commit_sha', item.get('commit'))
        _hash(commit, 40)
        if not repo:
            raise ValueError('Missing upstream repository')
        result.append({
            'provider_id': item.get('id', repo), 'upstream_repo': repo,
            'upstream_commit': commit, 'declared_license': item.get('license', 'UNKNOWN'),
            'declared_capabilities': list(item.get('primary_capabilities', item.get('use', []))),
            'integration_policy': item.get('integration', 'reference-only'),
            'host_candidate': source.get('zwcad_target_version'),
            'provenance': {'source_repo': source_repo, 'source_commit': source_commit,
                           'source_path': source_path, 'source_file_sha256': source_file_sha256},
            'evidence': [], 'execution_allowed': False,
        })
    return result


def import_snapshot(content, *, source_repo, source_commit, source_path):
    """Bind declaration provenance to exact bytes; does not authenticate acquisition."""
    if not isinstance(content, bytes):
        raise ValueError('Snapshot must be exact bytes')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate snapshot key: ' + key)
            result[key] = value
        return result

    try:
        source = json.loads(content.decode('utf-8'), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Snapshot must be UTF-8 JSON') from exc
    if not isinstance(source, dict):
        raise ValueError('Snapshot root must be an object')
    rows = source.get('providers', source.get('projects'))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Snapshot must contain a provider or project object list')
    return import_candidates(source, source_repo=source_repo, source_commit=source_commit,
                             source_path=source_path,
                             source_file_sha256=hashlib.sha256(content).hexdigest())


def evidence_record(**fields):
    required = ('capability_id', 'provider_id', 'upstream_commit', 'host', 'host_version',
                'adapter_version', 'fixture_sha256', 'verification_kind', 'outcome',
                'run_url', 'run_timestamp', 'valid_until')
    if any(not fields.get(k) for k in required):
        raise ValueError('Incomplete verification scope')
    _hash(fields['upstream_commit'], 40)
    _hash(fields['fixture_sha256'], 64)
    if fields['verification_kind'] not in {'static', 'unit', 'simulator', 'headless', 'native-live'}:
        raise ValueError('Unknown verification kind')
    if fields['outcome'] not in {'PASS', 'FAIL', 'ERROR', 'SKIPPED', 'NOT_RUN'}:
        raise ValueError('Unknown outcome')
    if _time(fields['valid_until']) <= _time(fields['run_timestamp']):
        raise ValueError('Invalid evidence lifetime')
    return deepcopy(fields)


def project_evidence(record, *, scope, now=None):
    """Exact scope matching; even native PASS does not authorize an edit."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Clock requires timezone')
    keys = ('capability_id', 'provider_id', 'upstream_commit', 'host', 'host_version',
            'adapter_version', 'fixture_sha256', 'verification_kind')
    matches = all(scope.get(k) == record.get(k) and scope.get(k) is not None for k in keys)
    freshness = ('REVOKED' if record.get('revoked_by') else
                 'STALE' if now >= _time(record['valid_until']) else
                 'FUTURE' if now < _time(record['run_timestamp']) else 'CURRENT')
    return {'outcome': record['outcome'], 'freshness': freshness, 'scope_matches': matches,
            'evidence_applicable': matches and freshness == 'CURRENT',
            'execution_allowed': False}


def license_projection(declarations, observed=None):
    """Keep conflicting declarations visible; resolution requires hashed original evidence."""
    declared = sorted(set(declarations))
    if observed is not None:
        _hash(observed.get('license_file_sha256'), 64)
        _hash(observed.get('upstream_commit'), 40)
        if not observed.get('source_path') or not observed.get('license'):
            raise ValueError('Missing original license evidence')
    return {'declarations': declared, 'observed': deepcopy(observed),
            'status': 'CONFLICTED' if len(declared) > 1 else 'UNKNOWN' if observed is None
                      else 'CONFIRMED' if declared == [observed['license']] else 'CONFLICTED'}
