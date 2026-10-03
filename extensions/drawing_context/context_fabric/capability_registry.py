"""Append-only evidence registry for external AEC/CAD capabilities.

This registry is a rebuildable projection. It never grants execution permission and
never promotes external provider data into CAIR canonical truth.

Storage deliberately separates capability declaration, upstream/source provenance,
verification result and validity, license evidence, and deployment observation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Any, Iterable


_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VerificationKind(StrEnum):
    STATIC = "static"
    UNIT = "unit"
    SIMULATOR = "simulator"
    HEADLESS = "headless"
    NATIVE_LIVE = "native-live"


class VerificationResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    NOT_RUN = "NOT_RUN"


class EvidenceValidity(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    REVOKED = "REVOKED"


class LicenseStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    CONFLICTED = "CONFLICTED"


class DeploymentStatus(StrEnum):
    UNINSTALLED = "UNINSTALLED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    RESPONSIVE = "RESPONSIVE"
    FAILED = "FAILED"


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()


def _sha(value: str, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} has invalid digest format")
    return value


@dataclass(frozen=True, order=True)
class CapabilityKey:
    provider_id: str
    capability_id: str
    host: str
    host_version: str
    upstream_commit: str

    def __post_init__(self):
        for name in ("provider_id", "capability_id", "host", "host_version"):
            _nonempty(getattr(self, name), f"CapabilityKey.{name}")
        _sha(self.upstream_commit, _SHA40, "CapabilityKey.upstream_commit")

    @property
    def stable_id(self) -> str:
        return "::".join((
            self.provider_id,
            self.capability_id,
            self.host,
            self.host_version,
            self.upstream_commit,
        ))


@dataclass(frozen=True)
class SourceProvenance:
    upstream_repo: str
    upstream_commit: str
    source_path: str
    source_sha256: str
    commit_verified: bool
    commit_verification_url: str | None = None
    observed_at: str = "1970-01-01T00:00:00+00:00"

    def __post_init__(self):
        _nonempty(self.upstream_repo, "SourceProvenance.upstream_repo")
        if self.upstream_repo.count("/") != 1:
            raise ValueError("upstream_repo must use owner/repository form")
        _sha(self.upstream_commit, _SHA40, "SourceProvenance.upstream_commit")
        _nonempty(self.source_path, "SourceProvenance.source_path")
        _sha(self.source_sha256, _SHA256, "SourceProvenance.source_sha256")
        _utc(self.observed_at)
        if self.commit_verified and not self.commit_verification_url:
            raise ValueError("verified upstream commit requires commit_verification_url")


@dataclass(frozen=True)
class CapabilityDeclaration:
    key: CapabilityKey
    source: SourceProvenance
    description: str
    required_verification_kinds: tuple[VerificationKind, ...] = ()
    declared_capabilities: tuple[str, ...] = ()

    def __post_init__(self):
        _nonempty(self.description, "CapabilityDeclaration.description")
        if self.key.upstream_commit != self.source.upstream_commit:
            raise ValueError("capability key and source provenance must use the same commit")
        if len(set(self.required_verification_kinds)) != len(self.required_verification_kinds):
            raise ValueError("required_verification_kinds must be unique")


@dataclass(frozen=True)
class VerificationEvidence:
    evidence_id: str
    key: CapabilityKey
    verification_kind: VerificationKind
    result: VerificationResult
    validity: EvidenceValidity
    recorded_at: str
    verifier_id: str
    verifier_version: str
    fixture_sha256: str | None = None
    run_url: str | None = None
    valid_until: str | None = None
    supersedes: tuple[str, ...] = ()
    revokes: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self):
        _nonempty(self.evidence_id, "VerificationEvidence.evidence_id")
        _utc(self.recorded_at)
        _nonempty(self.verifier_id, "VerificationEvidence.verifier_id")
        _nonempty(self.verifier_version, "VerificationEvidence.verifier_version")
        if self.fixture_sha256 is not None:
            _sha(self.fixture_sha256, _SHA256, "VerificationEvidence.fixture_sha256")
        if self.valid_until is not None and _utc(self.valid_until) < _utc(self.recorded_at):
            raise ValueError("valid_until cannot precede recorded_at")
        if self.evidence_id in self.supersedes or self.evidence_id in self.revokes:
            raise ValueError("evidence cannot supersede or revoke itself")
        if set(self.supersedes) & set(self.revokes):
            raise ValueError("one predecessor cannot be both superseded and revoked")
        for name, value in self.environment.items():
            _nonempty(name, "environment key")
            _nonempty(value, f"environment[{name!r}]")


@dataclass(frozen=True)
class LicenseEvidence:
    evidence_id: str
    key: CapabilityKey
    status: LicenseStatus
    recorded_at: str
    observed_license: str | None = None
    declared_license: str | None = None
    license_path: str | None = None
    license_sha256: str | None = None
    source_url: str | None = None
    supersedes: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self):
        _nonempty(self.evidence_id, "LicenseEvidence.evidence_id")
        _utc(self.recorded_at)
        if self.license_sha256 is not None:
            _sha(self.license_sha256, _SHA256, "LicenseEvidence.license_sha256")
        if self.status is LicenseStatus.CONFIRMED:
            _nonempty(self.observed_license or "", "confirmed observed_license")
            _nonempty(self.license_path or "", "confirmed license_path")
            _sha(self.license_sha256 or "", _SHA256, "confirmed license_sha256")
            _nonempty(self.source_url or "", "confirmed source_url")


@dataclass(frozen=True)
class DeploymentEvidence:
    evidence_id: str
    key: CapabilityKey
    status: DeploymentStatus
    recorded_at: str
    host_build: str
    adapter_version: str
    runtime: str
    os: str
    supersedes: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self):
        _nonempty(self.evidence_id, "DeploymentEvidence.evidence_id")
        _utc(self.recorded_at)
        for name in ("host_build", "adapter_version", "runtime", "os"):
            _nonempty(getattr(self, name), f"DeploymentEvidence.{name}")


def _active_records(records: list[Any]) -> list[Any]:
    by_id: dict[str, Any] = {}
    displaced: set[str] = set()
    for row in records:
        if row.evidence_id in by_id:
            raise ValueError(f"duplicate evidence_id: {row.evidence_id}")
        by_id[row.evidence_id] = row
        displaced.update(row.supersedes)
        displaced.update(getattr(row, "revokes", ()))
    unknown = displaced - set(by_id)
    if unknown:
        raise ValueError(f"evidence lineage references unknown records: {sorted(unknown)}")
    return [row for row in records if row.evidence_id not in displaced]


def _effective_validity(row: VerificationEvidence, now: datetime) -> EvidenceValidity:
    if row.validity is not EvidenceValidity.CURRENT:
        return row.validity
    if row.valid_until is not None and _utc(row.valid_until) < now:
        return EvidenceValidity.STALE
    return EvidenceValidity.CURRENT


def _verification_projection(
    declaration: CapabilityDeclaration,
    records: list[VerificationEvidence],
    now: datetime,
) -> tuple[dict[str, Any], list[str], bool]:
    active = _active_records(records)
    by_kind: dict[VerificationKind, list[VerificationEvidence]] = {}
    for row in active:
        by_kind.setdefault(row.verification_kind, []).append(row)

    projected: dict[str, Any] = {}
    conflicts: list[str] = []
    required_pass = True
    for kind in VerificationKind:
        rows = sorted(by_kind.get(kind, []), key=lambda row: _utc(row.recorded_at))
        if len(rows) > 1:
            conflicts.append(f"multiple_active_verification:{kind.value}")
        row = rows[-1] if rows else None
        if row is None:
            projected[kind.value] = {
                "result": VerificationResult.NOT_RUN.value,
                "validity": EvidenceValidity.CURRENT.value,
                "evidence_id": None,
            }
        else:
            projected[kind.value] = {
                "result": row.result.value,
                "validity": _effective_validity(row, now).value,
                "evidence_id": row.evidence_id,
                "recorded_at": row.recorded_at,
                "valid_until": row.valid_until,
                "verifier_id": row.verifier_id,
                "verifier_version": row.verifier_version,
                "fixture_sha256": row.fixture_sha256,
                "run_url": row.run_url,
                "environment": dict(sorted(row.environment.items())),
            }

    for kind in declaration.required_verification_kinds:
        row = projected[kind.value]
        if row["result"] != VerificationResult.PASS.value or row["validity"] != EvidenceValidity.CURRENT.value:
            required_pass = False
    return projected, conflicts, required_pass


def _latest_single(records: list[Any], conflict_name: str) -> tuple[Any | None, list[str]]:
    active = _active_records(records)
    if not active:
        return None, []
    active = sorted(active, key=lambda row: _utc(row.recorded_at))
    conflicts = [conflict_name] if len(active) > 1 else []
    return active[-1], conflicts


def build_manifest(
    declarations: Iterable[CapabilityDeclaration],
    *,
    verifications: Iterable[VerificationEvidence] = (),
    licenses: Iterable[LicenseEvidence] = (),
    deployments: Iterable[DeploymentEvidence] = (),
    now: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, rebuildable evidence projection.

    Runtime authorization intentionally belongs to the executor, not this registry.
    """
    timestamp = _utc(now) if now else datetime.now(timezone.utc)
    declaration_rows = list(declarations)
    by_id = {row.key.stable_id: row for row in declaration_rows}
    if len(by_id) != len(declaration_rows):
        raise ValueError("duplicate capability declaration")

    verification_rows = list(verifications)
    license_rows = list(licenses)
    deployment_rows = list(deployments)
    for collection, name in (
        (verification_rows, "verification"),
        (license_rows, "license"),
        (deployment_rows, "deployment"),
    ):
        for row in collection:
            if row.key.stable_id not in by_id:
                raise ValueError(f"{name} evidence references undeclared capability: {row.key.stable_id}")

    manifest_rows: list[dict[str, Any]] = []
    for stable_id, declaration in sorted(by_id.items()):
        vrows = [row for row in verification_rows if row.key == declaration.key]
        lrows = [row for row in license_rows if row.key == declaration.key]
        drows = [row for row in deployment_rows if row.key == declaration.key]

        verification, conflicts, required_pass = _verification_projection(declaration, vrows, timestamp)
        license_row, license_conflicts = _latest_single(lrows, "multiple_active_license_evidence")
        deployment_row, deployment_conflicts = _latest_single(drows, "multiple_active_deployment_evidence")
        conflicts.extend(license_conflicts)
        conflicts.extend(deployment_conflicts)

        license_status = license_row.status if license_row else LicenseStatus.UNKNOWN
        if license_status is LicenseStatus.CONFLICTED:
            conflicts.append("license_conflicted")

        has_tests = bool(vrows)
        any_current = any(
            cell["evidence_id"] is not None and cell["validity"] == EvidenceValidity.CURRENT.value
            for cell in verification.values()
        )
        all_revoked = bool(vrows) and not any(
            _effective_validity(row, timestamp) is not EvidenceValidity.REVOKED for row in vrows
        )
        all_stale_or_revoked = bool(vrows) and not any_current

        if conflicts:
            summary = "CONFLICTED"
        elif all_revoked:
            summary = "REVOKED"
        elif all_stale_or_revoked:
            summary = "STALE"
        elif (
            required_pass
            and declaration.source.commit_verified
            and license_status is LicenseStatus.CONFIRMED
        ):
            summary = "VERIFIED"
        elif has_tests:
            summary = "TESTED"
        else:
            summary = "DECLARED"

        row = {
            "capability_key": asdict(declaration.key),
            "capability_id": stable_id,
            "description": declaration.description,
            "declared_capabilities": list(declaration.declared_capabilities),
            "required_verification_kinds": [kind.value for kind in declaration.required_verification_kinds],
            "source": asdict(declaration.source),
            "verification": verification,
            "license": (
                {
                    **asdict(license_row),
                    "status": license_row.status.value,
                    "key": asdict(license_row.key),
                }
                if license_row
                else {"status": LicenseStatus.UNKNOWN.value}
            ),
            "deployment": (
                {
                    **asdict(deployment_row),
                    "status": deployment_row.status.value,
                    "key": asdict(deployment_row.key),
                }
                if deployment_row
                else {"status": DeploymentStatus.UNINSTALLED.value}
            ),
            "summary_state": summary,
            "conflicts": sorted(set(conflicts)),
            "may_authorize_execution": False,
            "execution_authority": "executor-only",
            "canonical": False,
            "derived_projection": True,
        }
        manifest_rows.append(row)

    return {
        "schema": "aec-external-capability-manifest/1",
        "generated_at": timestamp.isoformat(),
        "records": manifest_rows,
        "canonical": False,
        "derived_projection": True,
        "may_authorize_execution": False,
    }
