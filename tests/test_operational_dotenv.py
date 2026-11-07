"""The operational CLI loads the repo-root .env without overriding variables already set."""

from __future__ import annotations

from pathlib import Path

from aec_intelligence.operational import dotenv
from aec_intelligence.operational.dotenv import REPO_ROOT, env_file_path, load_repo_env, parse_env


def test_parse_env_handles_comments_quotes_and_export():
    text = (
        "\ufeff# comment\n"
        "\n"
        "AEC_DATABASE_URL=postgresql://aec:s3cret@127.0.0.1:55432/aec\n"
        "export AEC_DATA_ROOT='D:/AECData'\n"
        'AEC_EMBEDDING_MODEL="BAAI/bge-m3"  \n'
        "AEC_WORKERS=4 # inline comment\n"
        "AEC_HASH=abc#def\n"
        "not a line\n"
        "EMPTY=\n"
    )
    assert parse_env(text.lstrip("\ufeff")) == {
        "AEC_DATABASE_URL": "postgresql://aec:s3cret@127.0.0.1:55432/aec",
        "AEC_DATA_ROOT": "D:/AECData",
        "AEC_EMBEDDING_MODEL": "BAAI/bge-m3",
        "AEC_WORKERS": "4",
        "AEC_HASH": "abc#def",
        "EMPTY": "",
    }


def test_load_repo_env_does_not_override_existing_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("\ufeffAEC_DATABASE_URL=from-file\nAEC_DATA_ROOT=D:/AECData\n", encoding="utf-8")
    environ = {"AEC_DATABASE_URL": "from-shell"}

    loaded = load_repo_env(env_file, environ)

    assert loaded == ["AEC_DATA_ROOT"]
    assert environ == {"AEC_DATABASE_URL": "from-shell", "AEC_DATA_ROOT": "D:/AECData"}


def test_missing_file_and_opt_out_are_no_ops(tmp_path: Path):
    environ: dict[str, str] = {}
    assert load_repo_env(tmp_path / "absent.env", environ) == [] and environ == {}
    assert env_file_path({"AEC_ENV_FILE": "none"}) is None
    assert load_repo_env(None, {"AEC_ENV_FILE": "off"}) == []
    assert env_file_path({}) == REPO_ROOT / ".env"
    assert env_file_path({"AEC_ENV_FILE": str(tmp_path / "x.env")}) == tmp_path / "x.env"
    assert (REPO_ROOT / "pyproject.toml").is_file()  # REPO_ROOT really is the checkout root


def test_cli_main_loads_env_before_parsing(tmp_path: Path, monkeypatch):
    env_file = tmp_path / "ops.env"
    env_file.write_text("AEC_TEST_DOTENV_MARKER=loaded\n", encoding="utf-8")
    monkeypatch.setenv("AEC_ENV_FILE", str(env_file))
    monkeypatch.delenv("AEC_TEST_DOTENV_MARKER", raising=False)
    import os

    from aec_intelligence.operational.cli import main

    try:
        main(["--help"])
    except SystemExit:
        pass
    assert os.environ.get("AEC_TEST_DOTENV_MARKER") == "loaded"
    monkeypatch.delenv("AEC_TEST_DOTENV_MARKER")
    assert dotenv.load_repo_env is load_repo_env
