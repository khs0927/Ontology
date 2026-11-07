"""The halfvec migration fails fast with an actionable message on an old pgvector."""
import pytest

pytest.importorskip("psycopg")

from aec_intelligence.operational.db import (  # noqa: E402
    MIN_PGVECTOR_FOR_HALFVEC,
    _version_tuple,
    require_halfvec_support,
)


class _Conn:
    def __init__(self, version):
        self.version = version

    def execute(self, sql, *args):
        assert "pg_extension" in sql
        row = None if self.version is None else (self.version,)

        class _Cursor:
            def fetchone(self_inner):
                return row

        return _Cursor()


@pytest.mark.parametrize("text,expected", [
    ("0.8.1", (0, 8, 1)),
    ("0.7", (0, 7, 0)),
    ("0.7.0-dev", (0, 7, 0)),
    ("1", (1, 0, 0)),
    ("", None),
    (None, None),
    ("dev", None),
])
def test_version_tuple(text, expected):
    assert _version_tuple(text) == expected


@pytest.mark.parametrize("version", ["0.7.0", "0.8.1", "1.0.0"])
def test_supported_pgvector_passes(version):
    require_halfvec_support(_Conn(version))


@pytest.mark.parametrize("version", ["0.6.0", "0.5.1", None, "garbage"])
def test_old_or_missing_pgvector_is_refused(version):
    with pytest.raises(RuntimeError) as err:
        require_halfvec_support(_Conn(version))
    message = str(err.value)
    assert "halfvec" in message
    assert ".".join(map(str, MIN_PGVECTOR_FOR_HALFVEC)) in message
    assert "ALTER EXTENSION vector UPDATE" in message
