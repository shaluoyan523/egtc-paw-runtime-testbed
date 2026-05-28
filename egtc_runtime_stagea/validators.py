from __future__ import annotations

from .artifact_store import ArtifactStore
from .models import EvidenceBundle, NodeCapsule, ValidatorReport


class DeterministicValidator:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    def run(self, evidence: EvidenceBundle, node: NodeCapsule) -> list[ValidatorReport]:
        return [
            self._evidence_ref_present(evidence),
            self._required_artifacts_present(evidence, node),
            self._artifacts_verify(evidence),
            self._test_report_passes(evidence),
            self._diff_collected(evidence),
            self._sandbox_events_collected(evidence),
            self._resource_report_collected(evidence),
        ]

    def _evidence_ref_present(self, evidence: EvidenceBundle) -> ValidatorReport:
        passed = bool(evidence.evidence_ref and evidence.evidence_ref.uri)
        return ValidatorReport(
            validator_id="evidence_ref_present",
            passed=passed,
            findings=[] if passed else ["Evidence bundle has no evidence_ref."],
            evidence_ref=evidence.evidence_ref.uri if evidence.evidence_ref else None,
        )

    def _required_artifacts_present(
        self, evidence: EvidenceBundle, node: NodeCapsule
    ) -> ValidatorReport:
        missing = [
            artifact_kind
            for artifact_kind in node.required_evidence
            if artifact_kind not in evidence.artifacts
        ]
        return ValidatorReport(
            validator_id="required_artifacts_present",
            passed=not missing,
            findings=[f"Missing required artifact: {item}" for item in missing],
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _artifacts_verify(self, evidence: EvidenceBundle) -> ValidatorReport:
        invalid = [
            key for key, ref in evidence.artifacts.items() if not self.artifact_store.verify(ref)
        ]
        if not self.artifact_store.verify(evidence.evidence_ref):
            invalid.append("evidence_ref")
        return ValidatorReport(
            validator_id="artifact_integrity",
            passed=not invalid,
            findings=[f"Artifact failed integrity check: {item}" for item in invalid],
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _test_report_passes(self, evidence: EvidenceBundle) -> ValidatorReport:
        ref = evidence.artifacts.get("test")
        findings: list[str] = []
        passed = False
        if not ref or ref.metadata.get("kind") != "test_report":
            findings.append("No test report artifact was collected.")
        else:
            try:
                report = self.artifact_store.get_json(
                    ref,
                    self.artifact_store.identity.actor("validator-stagea", "validator"),
                    self.artifact_store.identity.issue_token(
                        self.artifact_store.identity.actor("validator-stagea", "validator"),
                        ["artifact:read"],
                    ),
                )
            except Exception as exc:
                findings.append(f"Unable to read test report artifact: {exc}")
            else:
                test_events = report.get("events", [])
                passed = bool(report.get("passed") and test_events)
                if not test_events:
                    findings.append("Test report contains no test events.")
                elif not report.get("passed"):
                    findings.append("Test report did not include a passing test event.")
        return ValidatorReport(
            validator_id="test_report_passed",
            passed=passed,
            findings=[] if passed else findings,
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _diff_collected(self, evidence: EvidenceBundle) -> ValidatorReport:
        ref = evidence.artifacts.get("diff")
        passed = bool(ref and ref.metadata.get("kind") == "workspace_diff")
        return ValidatorReport(
            validator_id="diff_collected",
            passed=passed,
            findings=[] if passed else ["No workspace diff artifact was collected."],
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _sandbox_events_collected(self, evidence: EvidenceBundle) -> ValidatorReport:
        ref = evidence.artifacts.get("sandbox_events")
        passed = bool(ref and "sandbox_events" in ref.metadata.get("kind", ""))
        return ValidatorReport(
            validator_id="sandbox_events_collected",
            passed=passed,
            findings=[] if passed else ["No sandbox event stream artifact was collected."],
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _resource_report_collected(self, evidence: EvidenceBundle) -> ValidatorReport:
        ref = evidence.artifacts.get("resource_report")
        findings: list[str] = []
        passed = False
        if not ref or "resource_report" not in ref.metadata.get("kind", ""):
            findings.append("No resource report artifact was collected.")
        else:
            try:
                report = self.artifact_store.get_json(
                    ref,
                    self.artifact_store.identity.actor("validator-stagea", "validator"),
                    self.artifact_store.identity.issue_token(
                        self.artifact_store.identity.actor("validator-stagea", "validator"),
                        ["artifact:read"],
                    ),
                )
            except Exception as exc:
                findings.append(f"Unable to read resource report artifact: {exc}")
            else:
                required = {"wall_time_sec", "cpu_time_sec", "command_count", "timeout_killed"}
                missing = sorted(required - set(report))
                passed = not missing
                findings.extend(f"Resource report missing field: {field}" for field in missing)
        return ValidatorReport(
            validator_id="resource_report_collected",
            passed=passed,
            findings=[] if passed else findings,
            evidence_ref=evidence.evidence_ref.uri,
        )
