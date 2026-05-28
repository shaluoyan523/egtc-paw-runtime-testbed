from __future__ import annotations

from typing import Any

from .artifact_store import ArtifactStore
from .models import EvidenceBundle, NodeCapsule, ValidatorReport


class DeterministicValidator:
    VIRTUAL_REQUIRED_EVIDENCE = {
        "solver_completed",
        "target_validation_passed",
    }

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    def run(self, evidence: EvidenceBundle, node: NodeCapsule) -> list[ValidatorReport]:
        reports = [
            self._evidence_ref_present(evidence),
            self._required_artifacts_present(evidence, node),
            self._artifacts_verify(evidence),
            self._test_report_passes(evidence),
            self._diff_collected(evidence),
            self._sandbox_events_collected(evidence),
            self._resource_report_collected(evidence),
        ]
        reports.extend(self._em_reports(evidence, node))
        return reports

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
            if artifact_kind not in self.VIRTUAL_REQUIRED_EVIDENCE
            and artifact_kind not in evidence.artifacts
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

    def _em_reports(
        self,
        evidence: EvidenceBundle,
        node: NodeCapsule,
    ) -> list[ValidatorReport]:
        em_required = {
            "bridge_report",
            "geometry_manifest",
            "hfss_project",
            "mesh_report",
            "optimizer_report",
            "sparameters",
            "solver_log",
            "solver_report",
            "validation_report",
        }
        if not em_required.intersection(set(node.required_evidence)):
            return []
        reports: list[ValidatorReport] = []
        if "solver_report" in node.required_evidence or "solver_completed" in node.required_evidence:
            reports.append(self._solver_report_passes(evidence))
        if "sparameters" in node.required_evidence:
            reports.append(self._sparameters_present(evidence))
        if "validation_report" in node.required_evidence:
            reports.append(self._validation_report_parseable(evidence))
        if "target_validation_passed" in node.required_evidence:
            reports.append(self._validation_report_passes(evidence))
        if "optimizer_report" in node.required_evidence:
            reports.append(self._optimizer_report_passes(evidence))
        if "bridge_report" in node.required_evidence:
            reports.append(self._bridge_report_passes(evidence))
        return reports

    def _solver_report_passes(self, evidence: EvidenceBundle) -> ValidatorReport:
        report = self._read_artifact_json(evidence, "solver_report")
        findings: list[str] = []
        passed = False
        if report is None:
            findings.append("No parseable solver_report artifact was collected.")
        else:
            status = str(report.get("status") or "").lower()
            solved = bool(report.get("solved"))
            sparameters = report.get("sparameters") or report.get("sparameter_path")
            passed = status in {"ok", "created_patch_antenna", "created_patch_antenna_with_port", "solved_patch_antenna"} or solved
            if status not in {"ok", "created_patch_antenna", "created_patch_antenna_with_port", "solved_patch_antenna"} and not solved:
                findings.append(f"solver_report status is not successful: {status or '<missing>'}")
            if solved and not sparameters:
                findings.append("solver_report solved=true but does not reference exported S-parameters.")
                passed = False
        return ValidatorReport(
            validator_id="em_solver_report_passed",
            passed=passed,
            findings=[] if passed else findings,
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _validation_report_parseable(self, evidence: EvidenceBundle) -> ValidatorReport:
        report = self._read_artifact_json(evidence, "validation_report")
        findings: list[str] = []
        passed = report is not None
        if report is None:
            findings.append("No parseable validation_report artifact was collected.")
        elif not isinstance(report.get("metrics", {}), dict):
            findings.append("validation_report does not contain a metrics object.")
            passed = False
        return ValidatorReport(
            validator_id="em_validation_report_parseable",
            passed=passed,
            findings=[] if passed else findings,
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _sparameters_present(self, evidence: EvidenceBundle) -> ValidatorReport:
        ref = evidence.artifacts.get("sparameters")
        passed = bool(ref and ref.size_bytes > 0)
        return ValidatorReport(
            validator_id="em_sparameters_present",
            passed=passed,
            findings=[] if passed else ["No non-empty Touchstone S-parameter artifact was collected."],
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _validation_report_passes(self, evidence: EvidenceBundle) -> ValidatorReport:
        report = self._read_artifact_json(evidence, "validation_report")
        findings: list[str] = []
        passed = False
        if report is None:
            findings.append("No parseable validation_report artifact was collected.")
        else:
            status = str(report.get("status") or "").lower()
            checks = report.get("checks") if isinstance(report.get("checks"), list) else []
            failed = [
                str(check.get("name") or index)
                for index, check in enumerate(checks)
                if isinstance(check, dict) and not bool(check.get("passed"))
            ]
            passed = status == "ok" and not failed
            if status != "ok":
                findings.append(f"validation_report status is not ok: {status or '<missing>'}")
            findings.extend(f"EM validation check failed: {name}" for name in failed)
        return ValidatorReport(
            validator_id="em_validation_report_passed",
            passed=passed,
            findings=[] if passed else findings,
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _optimizer_report_passes(self, evidence: EvidenceBundle) -> ValidatorReport:
        report = self._read_artifact_json(evidence, "optimizer_report")
        findings: list[str] = []
        passed = False
        if report is None:
            findings.append("No parseable optimizer_report artifact was collected.")
        else:
            status = str(report.get("status") or "").lower()
            passed = status == "ok" and bool(
                report.get("best_candidate")
                or report.get("ranked_candidates")
                or report.get("proposed_parameters")
            )
            if status != "ok":
                findings.append(f"optimizer_report status is not ok: {status or '<missing>'}")
            if not (report.get("best_candidate") or report.get("ranked_candidates") or report.get("proposed_parameters")):
                findings.append("optimizer_report does not contain candidate ranking or proposed parameters.")
        return ValidatorReport(
            validator_id="em_optimizer_report_passed",
            passed=passed,
            findings=[] if passed else findings,
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _bridge_report_passes(self, evidence: EvidenceBundle) -> ValidatorReport:
        report = self._read_artifact_json(evidence, "bridge_report")
        findings: list[str] = []
        passed = False
        if report is None:
            findings.append("No parseable bridge_report artifact was collected.")
        else:
            status = str(report.get("status") or "").lower()
            failed = [
                str(check.get("name") or index)
                for index, check in enumerate(report.get("checks") or [])
                if isinstance(check, dict) and not bool(check.get("passed"))
            ]
            passed = status == "ok" and not failed
            if status != "ok":
                findings.append(f"bridge_report status is not ok: {status or '<missing>'}")
            findings.extend(f"Bridge cross-tool check failed: {name}" for name in failed)
        return ValidatorReport(
            validator_id="em_bridge_report_passed",
            passed=passed,
            findings=[] if passed else findings,
            evidence_ref=evidence.evidence_ref.uri,
        )

    def _read_artifact_json(
        self,
        evidence: EvidenceBundle,
        artifact_name: str,
    ) -> dict[str, Any] | None:
        ref = evidence.artifacts.get(artifact_name)
        if not ref:
            return None
        try:
            value = self.artifact_store.get_json(
                ref,
                self.artifact_store.identity.actor("validator-stagea", "validator"),
                self.artifact_store.identity.issue_token(
                    self.artifact_store.identity.actor("validator-stagea", "validator"),
                    ["artifact:read"],
                ),
            )
        except Exception:
            return None
        return value if isinstance(value, dict) else None
