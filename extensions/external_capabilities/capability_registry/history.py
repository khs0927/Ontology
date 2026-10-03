"""Single local SQLite evidence history; producer authentication is out of scope."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3

from . import evidence_record, project_evidence

SCOPE_KEYS = ('capability_id', 'provider_id', 'upstream_commit', 'host', 'host_version',
              'adapter_version', 'fixture_sha256', 'verification_kind')
MAX_EVENTS = 10000
MAX_EVENT_BYTES = 65536


def _scope(record):
    if any(not isinstance(record.get(k), str) or not record[k].strip() for k in SCOPE_KEYS):
        raise ValueError('Scope fields must be nonempty strings')
    return {k: record[k] for k in SCOPE_KEYS}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(previous, payload):
    return hashlib.sha256((previous + '\n' + payload).encode()).hexdigest()


class EvidenceHistory:
    """Append-only through this API; hashes detect accidental alteration, not forgery."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute('CREATE TABLE IF NOT EXISTS events '
                        '(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, '
                        'previous TEXT NOT NULL, digest TEXT NOT NULL)')
        for operation in ('UPDATE', 'DELETE'):
            self.db.execute(f"CREATE TRIGGER IF NOT EXISTS deny_{operation.lower()} "
                            f"BEFORE {operation} ON events BEGIN "
                            "SELECT RAISE(ABORT, 'Append-only evidence history'); END")

    def close(self):
        self.db.close()

    def _read(self):
        rows = self.db.execute('SELECT seq,payload,previous,digest FROM events ORDER BY seq').fetchall()
        if len(rows) > MAX_EVENTS:
            raise ValueError('History exceeds bound')
        previous = '0' * 64
        events = []
        for expected, (seq, payload, linked, digest) in enumerate(rows, 1):
            if (seq != expected or linked != previous or
                    len(payload.encode()) > MAX_EVENT_BYTES or
                    digest != _digest(previous, payload)):
                raise ValueError('Evidence history integrity failure')
            event = json.loads(payload)
            if _json(event) != payload:
                raise ValueError('Noncanonical evidence history')
            events.append(event)
            previous = digest
        self._replay(events)
        return events, previous

    @staticmethod
    def _replay(events):
        records, inactive = {}, set()
        for event in events:
            kind = event.get('kind')
            if kind == 'record':
                record = evidence_record(**event['record'])
                _scope(record)
                identifier = event['id']
                if not isinstance(identifier, str) or not identifier.strip() or identifier in records:
                    raise ValueError('Duplicate or invalid evidence id')
                if record.get('revoked_by') or record.get('supersedes'):
                    raise ValueError('Lifecycle must be represented by history events')
                target = event.get('supersedes')
                if target is not None:
                    if target not in records or target in inactive or _scope(records[target]) != _scope(record):
                        raise ValueError('Supersession requires active evidence in the same scope')
                    inactive.add(target)
                records[identifier] = record
            elif kind == 'revoke':
                target = event['target']
                if target not in records or target in inactive or not isinstance(event.get('reason'), str) or not event['reason'].strip():
                    raise ValueError('Revocation requires active evidence and reason')
                inactive.add(target)
            else:
                raise ValueError('Unknown evidence event')
        return records, inactive

    def _append(self, event):
        payload = _json(event)
        if len(payload.encode()) > MAX_EVENT_BYTES:
            raise ValueError('Evidence event exceeds bound')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            events, previous = self._read()
            if len(events) >= MAX_EVENTS:
                raise ValueError('History exceeds bound')
            self._replay(events + [event])
            self.db.execute('INSERT INTO events VALUES (?,?,?,?)',
                            (len(events) + 1, payload, previous, _digest(previous, payload)))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def add(self, identifier, record, *, supersedes=None):
        self._append({'kind': 'record', 'id': identifier,
                      'record': deepcopy(record), 'supersedes': supersedes})

    def revoke(self, identifier, *, reason):
        self._append({'kind': 'revoke', 'target': identifier, 'reason': reason})

    def project(self, *, scope, now):
        """Report exact-scope active observations; never promote to VERIFIED."""
        _scope(scope)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Clock requires timezone')
        events, head = self._read()
        records, inactive = self._replay(events)
        matching = [(key, project_evidence(record, scope=scope, now=now))
                    for key, record in records.items() if _scope(record) == _scope(scope)]
        active = [(key, projection) for key, projection in matching if key not in inactive]
        current = [(key, p) for key, p in active if p['evidence_applicable']]
        outcomes = {p['outcome'] for _, p in current}
        status = ('CONFLICTED' if 'PASS' in outcomes and 'FAIL' in outcomes else
                  'TESTED' if 'PASS' in outcomes else
                  'UNKNOWN' if current else
                  'STALE' if active and all(p['freshness'] == 'STALE' for _, p in active) else
                  'UNKNOWN')
        # All matching evidence inactive may be revoked or superseded; expose both explicitly.
        revoked = [e['target'] for e in events if e['kind'] == 'revoke']
        if matching and not active and any(key in revoked for key, _ in matching):
            status = 'REVOKED'
        return {'status': status, 'head_digest': head,
                'observations': [{'evidence_id': key, **p, 'inactive': key in inactive}
                                 for key, p in matching],
                'execution_allowed': False, 'canonical_allowed': False}
