from __future__ import annotations

import unittest

from context_fabric.capability_registry import (
    CapabilityDeclaration,
    CapabilityKey,
    DeploymentEvidence,
    DeploymentStatus,
    EvidenceValidity,
    LicenseEvidence,
    LicenseStatus,
    SourceProvenance,
    VerificationEvidence,
    VerificationKind,
    VerificationResult,
    build_manifest,
)


SHA40 = "a" * 40
SHA256 = "b" * 64
FIXTURE = "c" * 64
LICENSE_SHA = "d" * 64


def key() -> CapabilityKey:
    return CapabilityKey(
        provider_id="power-cad",
        capability_id="source-live-binding",
        host="AutoCAD",
        host_version="2027",
        upstream_commit=SHA40,
    )


def declaration(*, commit_verified=True) -> CapabilityDeclaration:
    return CapabilityDeclaration(
        key=key(),
        source=SourceProvenance(
            upstream_repo="khs0927/power-cad-mcp",
            upstream_commit=SHA40,
            source_path="dotnet/PowerCad.Server/OntologyLocate.cs",
            source_sha256=SHA256,
            commit_verified=commit_verified,
            commit_verification_url="https://github.com/khs0927/power-cad-mcp/commit/" + SHA40
            if commit_verified
            else None,
            observed_at="2026-10-03T09:00:00+00:00",
        ),
        description="Map an Ontology source revision to a live AutoCAD object.",
        required_verification_kinds=(
            VerificationKind.UNIT,
            VerificationKind.NATIVE_LIVE,
        ),
        declared_capabilities=("read-source-identity", "resolve-live-object"),
    )


def verification(
    evidence_id: str,
    kind: VerificationKind,
    result: VerificationResult,
    *,
    validity=EvidenceValidity.CURRENT,
    valid_until="2026-11-03T00:00:00+00:00",
    supersedes=(),
    revokes=(),
):
    return VerificationEvidence(
        evidence_id=evidence_id,
        key=key(),
        verification_kind=kind,
        result=result,
        validity=validity,
        recorded_at="2026-10-03T09:10:00+00:00",
        verifier_id="ontology-ci",
        verifier_version="1",
        fixture_sha256=FIXTURE,
        run_url="https://github.com/khs0927/Ontology/actions/runs/1",
        valid_until=valid_until,
        supersedes=supersedes,
        revokes=revokes,
        environment={
            "os": "windows-11",
            "host_build": "AutoCAD-2027",
            "adapter_version": "power-cad-preview",
        },
    )


def license_evidence(*, status=LicenseStatus.CONFIRMED, evidence_id="lic-1", supersedes=()):
    return LicenseEvidence(
        evidence_id=evidence_id,
        key=key(),
        status=status,
        recorded_at="2026-10-03T09:05:00+00:00",
        observed_license="MIT" if status is LicenseStatus.CONFIRMED else None,
        declared_license="MIT",
        license_path="LICENSE" if status is LicenseStatus.CONFIRMED else None,
        license_sha256=LICENSE_SHA if status is LicenseStatus.CONFIRMED else None,
        source_url="https://github.com/khs0927/power-cad-mcp/blob/" + SHA40 + "/LICENSE"
        if status is LicenseStatus.CONFIRMED
        else None,
        supersedes=supersedes,
    )


