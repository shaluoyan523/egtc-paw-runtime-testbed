from __future__ import annotations

import uuid
import json
from pathlib import Path

from .artifact_store import ArtifactStore
from .models import (
    ActorIdentity,
    CapabilityToken,
    EvidenceBundle,
    NodeCapsule,
    WorkerResult,
    to_plain_dict,
)


class EvidenceCollector:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        actor: ActorIdentity,
        token: CapabilityToken,
    ) -> None:
        self.artifact_store = artifact_store
        self.actor = actor
        self.token = token

    def collect(
        self,
        node: NodeCapsule,
        worker_result: WorkerResult,
        workspace_diff: dict[str, list[str]],
        workspace: Path | None = None,
    ) -> EvidenceBundle:
        evidence_id = f"evidence-{uuid.uuid4().hex[:12]}"
        test_events = [
            event
            for event in worker_result.parsed_events
            if event.get("type") in {"test", "test_result"}
        ]
        workspace_test_report = self._workspace_test_report(workspace)
        if workspace_test_report:
            test_events.append(workspace_test_report)
        tool_artifacts = self._workspace_tool_artifacts(workspace, node.node_id)
        em_artifacts = self._workspace_em_artifacts(workspace, node.node_id)
        artifacts = {
            "log": worker_result.stdout_ref,
            "stderr": worker_result.stderr_ref,
            "worker_events": worker_result.event_refs[0],
            "sandbox_events": worker_result.sandbox_event_refs[0],
            "resource_report": worker_result.resource_report_ref,
            "diff": self.artifact_store.put_json(
                workspace_diff,
                {"kind": "workspace_diff", "node_id": node.node_id},
                self.actor,
                self.token,
            ),
            "test": self.artifact_store.put_json(
                {"events": test_events, "passed": any(e.get("passed") for e in test_events)},
                {"kind": "test_report", "node_id": node.node_id},
                self.actor,
                self.token,
            ),
        }
        artifacts.update(tool_artifacts)
        artifacts.update(em_artifacts)
        summary = {
            "evidence_id": evidence_id,
            "node_id": node.node_id,
            "worker_id": worker_result.worker_id,
            "worker_exit_code": worker_result.exit_code,
            "artifact_refs": {key: to_plain_dict(ref) for key, ref in artifacts.items()},
        }
        evidence_ref = self.artifact_store.put_json(
            summary,
            {"kind": "evidence_bundle", "node_id": node.node_id},
            self.actor,
            self.token,
        )
        return EvidenceBundle(
            evidence_id=evidence_id,
            node_id=node.node_id,
            worker_id=worker_result.worker_id,
            evidence_ref=evidence_ref,
            artifacts=artifacts,
        )

    def _workspace_test_report(self, workspace: Path | None) -> dict[str, object] | None:
        if not workspace:
            return None
        report_path = workspace / "phasea_test_result.json"
        if not report_path.exists():
            return None
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "type": "test_result",
                "name": "workspace_phasea_test_result",
                "passed": False,
                "error": str(exc),
            }
        if isinstance(report, dict):
            report.setdefault("type", "test_result")
            report.setdefault("name", "workspace_phasea_test_result")
            report["passed"] = bool(report.get("passed"))
            return report
        return {
            "type": "test_result",
            "name": "workspace_phasea_test_result",
            "passed": False,
            "error": "phasea_test_result.json is not a JSON object",
        }

    def _workspace_tool_artifacts(
        self,
        workspace: Path | None,
        node_id: str,
    ) -> dict[str, object]:
        if not workspace:
            return {}
        artifacts: dict[str, object] = {}
        audit_path = workspace / "tool_audit.jsonl"
        if audit_path.exists():
            artifacts["tool_audit"] = self.artifact_store.put_bytes(
                audit_path.read_bytes(),
                "application/jsonl",
                {"kind": "tool_audit", "node_id": node_id},
                self.actor,
                self.token,
            )
        evidence_path = workspace / "tool_evidence.json"
        if evidence_path.exists():
            try:
                tool_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            except Exception as exc:
                tool_evidence = {"error": str(exc), "valid": False}
            artifacts["tool_evidence"] = self.artifact_store.put_json(
                tool_evidence,
                {"kind": "tool_evidence", "node_id": node_id},
                self.actor,
                self.token,
            )
        return artifacts

    def _workspace_em_artifacts(
        self,
        workspace: Path | None,
        node_id: str,
    ) -> dict[str, object]:
        if not workspace:
            return {}
        artifacts: dict[str, object] = {}

        json_artifacts = {
            "bridge_report": [workspace / "bridge_report.json"],
            "geometry_manifest": [workspace / "geometry_manifest.json"],
            "input_manifest": [workspace / "input_manifest.json"],
            "solver_report": [workspace / "solver_report.json"],
            "validation_report": [workspace / "validation_report.json"],
            "optimizer_report": [workspace / "optimizer_report.json"],
            "mesh_report": [workspace / "mesh_report.json", workspace / "results" / "mesh_report.json"],
        }
        for key, candidates in json_artifacts.items():
            path = self._first_existing(candidates)
            if path is not None:
                artifacts[key] = self._put_json_or_bytes(path, key, node_id)

        log_path = self._first_existing(
            [
                workspace / "hfss_patch_antenna_log.json",
                workspace / "hfss_run_log.json",
                *sorted(workspace.glob("*.ansysedt.log")),
            ]
        )
        if log_path is not None:
            artifacts["solver_log"] = self.artifact_store.put_bytes(
                log_path.read_bytes(),
                "text/plain",
                {"kind": "solver_log", "node_id": node_id, "source_path": str(log_path)},
                self.actor,
                self.token,
            )

        sparameter_candidates = [
            *sorted(workspace.glob("*.s1p")),
            *sorted(workspace.glob("*.s2p")),
            *sorted(workspace.glob("*.s3p")),
        ]
        results_dir = workspace / "results"
        if results_dir.exists():
            sparameter_candidates.extend(sorted(results_dir.glob("*.s1p")))
            sparameter_candidates.extend(sorted(results_dir.glob("*.s2p")))
            sparameter_candidates.extend(sorted(results_dir.glob("*.s3p")))
        sparameter_path = self._first_existing(sparameter_candidates)
        if sparameter_path is not None:
            artifacts["sparameters"] = self.artifact_store.put_bytes(
                sparameter_path.read_bytes(),
                "text/plain",
                {"kind": "sparameters", "node_id": node_id, "source_path": str(sparameter_path)},
                self.actor,
                self.token,
            )

        project_path = self._first_existing([*sorted(workspace.glob("*.aedt"))])
        if project_path is not None:
            artifacts["hfss_project"] = self.artifact_store.put_bytes(
                str(project_path).encode("utf-8"),
                "text/uri-list",
                {"kind": "hfss_project", "node_id": node_id, "source_path": str(project_path)},
                self.actor,
                self.token,
            )
        return artifacts

    def _first_existing(self, candidates: list[Path]) -> Path | None:
        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    def _put_json_or_bytes(self, path: Path, kind: str, node_id: str):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return self.artifact_store.put_bytes(
                path.read_bytes(),
                "application/octet-stream",
                {"kind": kind, "node_id": node_id, "source_path": str(path)},
                self.actor,
                self.token,
            )
        return self.artifact_store.put_json(
            data,
            {"kind": kind, "node_id": node_id, "source_path": str(path)},
            self.actor,
            self.token,
        )
