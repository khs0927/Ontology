"""Caller-owned dependency and test policy; no host connection."""
import os
import subprocess
import sys

subprocess.run([sys.executable, "-m", "pip", "install", "pytest"], check=True)
subprocess.run(
    [sys.executable, "-m", "pytest", "-q", "extensions/external_capabilities/tests"],
    env={**os.environ, "PYTHONPATH": "extensions/external_capabilities"},
    check=True,
)
