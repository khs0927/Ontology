"""Read-only deployment preflight for the upstream RAGFlow v0.27.2 stack.

This module does not clone, pull, start, stop, or delete containers. It only
observes the host and produces the exact upstream-pinned commands an operator
may run after review.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Any, Callable


GIB = 1024 ** 3
_RELEASE = "v0.27.2"
_IMAGE = "infiniflow/ragflow:v0.27.2"
_REPO = "https://github.com/infiniflow/ragflow.git"
_MIN_DOCKER = (24, 0, 0)
_MIN_COMPOSE = (2, 26, 1)
_MIN_CPU = 4
_MIN_RAM_GB = 16.0
_MIN_DISK_GB = 50.0
_MIN_VM_MAP = 262144


@dataclass(frozen=True)
class RagflowDeploymentProfile:
    release: str = _RELEASE
    image: str = _IMAGE
    repository: str = _REPO
    minimum_cpu_cores: int = _MIN_CPU
    minimum_ram_gb: float = _MIN_RAM_GB
    minimum_disk_gb: float = _MIN_DISK_GB
    minimum_docker: tuple[int, int, int] = _MIN_DOCKER
    minimum_compose: tuple[int, int, int] = _MIN_COMPOSE
    minimum_vm_max_map_count: int = _MIN_VM_MAP


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )


def normalize_image_digest(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(sha256:[0-9a-f]{64})", value)
    return match.group(1) if match else None


def _ram_gb() -> float | None:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / GIB
        return None
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if isinstance(pages, int) and isinstance(page_size, int):
                return (pages * page_size) / GIB
        except (ValueError, OSError):
            pass
    return None


def _command(
    argv: list[str],
    *,
    timeout: float = 10.0,
) -> str | None:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def collect_local_observation(
    path: str | Path = ".",
    *,
    command: Callable[[list[str]], str | None] | None = None,
) -> dict[str, Any]:
    runner = command or _command
    root = Path(path).resolve()
    disk = shutil.disk_usage(root)
    vm_path = Path("/proc/sys/vm/max_map_count")
    vm_map = None
    if vm_path.is_file():
        try:
            vm_map = int(vm_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            vm_map = None
    return {
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
        "cpu_cores": os.cpu_count() or 0,
        "ram_gb": _ram_gb(),
        "disk_free_gb": disk.free / GIB,
        "docker_version": runner(
            ["docker", "version", "--format", "{{.Server.Version}}"]
        ),
        "compose_version": runner(
            ["docker", "compose", "version", "--short"]
        ),
        "vm_max_map_count": vm_map,
        "image_repo_digest": runner(
            [
                "docker",
                "image",
                "inspect",
                _IMAGE,
                "--format",
                "{{index .RepoDigests 0}}",
            ]
        ),
    }


def evaluate_preflight(
    observation: dict[str, Any],
    profile: RagflowDeploymentProfile | None = None,
) -> dict[str, Any]:
    profile = profile or RagflowDeploymentProfile()
    docker_version = parse_version(observation.get("docker_version"))
    compose_version = parse_version(observation.get("compose_version"))
    ram = observation.get("ram_gb")
    disk = observation.get("disk_free_gb")
    machine = str(observation.get("machine") or "").lower()
    vm_map = observation.get("vm_max_map_count")

    checks = {
        "cpu": {
            "pass": int(observation.get("cpu_cores") or 0) >= profile.minimum_cpu_cores,
            "observed": observation.get("cpu_cores"),
            "required": profile.minimum_cpu_cores,
        },
        "ram": {
            "pass": isinstance(ram, (int, float)) and ram >= profile.minimum_ram_gb,
            "observed": ram,
            "required": profile.minimum_ram_gb,
        },
        "disk": {
            "pass": isinstance(disk, (int, float)) and disk >= profile.minimum_disk_gb,
            "observed": disk,
            "required": profile.minimum_disk_gb,
        },
        "docker": {
            "pass": docker_version is not None and docker_version >= profile.minimum_docker,
            "observed": docker_version,
            "required": profile.minimum_docker,
        },
        "compose": {
            "pass": compose_version is not None and compose_version >= profile.minimum_compose,
            "observed": compose_version,
            "required": profile.minimum_compose,
        },
        "prebuilt_architecture": {
            "pass": machine in {"x86_64", "amd64", "x64"},
            "observed": machine,
            "required": "x86_64/amd64",
        },
    }

    manual_checks: list[str] = []
    system = str(observation.get("platform") or "").lower()
    if vm_map is None:
        if system in {"windows", "darwin"}:
            manual_checks.append(
                "Verify Docker Desktop/WSL2 vm.max_map_count >= 262144 when using Elasticsearch."
            )
    elif int(vm_map) < profile.minimum_vm_max_map_count:
        checks["vm_max_map_count"] = {
            "pass": False,
            "observed": vm_map,
            "required": profile.minimum_vm_max_map_count,
        }
    else:
        checks["vm_max_map_count"] = {
            "pass": True,
            "observed": vm_map,
            "required": profile.minimum_vm_max_map_count,
        }

    blockers = [name for name, check in checks.items() if check["pass"] is not True]
    status = (
        "BLOCKED"
        if blockers
        else ("READY_WITH_MANUAL_CHECKS" if manual_checks else "READY")
    )
    return {
        "schema": "drawing-context-ragflow-preflight/1",
        "status": status,
        "profile": {
            **asdict(profile),
            "minimum_docker": list(profile.minimum_docker),
            "minimum_compose": list(profile.minimum_compose),
        },
        "checks": checks,
        "blockers": blockers,
        "manual_checks": manual_checks,
        "observed_image_repo_digest": observation.get("image_repo_digest"),
        "observed_image_digest": normalize_image_digest(
            observation.get("image_repo_digest")
        ),
        "mutates_host": False,
    }


def deployment_commands(checkout: str | Path) -> list[str]:
    target = str(Path(checkout))
    compose = str(Path(target) / "docker" / "docker-compose.yml")
    return [
        f'git clone {_REPO} "{target}"',
        f'git -C "{target}" checkout -f {_RELEASE}',
        f'docker compose -f "{compose}" pull',
        f'docker compose -f "{compose}" up -d',
        "docker image inspect "
        + _IMAGE
        + " --format "{{index .RepoDigests 0}}"",
        "curl -f http://127.0.0.1/api/v1/system/healthz",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only RAGFlow v0.27.2 deployment preflight."
    )
    parser.add_argument("--path", default=".", help="Filesystem used for free-disk check.")
    parser.add_argument(
        "--checkout",
        default="runtime/ragflow-upstream",
        help="Proposed upstream checkout path; commands are printed only.",
    )
    args = parser.parse_args(argv)
    observation = collect_local_observation(args.path)
    report = evaluate_preflight(observation)
    report["deployment_commands"] = deployment_commands(args.checkout)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] != "BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
