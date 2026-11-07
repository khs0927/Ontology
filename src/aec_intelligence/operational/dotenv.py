"""Load the repository-root ``.env`` into the environment for the operational CLI.

Scheduled tasks and ad-hoc shells call ``python -m aec_intelligence.operational.cli`` without the
variables docker-compose reads from ``.env`` (AEC_DATABASE_URL, AEC_DATA_ROOT, ...), so the CLI fell
back to defaults such as the placeholder DB password. This loader fills only variables that are not
already set: an explicit environment always wins over the file.

``AEC_ENV_FILE`` points at another file; ``AEC_ENV_FILE=none`` disables loading. Values are never
printed; :func:`load_repo_env` returns only the names it set.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_LINE = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*$")


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines: comments, blank lines, ``export``, quotes and trailing `` # comments``."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
            if match.group("value")[0] == '"':
                value = value.replace("\\n", "\n").replace('\\"', '"')
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[match.group("key")] = value
    return values


def env_file_path(environ: MutableMapping[str, str] | None = None) -> Path | None:
    environ = os.environ if environ is None else environ
    configured = environ.get("AEC_ENV_FILE", "").strip()
    if configured.lower() in {"none", "off", "0", "false"}:
        return None
    return Path(configured) if configured else REPO_ROOT / ".env"


def load_repo_env(path: Path | None = None, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Set variables from ``path`` (default: repo-root ``.env``) that ``environ`` lacks; return their names."""
    environ = os.environ if environ is None else environ
    path = env_file_path(environ) if path is None else path
    if path is None or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    loaded = []
    for key, value in parse_env(text).items():
        if key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded
