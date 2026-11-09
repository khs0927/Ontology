"""Locate optional desktop runtimes for host acceptance tests.

Order: explicit environment variable, then PATH, then the Windows install the tests were first
written against. A test that needs a runtime skips when none of these exists.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_WINDOWS_FREECAD = Path(r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe")
_WINDOWS_BLENDER = Path(r"C:\Users\USER\Documents\ChatGPT\온톨로지\runtime\external\blender-5.2.0\blender.exe")


def _find(env: str, names: tuple[str, ...], fallback: Path) -> Path:
    explicit = os.environ.get(env, "").strip()
    if explicit:
        return Path(explicit)
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return fallback


def freecad_cmd() -> Path:
    """AEC_FREECAD_EXECUTABLE, else freecadcmd/FreeCADCmd on PATH, else the Windows default."""
    return _find("AEC_FREECAD_EXECUTABLE", ("freecadcmd", "FreeCADCmd"), _WINDOWS_FREECAD)


def blender() -> Path:
    """AEC_BLENDER_EXECUTABLE, else blender on PATH, else the Windows default."""
    return _find("AEC_BLENDER_EXECUTABLE", ("blender",), _WINDOWS_BLENDER)