class CapabilityRegistryTests(unittest.TestCase):
    NOW = "2026-10-03T10:00:00+00:00"

    def test_declaration_requires_explicit_validation_lane(self):
        with self.assertRaises(ValueError):
            CapabilityDeclaration(
                key=key(),
                source=declaration().source,
                description="bad",
                required_verification_kinds=(),
            )

    def test_declared_capability_is_not_verified_by_metadata_alone(self):
        manifest = build_manifest([declaration()], now=self.NOW)
        row = manifest["records"][0]
        self.assertEqual(row["summary_state"], "DECLARED")
        self.assertEqual(row["verification"]["unit"]["result"], "NOT_RUN")
        self.assertFalse(row["may_authorize_execution"])
        self.assertEqual(row["execution_authority"], "executor-only")
        self.assertFalse(manifest["may_authorize_execution"])

    def test_unit_pass_without_native_live_stays_tested(self):
        manifest = build_manifest(
            [declaration()],
            verifications=[
                verification("unit-1", VerificationKind.UNIT, VerificationResult.PASS),
            ],
            licenses=[license_evidence()],
            now=self.NOW,
        )
        row = manifest["records"][0]
        self.assertEqual(row["summary_state"], "TESTED")
        self.assertEqual(row["verification"]["native-live"]["result"], "NOT_RUN")

    def test_all_required_current_pass_plus_confirmed_source_and_license_is_verified(self):
        manifest = build_manifest(
            [declaration()],
            verifications=[
                verification("unit-1", VerificationKind.UNIT, VerificationResult.PASS),
                verification("live-1", VerificationKind.NATIVE_LIVE, VerificationResult.PASS),
            ],
            licenses=[license_evidence()],
            deployments=[
                DeploymentEvidence(
                    evidence_id="deploy-1",
                    key=key(),
                    status=DeploymentStatus.RESPONSIVE,
                    recorded_at="2026-10-03T09:20:00+00:00",
                    host_build="AutoCAD 2027.0",
                    adapter_version="0.5.0-preview.1",
                    runtime=".NET 10",
                    os="Windows 11",
                )
            ],
            now=self.NOW,
        )
        row = manifest["records"][0]
        self.assertEqual(row["summary_state"], "VERIFIED")
        self.assertEqual(row["license"]["status"], "CONFIRMED")
        self.assertEqual(row["deployment"]["status"], "RESPONSIVE")
        self.assertFalse(row["canonical"])
        self.assertTrue(row["derived_projection"])

    def test_unverified_upstream_commit_blocks_verified_state(self):
        manifest = build_manifest(
            [declaration(commit_verified=False)],
            verifications=[
                verification("unit-1", VerificationKind.UNIT, VerificationResult.PASS),
                verification("live-1", VerificationKind.NATIVE_LIVE, VerificationResult.PASS),
            ],
            licenses=[license_evidence()],
            now=self.NOW,
        )
        self.assertEqual(manifest["records"][0]["summary_state"], "TESTED")

    def test_expired_required_evidence_is_not_current_verification(self):
        expired = verification(
            "live-1",
            VerificationKind.NATIVE_LIVE,
            VerificationResult.PASS,
            valid_until="2026-10-03T09:30:00+00:00",
        )
        manifest = build_manifest(
            [declaration()],
            verifications=[
                verification("unit-1", VerificationKind.UNIT, VerificationResult.PASS),
                expired,
            ],
            licenses=[license_evidence()],
            now=self.NOW,
        )
        row = manifest["records"][0]
        self.assertEqual(row["summary_state"], "TESTED")
        self.assertEqual(row["verification"]["native-live"]["validity"], "STALE")

    def test_license_conflict_blocks_verification(self):
        manifest = build_manifest(
            [declaration()],
            verifications=[
                verification("unit-1", VerificationKind.UNIT, VerificationResult.PASS),
                verification("live-1", VerificationKind.NATIVE_LIVE, VerificationResult.PASS),
            ],
            licenses=[
                LicenseEvidence(
                    evidence_id="lic-conflict",
                    key=key(),
                    status=LicenseStatus.CONFLICTED,
                    recorded_at="2026-10-03T09:30:00+00:00",
                    declared_license="Custom/Permissive Reference",
                    observed_license="MIT",
                    notes="registry metadata disagrees with pinned upstream LICENSE",
                )
            ],
            now=self.NOW,
        )
        row = manifest["records"][0]
        self.assertEqual(row["summary_state"], "CONFLICTED")
        self.assertIn("license_conflicted", row["conflicts"])

    def test_unsuperseded_duplicate_evidence_is_conflicted(self):
        manifest = build_manifest(
            [declaration()],
            verifications=[
                verification("unit-1", VerificationKind.UNIT, VerificationResult.PASS),
                verification("unit-2", VerificationKind.UNIT, VerificationResult.FAIL),
            ],
            licenses=[license_evidence()],
            now=self.NOW,
        )
        row = manifest["records"][0]
        self.assertEqual(row["summary_state"], "CONFLICTED")
        self.assertIn("multiple_active_verification:unit", row["conflicts"])

    def test_superseding_evidence_preserves_history_but_resolves_projection(self):
        old = verification("unit-1", VerificationKind.UNIT, VerificationResult.FAIL)
        new = verification(
            "unit-2",
            VerificationKind.UNIT,
            VerificationResult.PASS,
            supersedes=("unit-1",),
        )
        manifest = build_manifest(
            [declaration()],
            verifications=[
                old,
                new,
                verification("live-1", VerificationKind.NATIVE_LIVE, VerificationResult.PASS),
            ],
            licenses=[license_evidence()],
            now=self.NOW,
        )
        row = manifest["records"][0]
        self.assertEqual(row["summary_state"], "VERIFIED")
        self.assertEqual(row["verification"]["unit"]["evidence_id"], "unit-2")
        self.assertEqual(row["conflicts"], [])

    def test_unknown_lineage_reference_is_rejected(self):
        bad = verification(
            "unit-2",
            VerificationKind.UNIT,
            VerificationResult.PASS,
            supersedes=("missing",),
        )
        with self.assertRaises(ValueError):
            build_manifest([declaration()], verifications=[bad], now=self.NOW)


if __name__ == "__main__":
    unittest.main()
