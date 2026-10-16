"""Validate portable declarations. No host calls or execution authority."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import PurePosixPath

from capability_registry import _hash

IDENTITY_FIELDS = ('provider_id', 'upstream_repo', 'upstream_commit',
                   'source_path', 'adapter_version')
PROVIDERS = {'freecad', 'rhino', 'sketcharch'}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    if type(value) not in (float, int) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _decode(content):
    if not isinstance(content, bytes):
        raise ValueError('Require original UTF-8 JSON bytes')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        data = json.loads(content.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid UTF-8 JSON') from exc
    if not isinstance(data, dict):
        raise ValueError('Require object')
    return data


def _identity(data, expected):
    if not isinstance(expected, dict) or set(expected) != set(IDENTITY_FIELDS):
        raise ValueError('Require exact caller-owned expected identity')
    if any(not _text(expected[k]) for k in IDENTITY_FIELDS):
        raise ValueError('Incomplete identity')
    _hash(expected['upstream_commit'], 40)
    path = PurePosixPath(expected['source_path'])
    if path.is_absolute() or '..' in path.parts or '\\' in expected['source_path']:
        raise ValueError('Require repository-relative source path')
    if data.get('identity') != expected:
        raise ValueError('Identity mismatch')
    return deepcopy(expected)


def _base(content, identity):
    return {'identity': identity, 'payload_sha256': hashlib.sha256(content).hexdigest(),
            'status': 'DECLARED', 'verification_kind': 'contract_only',
            'execution_allowed': False, 'canonical_allowed': False,
            'native_mapping_verified': False}


def ingest_section_catalog(content, *, expected_identity, source_files):
    """Validate rows against supplied original files, not authenticity or asset coverage.

    Portable bridge schema v1 uses explicit SI-related units and 6 dimensions.
    This is an ingestion contract, not a direct reader of HS Steel .NET reports.
    """
    data = _decode(content)
    identity = _identity(data, expected_identity)
    if identity['provider_id'] != 'hs-steel-cad' or type(data.get('schema_version')) is not int or data.get('schema_version') != 1:
        raise ValueError('Unsupported catalog')
    if data.get('units') != {'dimensions': 'mm', 'unit_weight': 'kg/m', 'paint_area': 'm2/m'}:
        raise ValueError('Explicit supported units required')
    rows = data.get('rows')
    if not isinstance(rows, list) or not rows or data.get('errors') != []:
        raise ValueError('Require nonempty catalog without parse errors')
    if not isinstance(source_files, dict) or not source_files:
        raise ValueError('Require original asset bytes')
    checked = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid row')
        path, line = row.get('source_path'), row.get('line_number')
        if not _text(path):
            raise ValueError('Invalid source path')
        original = source_files.get(path)
        if not isinstance(original, bytes) or not original:
            raise ValueError('Missing nonempty source bytes')
        if type(line) is not int or line < 1 or (path, line) in seen:
            raise ValueError('Invalid or duplicate source row')
        seen.add((path, line))
        if row.get('source_file_sha256') != hashlib.sha256(original).hexdigest():
            raise ValueError('Source hash mismatch')
        if row.get('parse_success') is not True or any(not _text(row.get(k)) for k in ('designation', 'family', 'raw_value')):
            raise ValueError('Incomplete parsed row')
        dims = row.get('dimensions')
        numbers = dims + [row.get('unit_weight'), row.get('paint_area')] if isinstance(dims, list) else []
        if len(numbers) != 8 or any(not _number(v) for v in numbers):
            raise ValueError('Invalid numeric fields')
        if not any(dims) or row['unit_weight'] <= 0 or type(row.get('aci_color')) is not int or not 0 <= row['aci_color'] <= 256:
            raise ValueError('Invalid dimensions, weight or color')
        # Retain only catalog fields; discard supplied claims, commands and permissions.
        checked.append({k: deepcopy(row[k]) for k in ('source_path', 'line_number', 'source_file_sha256', 'parse_success', 'designation', 'family', 'raw_value', 'dimensions', 'unit_weight', 'paint_area', 'aci_color')})
    result = _base(content, identity)
    result.update(rows=checked, units=deepcopy(data['units']))
    return result


def ingest_readonly_probe(content, *, expected_identity):
    """Normalize an untrusted probe response; fixtures never verify a native host."""
    data = _decode(content)
    identity = _identity(data, expected_identity)
    if identity['provider_id'] not in PROVIDERS or type(data.get('schema_version')) is not int or data.get('schema_version') != 1:
        raise ValueError('Unsupported readonly provider')
    if data.get('mutation_count') != 0 or type(data.get('mutation_count')) is not int:
        raise ValueError('Require explicit zero mutation count')
    if data.get('read_only') is not True:
        raise ValueError('Require readonly response')
    capabilities = data.get('capabilities')
    allowed = {'probe', 'capabilities', 'version', 'health', 'read_context'}
    if not isinstance(capabilities, list) or any(not isinstance(k, str) or k not in allowed for k in capabilities):
        raise ValueError('Unsupported capability')
    if len(capabilities) != len(set(capabilities)):
        raise ValueError('Duplicate capability declaration')
    result = _base(content, identity)
    result.update(capabilities=sorted(capabilities), probe_authenticated=False,
                  host_identity_verified=False)
    if not _text(data.get('host')) or not _text(data.get('host_version')):
        result['status'] = 'NOT_RUN'
        result['reason'] = 'Missing host identity; native execution not established'
    else:
        host = data['host'].strip()
        if host.lower() != identity['provider_id'].lower():
            raise ValueError('Host/provider declaration mismatch')
        result.update(host=host, host_version=data['host_version'].strip())
    return result
