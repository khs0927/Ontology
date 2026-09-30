"""Safe wrapper around the external jevgrep CLI.

The wrapper deliberately does not authenticate, install packages, or silently
send private source code. Upstream jevgrep can transmit eligible source content
to the configured Jev/provider, so source egress requires an explicit flag.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Iterable, Sequence


@dataclass(frozen=True)
class JevGrepReport:
    status: str
    question: str
    root: str
    command: tuple[str, ...]
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JevGrepAdapter:
    """Invoke jevgrep as an optional external context selector.

    command_prefix makes the adapter usable with wrappers such as WSL. For
    example, a Windows host may pass ("wsl", "jg") and a WSL-visible root.
    The adapter checks only the first executable in the prefix.
    """

    def __init__(
        self,
        command_prefix: Sequence[str] = ("jg",),
        *,
        resolver: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        if not command_prefix or not all(isinstance(value, str) and value for value in command_prefix):
            raise ValueError("command_prefix must contain at least one executable token")
        self.command_prefix = tuple(command_prefix)
        self._resolver = resolver
        self._runner = runner

    @staticmethod
    def _root(root: str | Path) -> Path:
        path = Path(root).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"jevgrep root is not a directory: {path}")
        return path

    @staticmethod
    def _exclude_args(excludes: Iterable[str]) -> list[str]:
        args: list[str] = []
        for pattern in excludes:
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError("exclude patterns must be non-empty strings")
            args.extend(["--exclude", pattern])
        return args

    def _available(self) -> bool:
        return self._resolver(self.command_prefix[0]) is not None

    def files(
        self,
        root: str | Path,
        *,
        excludes: Iterable[str] = (),
        timeout: float = 30.0,
    ) -> JevGrepReport:
        """Run jg files which upstream documents as a local file-count step."""
        repo = self._root(root)
        command = (*self.command_prefix, "files", str(repo), *self._exclude_args(excludes))
        if not self._available():
            return JevGrepReport(
                "REQUIRES_DEPENDENCY",
                "",
                str(repo),
                command,
                warnings=("jevgrep executable is unavailable; no command was executed",),
            )
        return self._run(command, repo, "", timeout)

    def search(
        self,
        question: str,
        root: str | Path,
        *,
        excludes: Iterable[str] = (),
        allow_source_egress: bool = False,
        timeout: float = 60.0,
    ) -> JevGrepReport:
        """Search source context only after explicit source-egress approval."""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        repo = self._root(root)
        command = (*self.command_prefix, question, str(repo), *self._exclude_args(excludes))
        if not allow_source_egress:
            return JevGrepReport(
                "REQUIRES_EGRESS_APPROVAL",
                question,
                str(repo),
                command,
                warnings=(
                    "jevgrep may send eligible source content to the configured provider; set allow_source_egress=True only for an approved root",
                ),
            )
        if not self._available():
            return JevGrepReport(
                "REQUIRES_DEPENDENCY",
                question,
                str(repo),
                command,
                warnings=("jevgrep executable is unavailable; no source left the machine",),
            )
        return self._run(command, repo, question, timeout)

    def _run(self, command: tuple[str, ...], repo: Path, question: str, timeout: float) -> JevGrepReport:
        try:
            completed = self._runner(
                list(command),
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return JevGrepReport(
                "FAILED",
                question,
                str(repo),
                command,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                warnings=(f"jevgrep timed out after {timeout:g}s",),
            )
        except OSError as exc:
            return JevGrepReport("FAILED", question, str(repo), command, stderr=str(exc))
        return JevGrepReport(
            "SUCCESS" if completed.returncode == 0 else "FAILED",
            question,
            str(repo),
            command,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            returncode=int(completed.returncode),
        )
