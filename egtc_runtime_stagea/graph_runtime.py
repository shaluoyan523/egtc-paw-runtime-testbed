from __future__ import annotations

import shutil
import time
import uuid
import json
import copy
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .agent_wrapper import AgentExecWrapper
from .artifact_store import ArtifactStore
from .compiler import WorkflowCompiler
from .event_log import EventLog
from .experience import (
    ExperienceLibrary,
    ExperienceObservation,
    WorkflowExperienceObservation,
)
from .evidence import EvidenceCollector
from .identity import IdentityService
from .models import (
    CompiledGraphPatch,
    ConflictResolution,
    DecisionConflict,
    EvidenceBundle,
    GraphPatch,
    GraphPatchOperation,
    NodeCapsule,
    NodeState,
    OverlookerReport,
    ValidatorReport,
    WorkerResult,
    to_plain_dict,
)
from .phaseb_models import WorkflowBlueprint
from .overlooker import CodexOverlooker, ModelOverlooker
from .validators import DeterministicValidator
from .workspace_diff import diff_snapshots, snapshot_workspace


TERMINAL_STATUSES = {
    "NODE_ACCEPTED",
    "NODE_REJECTED",
    "NODE_BLOCKED",
    "NODE_ABORTED",
}

PHASE_E_BRANCH_READY = "NODE_BRANCH_READY"


@dataclass
class GraphRunSpec:
    graph_id: str
    nodes: list[NodeCapsule]
    edges: list[tuple[str, str]]
    max_parallelism: int = 2
    max_attempts: int = 1
    retry_budget: int = 0
    max_same_failure_retries: int = 2
    overlooker_mode: str = "deterministic"
    director_mode: str = "deterministic"
    replan_budget: int = 0
    phase: str = "D"
    second_overlooker_mode: str = "deterministic"
    integration_overlooker_mode: str = "deterministic"
    scaling_policy: dict[str, Any] = field(default_factory=dict)


def graph_spec_from_blueprint(
    blueprint: WorkflowBlueprint,
    *,
    max_parallelism: int = 2,
    max_attempts: int = 1,
    retry_budget: int = 0,
    max_same_failure_retries: int = 2,
    overlooker_mode: str = "deterministic",
    director_mode: str | None = None,
    replan_budget: int = 0,
    phase: str = "F",
    second_overlooker_mode: str = "deterministic",
    integration_overlooker_mode: str = "deterministic",
) -> GraphRunSpec:
    """Create a runtime graph spec without losing Director planning metadata."""

    return GraphRunSpec(
        graph_id=blueprint.blueprint_id,
        nodes=[inst.node for inst in blueprint.node_instantiations],
        edges=list(blueprint.workflow_skeleton.edges),
        max_parallelism=max_parallelism,
        max_attempts=max_attempts,
        retry_budget=retry_budget,
        max_same_failure_retries=max_same_failure_retries,
        overlooker_mode=overlooker_mode,
        director_mode=director_mode or blueprint.director_mode,
        replan_budget=replan_budget,
        phase=phase,
        second_overlooker_mode=second_overlooker_mode,
        integration_overlooker_mode=integration_overlooker_mode,
        scaling_policy=dict(blueprint.workflow_skeleton.scaling_policy),
    )


@dataclass
class GraphNodeRecord:
    node_id: str
    status: str = "NODE_PLANNED"
    attempts: int = 0
    depends_on: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    read_only: bool = True
    write_paths: list[str] = field(default_factory=list)
    workspace: str | None = None
    current_worker_id: str | None = None
    final_state: str | None = None
    failure_code: str | None = None
    failure_counts: dict[str, int] = field(default_factory=dict)
    evidence_ref: str | None = None
    resource_report_ref: str | None = None
    sandbox_events_ref: str | None = None
    overlooker_verdict: str | None = None
    overlooker_report_ref: str | None = None
    overlooker_recommended_action: str | None = None
    overlooker_failure_type: str | None = None
    overlooker_confidence: str | None = None
    release_overlooker: bool = False
    high_risk: bool = False
    second_overlooker_report_ref: str | None = None
    conflict_resolution: dict[str, Any] | None = None
    conflict_history: list[dict[str, Any]] = field(default_factory=list)
    branch_name: str | None = None
    branch_workspace: str | None = None
    branch_candidate_ref: str | None = None
    branch_candidate: dict[str, Any] | None = None
    integration_report_ref: str | None = None
    integration_decision: dict[str, Any] | None = None
    human_review_required: bool = False
    permission_escalation_required: bool = False
    current_workspace: str | None = None
    accepted_workspace: str | None = None
    fork_source_node_id: str | None = None
    fork_source_workspace: str | None = None
    fork_history: list[dict[str, Any]] = field(default_factory=list)
    fork_advisor_history: list[dict[str, Any]] = field(default_factory=list)
    graph_patch_history: list[dict[str, Any]] = field(default_factory=list)
    experience_pattern_ids: list[str] = field(default_factory=list)
    experience_observations: list[dict[str, Any]] = field(default_factory=list)
    experience_update_proposals: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class NodeExecutionResult:
    node_id: str
    attempt: int
    workspace: str
    final_state: str
    worker_result: WorkerResult
    evidence: EvidenceBundle
    validator_reports: list[ValidatorReport]
    overlooker_report: OverlookerReport
    second_overlooker_report: OverlookerReport | None
    conflict: DecisionConflict | None
    conflict_resolution: ConflictResolution | None
    workspace_diff: dict[str, list[str]]
    experience_pattern_ids: list[str]


@dataclass(frozen=True)
class WorkspaceForkPlan:
    attempt: int
    workspace: Path
    source_node_id: str | None
    source_workspace: Path | None
    candidate_node_ids: list[str]
    reason: str


class GraphRuntime:
    """Phase D DAG scheduler built on the Stage A/C node execution chain."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = IdentityService()
        self.runtime_actor = self.identity.actor("runtime-phased", "runtime")
        self.runtime_token = self.identity.issue_token(
            self.runtime_actor,
            ["artifact:read", "artifact:write"],
        )
        self.director_actor = self.identity.actor("director-phased", "director")
        self.director_token = self.identity.issue_token(
            self.director_actor,
            ["artifact:read", "artifact:write"],
        )
        self.artifacts = ArtifactStore(self.root / "artifacts", self.identity)
        self.event_log = EventLog(self.root / "events.sqlite3")
        self.wrapper = AgentExecWrapper(
            self.artifacts, self.runtime_actor, self.runtime_token
        )
        self.director_wrapper = AgentExecWrapper(
            self.artifacts, self.director_actor, self.director_token
        )
        self.compiler = WorkflowCompiler()
        self.experience_library = ExperienceLibrary(self.root / "experience")
        self.experience_library.seed_defaults()
        self.collector = EvidenceCollector(
            self.artifacts, self.runtime_actor, self.runtime_token
        )
        self.validator = DeterministicValidator(self.artifacts)
        self.overlooker = CodexOverlooker(
            self.artifacts, self.runtime_actor, self.runtime_token, self.wrapper
        )
        self.model_overlooker = ModelOverlooker(
            self.artifacts,
            self.runtime_actor,
            self.runtime_token,
            self.wrapper,
        )

    def run_graph(
        self,
        spec: GraphRunSpec,
        run_id: str | None = None,
        stop_after_accepted: int | None = None,
    ) -> dict[str, Any]:
        self._validate_spec(spec)
        run_id = run_id or f"graph-{uuid.uuid4().hex[:12]}"
        records = self._initial_records(spec)
        return self._run_loop(spec, run_id, records, stop_after_accepted)

    def resume_graph(
        self,
        run_id: str,
        stop_after_accepted: int | None = None,
    ) -> dict[str, Any]:
        checkpoint = self._read_checkpoint(run_id)
        spec = self._spec_from_checkpoint(checkpoint)
        records = {
            node_id: GraphNodeRecord(**record)
            for node_id, record in checkpoint["nodes"].items()
        }
        for record in records.values():
            if record.status == "WORKER_RUNNING":
                record.status = "NODE_PLANNED"
                record.current_worker_id = None
        return self._run_loop(spec, run_id, records, stop_after_accepted)

    def _run_loop(
        self,
        spec: GraphRunSpec,
        run_id: str,
        records: dict[str, GraphNodeRecord],
        stop_after_accepted: int | None,
    ) -> dict[str, Any]:
        active: dict[Future[NodeExecutionResult], str] = {}
        active_writer: str | None = None
        max_observed_parallelism = 0
        write_lock_waits: list[dict[str, Any]] = []
        retry_events: list[dict[str, Any]] = []
        pause_requested = False

        self._record(run_id, spec.graph_id, "GraphRunStarted", {"graph_id": spec.graph_id})
        self._write_checkpoint(run_id, spec, records, "running")

        with ThreadPoolExecutor(max_workers=max(1, spec.max_parallelism)) as executor:
            while True:
                accepted_count = sum(
                    1 for record in records.values() if record.status == "NODE_ACCEPTED"
                )
                if (
                    stop_after_accepted is not None
                    and accepted_count >= stop_after_accepted
                    and not active
                ):
                    pause_requested = True
                    break

                made_progress = False
                while len(active) < spec.max_parallelism:
                    candidate = self._next_runnable(spec, records)
                    if candidate is None:
                        break
                    record = records[candidate]
                    if not self._can_start(record, active, active_writer):
                        wait_event = {
                            "node_id": record.node_id,
                            "active_writer": active_writer,
                            "active_nodes": sorted(active.values()),
                            "write_paths": record.write_paths,
                        }
                        if not write_lock_waits or write_lock_waits[-1] != wait_event:
                            write_lock_waits.append(wait_event)
                            self._record(run_id, record.node_id, "WriteConflictWait", wait_event)
                        break
                    record.status = "WORKER_RUNNING"
                    record.attempts += 1
                    nodes = {node.node_id: node for node in spec.nodes}
                    fork_plan = self._select_fork_plan(
                        run_id,
                        nodes[candidate],
                        record,
                        records,
                        spec.overlooker_mode,
                    )
                    if not record.read_only:
                        active_writer = record.node_id
                        self._record(
                            run_id,
                            record.node_id,
                            "WriteLockAcquired",
                            {"write_paths": record.write_paths},
                        )
                    self._record(
                        run_id,
                        record.node_id,
                        "NodeStateChanged",
                        {"state": "WORKER_RUNNING", "attempt": record.attempts},
                    )
                    future = executor.submit(
                        self._execute_node,
                        run_id,
                        nodes[candidate],
                        spec.overlooker_mode,
                        spec.phase,
                        spec.second_overlooker_mode,
                        fork_plan,
                    )
                    active[future] = candidate
                    max_observed_parallelism = max(max_observed_parallelism, len(active))
                    made_progress = True

                if not active:
                    if self._all_terminal(records):
                        break
                    if not made_progress:
                        self._mark_blocked(run_id, records)
                        break
                    continue

                done, _pending = wait(active, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    node_id = active.pop(future)
                    record = records[node_id]
                    if active_writer == node_id:
                        active_writer = None
                        self._record(
                            run_id,
                            node_id,
                            "WriteLockReleased",
                            {"write_paths": record.write_paths},
                        )
                    try:
                        result = future.result()
                    except Exception as exc:  # pragma: no cover - defensive runtime path
                        result = None
                        failure_code = type(exc).__name__
                        record.failure_code = failure_code
                        record.failure_counts[failure_code] = (
                            record.failure_counts.get(failure_code, 0) + 1
                        )
                        self._record(
                            run_id,
                            node_id,
                            "NodeExecutionErrored",
                            {"failure_code": failure_code, "message": str(exc)},
                        )
                    if result is not None:
                        self._apply_node_result(run_id, spec.graph_id, record, result)
                    if record.status == "NODE_REJECTED":
                        retry_event = self._maybe_retry(run_id, spec, record, records)
                        if retry_event:
                            retry_events.append(retry_event)
                    self._write_checkpoint(run_id, spec, records, "running")

        integration_result: dict[str, Any] | None = None
        if (
            not pause_requested
            and self._phase_e_enabled(spec.phase, spec.overlooker_mode)
            and self._ready_for_phase_e_integration(records)
        ):
            integration_result = self._run_phase_e_integration_gate(
                run_id,
                spec,
                records,
            )
        status = "paused" if pause_requested else self._graph_status(records)
        self._write_checkpoint(run_id, spec, records, status)
        summary = {
            "run_id": run_id,
            "graph_id": spec.graph_id,
            "status": status,
            "accepted": status == "accepted",
            "integration_result": integration_result,
            "max_parallelism": spec.max_parallelism,
            "max_observed_parallelism": max_observed_parallelism,
            "write_lock_waits": write_lock_waits,
            "retry_events": retry_events,
            "checkpoint_path": str(self._checkpoint_path(run_id)),
            "nodes": {node_id: to_plain_dict(record) for node_id, record in records.items()},
            "events": self.event_log.list_events(run_id),
        }
        if not pause_requested:
            summary["workflow_learning"] = self._record_workflow_experience_observation(
                run_id,
                spec,
                records,
                status,
                integration_result,
                retry_events,
                summary["events"],
            )
        self._record(run_id, spec.graph_id, "GraphRunCompleted", summary)
        return summary

    def _execute_node(
        self,
        run_id: str,
        node: NodeCapsule,
        overlooker_mode: str,
        phase: str,
        second_overlooker_mode: str,
        fork_plan: WorkspaceForkPlan,
    ) -> NodeExecutionResult:
        workspace = self._prepare_workspace(node, fork_plan)
        before = snapshot_workspace(workspace)
        worker_result = self.wrapper.run(node, workspace, run_id=run_id)
        self._record(
            run_id,
            node.node_id,
            "NodeStateChanged",
            {"state": "WORKER_SUBMITTED", "worker_id": worker_result.worker_id},
        )
        after = snapshot_workspace(workspace)
        workspace_diff = diff_snapshots(before, after)
        evidence = self.collector.collect(node, worker_result, workspace_diff, workspace)
        self._record(run_id, node.node_id, "EvidenceCollected", to_plain_dict(evidence))
        validator_reports = self.validator.run(evidence, node)
        self._record(
            run_id,
            node.node_id,
            "DeterministicValidationCompleted",
            {"reports": to_plain_dict(validator_reports)},
        )
        if overlooker_mode in {"codex", "codex_phase_e"}:
            overlooker_report = self.overlooker.review(
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                self.root
                / "runs"
                / run_id
                / "nodes"
                / node.node_id
                / f"attempt-{fork_plan.attempt}"
                / "overlooker",
            )
        elif overlooker_mode in {"model_agent", "model_phase_e"}:
            overlooker_report = self.model_overlooker.review(
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                self.root
                / "runs"
                / run_id
                / "nodes"
                / node.node_id
                / f"attempt-{fork_plan.attempt}"
                / "overlooker",
            )
        else:
            overlooker_report = self._deterministic_review(
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace,
            )
        second_overlooker_report: OverlookerReport | None = None
        conflict: DecisionConflict | None = None
        resolution: ConflictResolution | None = None
        high_risk = self._is_high_risk(node)
        phase_e_enabled = self._phase_e_enabled(phase, overlooker_mode)
        if phase_e_enabled and high_risk:
            second_overlooker_report = self._second_overlooker_review(
                run_id,
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                fork_plan,
                second_overlooker_mode,
            )
            conflict = self._build_conflict(
                run_id,
                node,
                validator_reports,
                [overlooker_report, second_overlooker_report],
                high_risk,
            )
            self._record(
                run_id,
                node.node_id,
                "PhaseEBranchGateReviewed",
                {
                    "conflict": to_plain_dict(conflict),
                    "resolution": None,
                    "note": "Phase E defers final integration to the integration Overlooker.",
                },
            )

        if resolution is not None:
            final_state = (
                NodeState.NODE_ACCEPTED.value
                if resolution.release_node
                else NodeState.NODE_REJECTED.value
            )
        elif phase_e_enabled:
            validators_passed = all(report.passed for report in validator_reports)
            branch_gate_passed = (
                worker_result.exit_code == 0
                and validators_passed
                and bool(evidence.evidence_ref.uri)
                and (
                    not high_risk
                    or (
                        second_overlooker_report is not None
                        and second_overlooker_report.evidence_ref is not None
                    )
                )
            )
            final_state = (
                PHASE_E_BRANCH_READY
                if branch_gate_passed
                else NodeState.NODE_REJECTED.value
            )
        else:
            final_state = (
                NodeState.NODE_ACCEPTED.value
                if overlooker_report.verdict == "pass"
                else NodeState.NODE_REJECTED.value
            )
        return NodeExecutionResult(
            node_id=node.node_id,
            attempt=fork_plan.attempt,
            workspace=str(workspace),
            final_state=final_state,
            worker_result=worker_result,
            evidence=evidence,
            validator_reports=validator_reports,
            overlooker_report=overlooker_report,
            second_overlooker_report=second_overlooker_report,
            conflict=conflict,
            conflict_resolution=resolution,
            workspace_diff=workspace_diff,
            experience_pattern_ids=list(node.experience_pattern_ids),
        )

    def _apply_node_result(
        self,
        run_id: str,
        graph_id: str,
        record: GraphNodeRecord,
        result: NodeExecutionResult,
    ) -> None:
        record.current_worker_id = result.worker_result.worker_id
        record.final_state = result.final_state
        record.current_workspace = result.workspace
        record.evidence_ref = result.evidence.evidence_ref.uri
        resource_ref = result.evidence.artifacts.get("resource_report")
        sandbox_ref = result.evidence.artifacts.get("sandbox_events")
        record.resource_report_ref = resource_ref.uri if resource_ref else None
        record.sandbox_events_ref = sandbox_ref.uri if sandbox_ref else None
        validators_passed = all(report.passed for report in result.validator_reports)
        record.overlooker_verdict = result.overlooker_report.verdict
        record.overlooker_report_ref = (
            result.overlooker_report.report_ref.uri
            if result.overlooker_report.report_ref
            else None
        )
        record.overlooker_recommended_action = result.overlooker_report.recommended_action
        record.overlooker_failure_type = result.overlooker_report.failure_type
        record.overlooker_confidence = result.overlooker_report.confidence
        record.release_overlooker = result.overlooker_report.release_overlooker
        record.high_risk = result.conflict.details.get("high_risk", False) if result.conflict else False
        record.second_overlooker_report_ref = (
            result.second_overlooker_report.report_ref.uri
            if result.second_overlooker_report and result.second_overlooker_report.report_ref
            else None
        )
        if result.conflict and result.conflict_resolution:
            conflict_event = {
                "conflict": to_plain_dict(result.conflict),
                "resolution": to_plain_dict(result.conflict_resolution),
            }
            record.conflict_history.append(conflict_event)
            record.conflict_resolution = to_plain_dict(result.conflict_resolution)
            if result.conflict_resolution.required_action == "request_permission_review":
                record.overlooker_recommended_action = "request_permission_review"
            elif result.conflict_resolution.required_action == "require_human_review":
                record.overlooker_recommended_action = "require_human_review"
        if result.final_state in {NodeState.NODE_ACCEPTED.value, PHASE_E_BRANCH_READY}:
            if result.final_state == PHASE_E_BRANCH_READY:
                self._record_branch_candidate(run_id, graph_id, record, result)
            record.status = "NODE_ACCEPTED"
            record.failure_code = None
            record.workspace = result.workspace
            record.accepted_workspace = result.workspace
            if result.final_state == PHASE_E_BRANCH_READY:
                record.status = PHASE_E_BRANCH_READY
        else:
            record.status = "NODE_REJECTED"
            record.failure_code = self._failure_code(result.worker_result, result.validator_reports)
            record.failure_counts[record.failure_code] = (
                record.failure_counts.get(record.failure_code, 0) + 1
            )
        self._record(
            run_id,
            record.node_id,
            "NodeStateChanged",
            {
                "state": record.status,
                "worker_id": record.current_worker_id,
                "validators_passed": validators_passed,
                "overlooker_verdict": result.overlooker_report.verdict,
                "overlooker_report_ref": record.overlooker_report_ref,
                "overlooker_recommended_action": record.overlooker_recommended_action,
                "overlooker_failure_type": record.overlooker_failure_type,
                "release_overlooker": record.release_overlooker,
                "second_overlooker_report_ref": record.second_overlooker_report_ref,
                "conflict_resolution": record.conflict_resolution,
            },
        )
        self._record_experience_observation(run_id, graph_id, record, result)

    def _maybe_retry(
        self,
        run_id: str,
        spec: GraphRunSpec,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
    ) -> dict[str, Any] | None:
        failure_code = record.failure_code or "unknown_failure"
        repeated = record.failure_counts.get(failure_code, 0)
        if repeated > spec.max_same_failure_retries:
            record.status = "NODE_ABORTED"
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "repeated": repeated,
                "action": "abort_livelock",
            }
            self._record(run_id, record.node_id, "LivelockDetected", event)
            return event
        recommended_action = record.overlooker_recommended_action or "retry_same_node"
        retry_actions = {"retry_same_node", "retry_with_modified_instruction"}
        replan_actions = {"request_director_replan"}
        if recommended_action not in retry_actions | replan_actions:
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "recommended_action": recommended_action,
                "action": "blocked_by_overlooker_recommendation",
            }
            self._record(run_id, record.node_id, "NodeRetryNotScheduled", event)
            return event
        if recommended_action in replan_actions and spec.replan_budget <= 0:
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "recommended_action": recommended_action,
                "action": "blocked_replan_budget_exhausted",
            }
            self._record(run_id, record.node_id, "NodeRetryNotScheduled", event)
            return event
        has_retry_budget = recommended_action in retry_actions and spec.retry_budget > 0
        has_replan_budget = recommended_action in replan_actions and spec.replan_budget > 0
        if record.attempts < spec.max_attempts and (has_retry_budget or has_replan_budget):
            patch = self._propose_retry_patch(run_id, spec, record, records)
            compiled = self.compiler.validate_patch(
                patch,
                set(records),
                spec.graph_id,
                spec.phase,
            )
            patch_ref = self.artifacts.put_json(
                to_plain_dict(patch),
                {
                    "kind": f"phase_{spec.phase.lower()}_graph_patch",
                    "graph_id": spec.graph_id,
                    "node_id": record.node_id,
                },
                self.director_actor,
                self.director_token,
            )
            compiled_ref = self.artifacts.put_json(
                to_plain_dict(compiled),
                {
                    "kind": f"phase_{spec.phase.lower()}_compiled_graph_patch",
                    "graph_id": spec.graph_id,
                    "node_id": record.node_id,
                    "patch_id": patch.patch_id,
                },
                self.runtime_actor,
                self.runtime_token,
            )
            patch_event = {
                "patch": to_plain_dict(patch),
                "compiled": to_plain_dict(compiled),
                "patch_ref": to_plain_dict(patch_ref),
                "compiled_patch_ref": to_plain_dict(compiled_ref),
            }
            record.graph_patch_history.append(patch_event)
            self._record(run_id, record.node_id, "DirectorGraphPatchProposed", patch_event)
            if not compiled.accepted:
                record.status = "NODE_ABORTED"
                event = {
                    "node_id": record.node_id,
                    "failure_code": failure_code,
                    "patch_id": patch.patch_id,
                    "findings": compiled.findings,
                    "action": "abort_invalid_graph_patch",
                }
                self._record(run_id, record.node_id, "GraphPatchRejected", event)
                return event

            applied = self._apply_graph_patch(run_id, spec, compiled, record, records)
            if not applied:
                event = {
                    "node_id": record.node_id,
                    "failure_code": failure_code,
                    "patch_id": patch.patch_id,
                    "action": "graph_patch_noop",
                }
                self._record(run_id, record.node_id, "GraphPatchNoop", event)
                return event
            if recommended_action in replan_actions:
                spec.replan_budget = max(0, spec.replan_budget - patch.replan_budget_cost)
            else:
                spec.retry_budget = max(0, spec.retry_budget - 1)
            event = {
                "node_id": record.node_id,
                "failure_code": failure_code,
                "attempts": record.attempts,
                "remaining_retry_budget": spec.retry_budget,
                "remaining_replan_budget": spec.replan_budget,
                "patch_id": patch.patch_id,
                "recommended_action": recommended_action,
                "action": "retry_via_director_graph_patch",
            }
            self._record(run_id, record.node_id, "NodeRetryScheduled", event)
            return event
        return None

    def _propose_retry_patch(
        self,
        run_id: str,
        spec: GraphRunSpec,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
    ) -> GraphPatch:
        if spec.director_mode not in {"codex", "model_agent"}:
            if (
                spec.phase.upper() != "D"
                and record.overlooker_recommended_action == "request_director_replan"
            ):
                inserted_node_id = f"{record.node_id}-phase-e-diagnostic"
                return GraphPatch(
                    patch_id=f"graph-patch-{uuid.uuid4().hex[:12]}",
                    director_id=self.director_actor.actor_id,
                    graph_id=spec.graph_id,
                    triggering_node_id=record.node_id,
                    triggering_event="overlooker_rejected_node",
                    overlooker_report_ref=record.overlooker_report_ref,
                    operations=[
                        GraphPatchOperation(
                            op="insert_node",
                            node_id=inserted_node_id,
                            value={
                                "node": {
                                    "node_id": inserted_node_id,
                                    "phase": "diagnosis",
                                    "goal": "Perform targeted read-only diagnosis before retrying the rejected node.",
                                    "command": [
                                        "python3",
                                        "-c",
                                        (
                                            "from pathlib import Path; import json; "
                                            "Path('phase_e_diagnostic.txt').write_text('diagnostic completed\\n'); "
                                            "Path('phasea_test_result.json').write_text(json.dumps({'passed': True, 'name': 'phase_e_diagnostic'})); "
                                            "print(json.dumps({'type':'test_result','name':'phase_e_diagnostic','passed':True}))"
                                        ),
                                    ],
                                    "acceptance_criteria": [
                                        "Diagnostic node must produce read-only evidence for the retried node.",
                                        "Overlooker must cite diagnostic evidence_ref.",
                                    ],
                                    "required_evidence": [
                                        "test",
                                        "log",
                                        "sandbox_events",
                                        "resource_report",
                                    ],
                                }
                            },
                            rationale="Insert targeted diagnostic node before retrying after Director replan request.",
                        ),
                        GraphPatchOperation(
                            op="add_edge",
                            source_node_id=inserted_node_id,
                            target_node_id=record.node_id,
                            rationale="Retried node consumes diagnostic evidence before rerun.",
                        ),
                        GraphPatchOperation(
                            op="retry_node",
                            node_id=record.node_id,
                            value={
                                "failure_code": record.failure_code,
                                "recommended_action": record.overlooker_recommended_action,
                            },
                            rationale="Retry rejected node after Phase E diagnostic insertion.",
                        ),
                    ],
                    rationale="Deterministic Director selected Phase E replan with diagnostic insertion.",
                    replan_budget_cost=1,
                )
            return GraphPatch(
                patch_id=f"graph-patch-{uuid.uuid4().hex[:12]}",
                director_id=self.director_actor.actor_id,
                graph_id=spec.graph_id,
                triggering_node_id=record.node_id,
                triggering_event="overlooker_rejected_node",
                overlooker_report_ref=record.overlooker_report_ref,
                operations=[
                    GraphPatchOperation(
                        op="retry_node",
                        node_id=record.node_id,
                        value={
                            "failure_code": record.failure_code,
                            "recommended_action": record.overlooker_recommended_action,
                        },
                        rationale="Retry the rejected node through a compiler-validated Stage D GraphPatch.",
                    )
                ],
                rationale="Deterministic Director selected bounded retry for the rejected node.",
            )

        director_workspace = (
            self.root
            / "runs"
            / run_id
            / "nodes"
            / record.node_id
            / f"after-attempt-{record.attempts}"
            / "director"
        )
        director_workspace.mkdir(parents=True, exist_ok=True)
        runtime_state = {
            "run_id": run_id,
            "graph_id": spec.graph_id,
            "triggering_node": to_plain_dict(record),
            "known_node_ids": sorted(records),
            "node_statuses": {
                node_id: {
                    "status": node_record.status,
                    "attempts": node_record.attempts,
                    "accepted_workspace": node_record.accepted_workspace,
                    "failure_code": node_record.failure_code,
                    "overlooker_recommended_action": node_record.overlooker_recommended_action,
                    "overlooker_report_ref": node_record.overlooker_report_ref,
                    "conflict_resolution": node_record.conflict_resolution,
                }
                for node_id, node_record in records.items()
            },
            "policy": {
                "stage": f"Phase {spec.phase.upper()}",
                "allowed_operations": (
                    ["retry_node"]
                    if spec.phase.upper() == "D"
                    else [
                        "retry_node",
                        "replace_worker",
                        "split_node",
                        "insert_node",
                        "add_edge",
                        "remove_edge",
                        "update_join_policy",
                    ]
                ),
                "forbidden": [
                    "Do not change permissions or sandbox_profile.",
                    "Do not skip the Overlooker gate.",
                    (
                        "Retry only the triggering node."
                        if spec.phase.upper() == "D"
                        else "Only change graph topology through compiler-valid Phase E operations."
                    ),
                ],
            },
        }
        (director_workspace / "director_runtime_state.json").write_text(
            json.dumps(runtime_state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        director_node = NodeCapsule(
            node_id=f"{record.node_id}-director-graph-patch",
            phase=f"Phase {spec.phase.upper()} Director",
            goal=f"Create a compiler-valid Phase {spec.phase.upper()} GraphPatch for the rejected node.",
            command=[],
            acceptance_criteria=[
                "Director must write strict JSON to graph_patch.json.",
                "Director may only use operations allowed by director_runtime_state.json.",
                "Director must not change permissions, sandbox policy, or Overlooker gates.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="model_agent" if spec.director_mode == "model_agent" else "codex_cli",
            prompt=self._director_patch_prompt(spec.phase),
            model_provider="deterministic" if spec.director_mode == "model_agent" else None,
            model_config=(
                {
                    "input_files": ["director_runtime_state.json"],
                    "output_file": "graph_patch.json",
                    "output_json": True,
                }
                if spec.director_mode == "model_agent"
                else {}
            ),
            sandbox_profile={
                "backend": "model_agent" if spec.director_mode == "model_agent" else "codex_native",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": 240,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 48,
                    "max_command_count": 1,
                },
            },
        )
        director_result = self.director_wrapper.run(
            director_node,
            director_workspace,
            role="director",
            run_id=run_id,
        )
        data = self._read_graph_patch(director_workspace / "graph_patch.json")
        patch = self._graph_patch_from_data(
            data,
            spec,
            record,
        )
        self._record(
            run_id,
            record.node_id,
            "DirectorGraphPatchSessionCompleted",
            {
                "director_id": self.director_actor.actor_id,
                "director_session_id": director_result.worker_id,
                "exit_code": director_result.exit_code,
                "workspace": str(director_workspace),
                "event_refs": [to_plain_dict(ref) for ref in director_result.event_refs],
                "patch_id": patch.patch_id,
            },
        )
        return patch

    def _director_patch_prompt(self, phase: str = "D") -> str:
        phase_name = phase.upper()
        if phase_name == "D":
            operation_schema = """
    {
      "op": "retry_node",
      "node_id": "...",
      "source_node_id": null,
      "target_node_id": null,
      "value": {
        "failure_code": "...",
        "recommended_action": "..."
      },
      "rationale": "short reason"
    }
""".strip()
            rules = """
- Use exactly one retry_node operation.
- Retry only the triggering node from director_runtime_state.json.
- Do not modify permissions, sandbox_profile, network policy, graph edges, or Overlooker gates.
""".strip()
        else:
            operation_schema = """
    {
      "op": "retry_node | replace_worker | split_node | insert_node | add_edge | remove_edge | update_join_policy",
      "node_id": "target node id or inserted node id",
      "source_node_id": "edge source or null",
      "target_node_id": "edge target or null",
      "value": {
        "prompt": "replacement prompt when using replace_worker",
        "executor_kind": "replacement executor when using replace_worker",
        "node": {
          "node_id": "new-node-id",
          "phase": "verification",
          "goal": "why this inserted node exists",
          "command": [],
          "acceptance_criteria": ["Overlooker acceptance criterion"],
          "required_evidence": ["test", "log"]
        },
        "nodes": [
          {
            "node_id": "split-subtask-id",
            "phase": "verification",
            "goal": "why this split node exists",
            "command": [],
            "acceptance_criteria": ["Overlooker acceptance criterion"],
            "required_evidence": ["test", "log"]
          }
        ],
        "join_policy": "all_success"
      },
      "rationale": "short reason"
    }
""".strip()
            rules = """
- Use the smallest compiler-valid operation set that addresses the failure.
- Prefer retry_node for a transient failure, replace_worker for bad prompt/executor, insert_node for missing verification/research, split_node for overloaded ownership, and edge changes only when dependency order is wrong.
- Inserted or split nodes must include node_id, phase, goal, acceptance_criteria, and required_evidence.
- When repairing a rejected triggering node, the patch must create a re-entry path: either include retry_node for the triggering node, replace_worker for the triggering node, or add edges that make the inserted/split node a dependency of the triggering node and then retry the triggering node.
- Do not modify permissions, sandbox_profile, network policy, or Overlooker gates.
- Use update_join_policy only as a recorded planning hint; runtime may not reschedule from it yet.
""".strip()
        return """
You are the EGTC-PAW Phase {phase_name} Director Agent.

Read ./director_runtime_state.json and create ./graph_patch.json.

Output strict JSON:
{{
  "patch_id": "graph-patch-...",
  "director_id": "codex-director",
  "graph_id": "...",
  "triggering_node_id": "...",
  "triggering_event": "overlooker_rejected_node",
  "overlooker_report_ref": "artifact://..." | null,
  "operations": [
{operation_schema}
  ],
  "rationale": "short reason",
  "replan_budget_cost": 1
}}

Rules:
{rules}
- Do not clone repositories.
- Do not use network.
- Do not write outside this workspace.
""".format(
            phase_name=phase_name,
            operation_schema=operation_schema,
            rules=rules,
        ).strip()

    def _read_graph_patch(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"operations": [{"op": "missing_director_output"}]}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "rationale": f"Director graph_patch.json was not valid JSON: {exc}",
                "operations": [{"op": "invalid_director_output"}],
            }
        return data if isinstance(data, dict) else {"operations": [{"op": "invalid_director_output"}]}

    def _graph_patch_from_data(
        self,
        data: dict[str, Any],
        spec: GraphRunSpec,
        record: GraphNodeRecord,
    ) -> GraphPatch:
        raw_operations = data.get("operations")
        operations: list[GraphPatchOperation] = []
        if isinstance(raw_operations, list):
            for raw in raw_operations:
                if not isinstance(raw, dict):
                    continue
                value = raw.get("value")
                operations.append(
                    GraphPatchOperation(
                        op=str(raw.get("op") or ""),
                        node_id=str(raw["node_id"]) if raw.get("node_id") is not None else None,
                        source_node_id=str(raw["source_node_id"])
                        if raw.get("source_node_id") is not None
                        else None,
                        target_node_id=str(raw["target_node_id"])
                        if raw.get("target_node_id") is not None
                        else None,
                        value=value if isinstance(value, dict) else {},
                        rationale=str(raw.get("rationale") or ""),
                    )
                )
        if not operations:
            operations = [GraphPatchOperation(op="missing_director_output", node_id=record.node_id)]

        return GraphPatch(
            patch_id=str(data.get("patch_id") or f"graph-patch-{uuid.uuid4().hex[:12]}"),
            director_id=self.director_actor.actor_id,
            graph_id=str(data.get("graph_id") or spec.graph_id),
            triggering_node_id=str(data.get("triggering_node_id") or record.node_id),
            triggering_event=str(data.get("triggering_event") or "overlooker_rejected_node"),
            overlooker_report_ref=(
                str(data["overlooker_report_ref"])
                if data.get("overlooker_report_ref") is not None
                else record.overlooker_report_ref
            ),
            operations=operations,
            rationale=str(data.get("rationale") or f"Director proposed a Phase {spec.phase.upper()} graph patch."),
            replan_budget_cost=int(data.get("replan_budget_cost") or 1),
        )

    def _apply_graph_patch(
        self,
        run_id: str,
        spec: GraphRunSpec,
        compiled: CompiledGraphPatch,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
    ) -> bool:
        applied = False
        phase_name = spec.phase.upper()
        original_nodes = copy.deepcopy(spec.nodes)
        original_edges = list(spec.edges)
        original_records = copy.deepcopy(records)
        for operation in compiled.operations:
            target_node_id = operation.node_id or operation.target_node_id
            if operation.op == "retry_node" and target_node_id == record.node_id:
                record.status = "NODE_PLANNED"
                record.current_worker_id = None
                record.final_state = None
                applied = True
            elif phase_name != "D" and operation.op == "replace_worker" and target_node_id:
                node = self._find_spec_node(spec, target_node_id)
                if node is None:
                    continue
                if isinstance(operation.value.get("prompt"), str):
                    node.prompt = str(operation.value["prompt"])
                if isinstance(operation.value.get("executor_kind"), str):
                    node.executor_kind = str(operation.value["executor_kind"])
                if isinstance(operation.value.get("command"), list):
                    node.command = [str(item) for item in operation.value["command"]]
                if records[target_node_id].status in {"NODE_REJECTED", "NODE_ABORTED"}:
                    records[target_node_id].status = "NODE_PLANNED"
                    records[target_node_id].current_worker_id = None
                    records[target_node_id].final_state = None
                applied = True
            elif phase_name != "D" and operation.op == "insert_node":
                new_node = self._node_from_patch_payload(operation.value.get("node"))
                if new_node is None or new_node.node_id in records:
                    continue
                spec.nodes.append(new_node)
                self._ensure_record_for_node(new_node, records)
                applied = True
            elif phase_name != "D" and operation.op == "split_node" and target_node_id:
                split_nodes = operation.value.get("nodes")
                if not isinstance(split_nodes, list):
                    continue
                previous_dependents = [
                    edge_target
                    for edge_source, edge_target in spec.edges
                    if edge_source == target_node_id
                ]
                inserted_ids: list[str] = []
                for raw_node in split_nodes:
                    new_node = self._node_from_patch_payload(raw_node)
                    if new_node is None or new_node.node_id in records:
                        continue
                    spec.nodes.append(new_node)
                    self._ensure_record_for_node(new_node, records)
                    inserted_ids.append(new_node.node_id)
                for dependent_id in previous_dependents:
                    self._remove_edge(spec, records, target_node_id, dependent_id)
                for new_node_id in inserted_ids:
                    self._add_edge(spec, records, target_node_id, new_node_id)
                    for dependent_id in previous_dependents:
                        self._add_edge(spec, records, new_node_id, dependent_id)
                if inserted_ids:
                    applied = True
            elif phase_name != "D" and operation.op == "add_edge":
                if operation.source_node_id and operation.target_node_id:
                    applied = self._add_edge(
                        spec,
                        records,
                        operation.source_node_id,
                        operation.target_node_id,
                    ) or applied
            elif phase_name != "D" and operation.op == "remove_edge":
                if operation.source_node_id and operation.target_node_id:
                    applied = self._remove_edge(
                        spec,
                        records,
                        operation.source_node_id,
                        operation.target_node_id,
                    ) or applied
            elif phase_name != "D" and operation.op == "update_join_policy" and target_node_id in records:
                records[target_node_id].graph_patch_history.append(
                    {
                        "patch_id": compiled.patch_id,
                        "operation": to_plain_dict(operation),
                        "note": "join_policy update recorded for future scheduler phases",
                    }
                )
                applied = True
        if applied:
            try:
                self._topological_order(spec)
            except ValueError as exc:
                spec.nodes = original_nodes
                spec.edges = original_edges
                records.clear()
                records.update(original_records)
                self._record(
                    run_id,
                    record.node_id,
                    "GraphPatchRolledBack",
                    {
                        "graph_id": spec.graph_id,
                        "patch_id": compiled.patch_id,
                        "reason": str(exc),
                    },
                )
                return False
            self._record(
                run_id,
                record.node_id,
                "GraphPatchApplied",
                {
                    "graph_id": spec.graph_id,
                    "patch_id": compiled.patch_id,
                    "operations": to_plain_dict(compiled.operations),
                },
            )
        return applied

    def _find_spec_node(self, spec: GraphRunSpec, node_id: str) -> NodeCapsule | None:
        for node in spec.nodes:
            if node.node_id == node_id:
                return node
        return None

    def _node_from_patch_payload(self, raw: Any) -> NodeCapsule | None:
        if not isinstance(raw, dict):
            return None
        node_id = str(raw.get("node_id") or "")
        if not node_id:
            return None
        command = raw.get("command")
        acceptance = raw.get("acceptance_criteria")
        evidence = raw.get("required_evidence")
        return NodeCapsule(
            node_id=node_id,
            phase=str(raw.get("phase") or "verification"),
            goal=str(raw.get("goal") or f"Phase E inserted node {node_id}."),
            command=[str(item) for item in command] if isinstance(command, list) else [],
            acceptance_criteria=(
                [str(item) for item in acceptance]
                if isinstance(acceptance, list)
                else ["Overlooker must cite evidence_ref before acceptance."]
            ),
            required_evidence=(
                [str(item) for item in evidence]
                if isinstance(evidence, list)
                else ["log", "sandbox_events", "resource_report"]
            ),
            experience_pattern_ids=[
                str(item)
                for item in (
                    raw.get("experience_pattern_ids")
                    if isinstance(raw.get("experience_pattern_ids"), list)
                    else []
                )
            ],
            executor_kind=str(raw.get("executor_kind") or "subprocess"),
            prompt=str(raw["prompt"]) if raw.get("prompt") is not None else None,
        )

    def _ensure_record_for_node(
        self,
        node: NodeCapsule,
        records: dict[str, GraphNodeRecord],
    ) -> None:
        records[node.node_id] = GraphNodeRecord(
            node_id=node.node_id,
            read_only=self._is_read_only(node),
            write_paths=self._write_paths(node),
            experience_pattern_ids=list(node.experience_pattern_ids),
        )

    def _add_edge(
        self,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
        source_node_id: str,
        target_node_id: str,
    ) -> bool:
        edge = (source_node_id, target_node_id)
        if edge in spec.edges:
            return False
        spec.edges.append(edge)
        if target_node_id in records and source_node_id not in records[target_node_id].depends_on:
            records[target_node_id].depends_on.append(source_node_id)
            records[target_node_id].depends_on.sort()
        if source_node_id in records and target_node_id not in records[source_node_id].dependents:
            records[source_node_id].dependents.append(target_node_id)
            records[source_node_id].dependents.sort()
        return True

    def _remove_edge(
        self,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
        source_node_id: str,
        target_node_id: str,
    ) -> bool:
        edge = (source_node_id, target_node_id)
        if edge not in spec.edges:
            return False
        spec.edges = [existing for existing in spec.edges if existing != edge]
        if target_node_id in records:
            records[target_node_id].depends_on = [
                node_id for node_id in records[target_node_id].depends_on
                if node_id != source_node_id
            ]
        if source_node_id in records:
            records[source_node_id].dependents = [
                node_id for node_id in records[source_node_id].dependents
                if node_id != target_node_id
            ]
        return True

    def _deterministic_review(
        self,
        node: NodeCapsule,
        evidence: EvidenceBundle,
        validator_reports: list[ValidatorReport],
        worker_result: WorkerResult,
        workspace: Path | None = None,
    ) -> OverlookerReport:
        validators_passed = all(report.passed for report in validator_reports)
        hint = self._read_overlooker_hint(workspace)
        hinted_action = str(hint.get("recommended_action") or "")
        blocking_hint = hinted_action in {
            "request_permission_review",
            "require_human_review",
            "require_second_overlooker",
        }
        can_pass = (
            worker_result.exit_code == 0
            and validators_passed
            and bool(evidence.evidence_ref.uri)
            and not blocking_hint
        )
        recommended_action = (
            "advance"
            if can_pass
            else str(hint.get("recommended_action") or "retry_same_node")
        )
        failure_type = (
            None
            if can_pass
            else str(hint.get("failure_type") or self._failure_code(worker_result, validator_reports))
        )
        report = {
            "verdict": "pass" if can_pass else "fail",
            "rationale": (
                "Deterministic Phase D overlooker accepted validator-backed evidence."
                if can_pass
                else "Deterministic Phase D overlooker rejected the node."
            ),
            "evidence_ref": evidence.evidence_ref.uri if can_pass else None,
            "cited_evidence": [evidence.evidence_ref.uri] if can_pass else [],
            "validator_refs": [report.validator_id for report in validator_reports],
            "confidence": "high",
            "failure_type": failure_type,
            "recommended_action": recommended_action,
            "release_overlooker": can_pass,
        }
        report_ref = self.artifacts.put_json(
            report,
            {"kind": "phased_deterministic_overlooker_report", "node_id": node.node_id},
            self.runtime_actor,
            self.runtime_token,
        )
        return OverlookerReport(
            overlooker_id=f"deterministic-overlooker-{uuid.uuid4().hex[:12]}",
            verdict=str(report["verdict"]),
            rationale=str(report["rationale"]),
            evidence_ref=report["evidence_ref"],
            validator_refs=list(report["validator_refs"]),
            report_ref=report_ref,
            agent_event_refs=[],
            codex_event_refs=[],
            confidence=str(report["confidence"]),
            cited_evidence=list(report["cited_evidence"]),
            failure_type=report["failure_type"],
            recommended_action=str(report["recommended_action"]),
            release_overlooker=bool(report["release_overlooker"]),
        )

    def _read_overlooker_hint(self, workspace: Path | None) -> dict[str, Any]:
        if workspace is None:
            return {}
        path = workspace / "overlooker_hint.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _phase_e_enabled(self, phase: str, overlooker_mode: str) -> bool:
        return phase.upper() == "E" or overlooker_mode in {"phase_e", "codex_phase_e", "model_phase_e"}

    def _is_high_risk(self, node: NodeCapsule) -> bool:
        profile = node.sandbox_profile or {}
        if bool(profile.get("high_risk")):
            return True
        risk = str(profile.get("risk_level") or "").lower()
        if risk in {"high", "critical"}:
            return True
        if node.phase.lower() in {"release", "deployment", "permission", "security"}:
            return True
        return bool(self._write_paths(node)) and bool(profile.get("requires_second_overlooker"))

    def _second_overlooker_review(
        self,
        run_id: str,
        node: NodeCapsule,
        evidence: EvidenceBundle,
        validator_reports: list[ValidatorReport],
        worker_result: WorkerResult,
        workspace_diff: dict[str, list[str]],
        fork_plan: WorkspaceForkPlan,
        second_overlooker_mode: str,
    ) -> OverlookerReport:
        if second_overlooker_mode == "codex":
            return self.overlooker.review(
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                self.root
                / "runs"
                / run_id
                / "nodes"
                / node.node_id
                / f"attempt-{fork_plan.attempt}"
                / "second-overlooker",
            )
        if second_overlooker_mode == "model_agent":
            return self.model_overlooker.review(
                node,
                evidence,
                validator_reports,
                worker_result,
                workspace_diff,
                self.root
                / "runs"
                / run_id
                / "nodes"
                / node.node_id
                / f"attempt-{fork_plan.attempt}"
                / "second-overlooker",
            )
        return self._deterministic_review(
            node,
            evidence,
            validator_reports,
            worker_result,
        )

    def _build_conflict(
        self,
        run_id: str,
        node: NodeCapsule,
        validator_reports: list[ValidatorReport],
        overlooker_reports: list[OverlookerReport],
        high_risk: bool,
    ) -> DecisionConflict:
        policy_findings = self._policy_findings(node)
        conflict_type = "high_risk_second_overlooker" if high_risk else "phase_e_gate"
        if policy_findings:
            conflict_type = "policy_conflict"
        elif any(not report.passed for report in validator_reports):
            conflict_type = "validator_conflict"
        elif len({report.verdict for report in overlooker_reports}) > 1:
            conflict_type = "overlooker_disagreement"
        return DecisionConflict(
            conflict_id=f"conflict-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            node_id=node.node_id,
            conflict_type=conflict_type,
            policy_findings=policy_findings,
            validator_reports=validator_reports,
            overlooker_reports=overlooker_reports,
            director_intent="advance",
            risk_level="high" if high_risk else "normal",
            details={
                "high_risk": high_risk,
                "policy_source": "sandbox_profile",
            },
        )

    def _policy_findings(self, node: NodeCapsule) -> list[dict[str, Any]]:
        profile = node.sandbox_profile or {}
        findings: list[dict[str, Any]] = []
        if profile.get("network") not in {None, "none"}:
            findings.append(
                {
                    "severity": "error",
                    "code": "network_requires_permission_review",
                    "message": "Phase E blocks non-none network until permission review.",
                    "node_id": node.node_id,
                }
            )
        sensitive_paths = profile.get("sensitive_paths") or [".env", ".git", "secrets"]
        write_paths = profile.get("allowed_write_paths") or []
        for path in write_paths:
            text = str(path).strip("/")
            if any(text == str(s).strip("/") or text.startswith(f"{str(s).strip('/')}/") for s in sensitive_paths):
                findings.append(
                    {
                        "severity": "error",
                        "code": "sensitive_write_requires_permission_review",
                        "message": f"Write path requires permission review: {path}",
                        "node_id": node.node_id,
                    }
                )
        return findings

    def _initial_records(self, spec: GraphRunSpec) -> dict[str, GraphNodeRecord]:
        dependencies: dict[str, list[str]] = {node.node_id: [] for node in spec.nodes}
        dependents: dict[str, list[str]] = {node.node_id: [] for node in spec.nodes}
        for source, target in spec.edges:
            dependencies[target].append(source)
            dependents[source].append(target)
        return {
            node.node_id: GraphNodeRecord(
                node_id=node.node_id,
                depends_on=sorted(dependencies[node.node_id]),
                dependents=sorted(dependents[node.node_id]),
                read_only=self._is_read_only(node),
                write_paths=self._write_paths(node),
                experience_pattern_ids=list(node.experience_pattern_ids),
            )
            for node in spec.nodes
        }

    def _record_experience_observation(
        self,
        run_id: str,
        graph_id: str,
        record: GraphNodeRecord,
        result: NodeExecutionResult,
    ) -> None:
        pattern_ids = list(result.experience_pattern_ids or record.experience_pattern_ids)
        if not pattern_ids:
            return
        if record.status in {"NODE_ACCEPTED", PHASE_E_BRANCH_READY}:
            outcome = "accepted"
            recommended_update = "promote"
        elif record.status == "NODE_ABORTED":
            outcome = "aborted"
            recommended_update = "demote"
        else:
            outcome = "rejected"
            recommended_update = "demote"
        evidence_refs = [result.evidence.evidence_ref.uri]
        if record.overlooker_report_ref:
            evidence_refs.append(record.overlooker_report_ref)
        observation = ExperienceObservation(
            observation_id=f"exp-observation-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            graph_id=graph_id,
            node_id=result.node_id,
            pattern_ids_used=pattern_ids,
            outcome=outcome,
            validator_findings=[
                {
                    "validator_id": report.validator_id,
                    "passed": report.passed,
                    "findings": report.findings,
                    "evidence_ref": report.evidence_ref,
                }
                for report in result.validator_reports
            ],
            overlooker_verdict=result.overlooker_report.verdict,
            failure_type=result.overlooker_report.failure_type or record.failure_code,
            recommended_update=recommended_update,
            evidence_refs=evidence_refs,
        )
        self.experience_library.record_observation(observation)
        proposals = self.experience_library.update_from_observation(
            observation,
            proposed_by="runtime",
        )
        observation_event = {
            "observation": to_plain_dict(observation),
            "update_proposals": [to_plain_dict(proposal) for proposal in proposals],
        }
        record.experience_observations.append(to_plain_dict(observation))
        record.experience_update_proposals.extend(
            to_plain_dict(proposal) for proposal in proposals
        )
        self._record(
            run_id,
            record.node_id,
            "ExperienceObservationRecorded",
            observation_event,
        )

    def _record_workflow_experience_observation(
        self,
        run_id: str,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
        status: str,
        integration_result: dict[str, Any] | None,
        retry_events: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        pattern_ids: list[str] = []
        for record in records.values():
            for pattern_id in record.experience_pattern_ids:
                if pattern_id not in pattern_ids:
                    pattern_ids.append(pattern_id)
        dynamic_event_names = {
            "DirectorGraphPatchProposed",
            "DirectorGraphPatchSessionCompleted",
            "GraphPatchApplied",
            "GraphPatchRolledBack",
            "GraphPatchRejected",
            "GraphPatchNoop",
            "NodeRetryScheduled",
            "NodeRetryNotScheduled",
            "PhaseEBranchCandidateCreated",
            "PhaseEBranchGateReviewed",
            "PhaseEIntegrationGateCompleted",
            "OverlookerForkDecision",
            "OverlookerForkPointSelected",
        }
        replan_event_names = {
            "DirectorGraphPatchProposed",
            "DirectorGraphPatchSessionCompleted",
            "GraphPatchApplied",
            "GraphPatchRolledBack",
            "GraphPatchRejected",
            "GraphPatchNoop",
        }
        dynamic_events = [
            {
                "event_type": event.get("event_type"),
                "node_id": event.get("node_id"),
                "payload": event.get("payload"),
            }
            for event in events
            if event.get("event_type") in dynamic_event_names
        ]
        replan_patch_ids: set[str] = set()
        replan_events_without_patch = 0
        for event in dynamic_events:
            if event.get("event_type") not in replan_event_names:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
            compiled = (
                payload.get("compiled")
                if isinstance(payload.get("compiled"), dict)
                else {}
            )
            patch_id = payload.get("patch_id") or patch.get("patch_id") or compiled.get("patch_id")
            if patch_id:
                replan_patch_ids.add(str(patch_id))
            else:
                replan_events_without_patch += 1
        replan_count = len(replan_patch_ids) + replan_events_without_patch
        branch_candidate_count = sum(
            1 for record in records.values() if record.branch_candidate_ref
        )
        evidence_refs: list[str] = []
        for record in records.values():
            for ref in [
                record.evidence_ref,
                record.overlooker_report_ref,
                record.second_overlooker_report_ref,
                record.branch_candidate_ref,
                record.integration_report_ref,
            ]:
                if ref and ref not in evidence_refs:
                    evidence_refs.append(ref)
        outcome = self._workflow_experience_outcome(
            status,
            retry_events,
            replan_count,
        )
        observation = WorkflowExperienceObservation(
            observation_id=f"workflow-exp-observation-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            graph_id=spec.graph_id,
            phase=spec.phase,
            outcome=outcome,
            pattern_ids_used=pattern_ids,
            node_outcomes={
                node_id: record.status for node_id, record in records.items()
            },
            dynamic_workflow_events=dynamic_events,
            integration_summary=(integration_result or {}),
            retry_count=len(retry_events),
            replan_count=replan_count,
            branch_candidate_count=branch_candidate_count,
            recommended_update=(
                "revise"
                if status == "accepted" and (replan_count or retry_events)
                else ("promote" if status == "accepted" else "demote")
            ),
            scaling_observations=self._scaling_observations(
                spec,
                records,
                status,
                replan_count,
                len(retry_events),
            ),
            reflection_attribution=self._reflection_attribution(
                spec,
                records,
                status,
                replan_count,
                len(retry_events),
                dynamic_events,
                integration_result or {},
            ),
            evidence_refs=evidence_refs,
        )
        self.experience_library.record_workflow_observation(observation)
        proposals = self.experience_library.update_from_workflow_observation(
            observation,
            proposed_by="workflow-runtime",
        )
        event = {
            "observation": to_plain_dict(observation),
            "update_proposals": [to_plain_dict(proposal) for proposal in proposals],
        }
        self._record(run_id, spec.graph_id, "WorkflowExperienceObservationRecorded", event)
        return event

    def _scaling_observations(
        self,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
        status: str,
        replan_count: int,
        retry_count: int,
    ) -> dict[str, Any]:
        pattern_ids: set[str] = set()
        for node in spec.nodes:
            pattern_ids.update(node.experience_pattern_ids)
        for record in records.values():
            pattern_ids.update(record.experience_pattern_ids)

        scaling_enabled = (
            bool(spec.scaling_policy)
            or "seed-scaling-adaptive-population-curriculum" in pattern_ids
        )
        if not scaling_enabled:
            return {}

        node_count = len(spec.nodes)
        accepted_count = sum(
            1 for record in records.values() if record.status == "NODE_ACCEPTED"
        )
        rejected_count = sum(
            1 for record in records.values() if record.status == "NODE_REJECTED"
        )
        branch_candidate_count = sum(
            1 for record in records.values() if record.branch_candidate_ref
        )
        validator_pass_rate = accepted_count / node_count if node_count else 0.0
        current_level = self._infer_scale_level(spec, node_count)
        ranking_entropy = (
            spec.scaling_policy.get("ranking_entropy")
            if isinstance(spec.scaling_policy, dict)
            else None
        )
        if ranking_entropy is None and node_count:
            unresolved = node_count - accepted_count - rejected_count
            ranking_entropy = round((rejected_count + unresolved) / node_count, 4)

        next_hint = "hold"
        trigger_reasons: list[str] = []
        if status == "accepted" and retry_count == 0 and replan_count == 0:
            next_hint = "promote_current_scale_level"
            trigger_reasons.append("accepted_without_dynamic_correction")
        elif accepted_count > 0 and (rejected_count or retry_count or replan_count):
            next_hint = "scale_up_mutation_or_comparison_density"
            trigger_reasons.append("partial_competence_detected")
        elif accepted_count == 0 and rejected_count == node_count and node_count > 0:
            next_hint = "route_to_research_or_specialist_before_more_population"
            trigger_reasons.append("no_candidate_near_correct")
        elif retry_count or replan_count:
            next_hint = "revise_scaling_policy"
            trigger_reasons.append("dynamic_correction_required")

        if branch_candidate_count:
            trigger_reasons.append("branch_candidates_created")
        if spec.scaling_policy.get("requested_scale_level") is not None:
            trigger_reasons.append("director_declared_requested_scale_level")

        return {
            "policy_id": spec.scaling_policy.get(
                "policy_id",
                "seed-scaling-adaptive-population-curriculum",
            ),
            "current_scale_level": current_level,
            "requested_scale_level": spec.scaling_policy.get("requested_scale_level"),
            "planned_agent_count": node_count,
            "candidate_count": spec.scaling_policy.get("candidate_count", node_count),
            "comparison_count": spec.scaling_policy.get("comparison_count", 0),
            "mutation_rounds": spec.scaling_policy.get("mutation_rounds", 0),
            "ranking_entropy": ranking_entropy,
            "validator_pass_rate": round(validator_pass_rate, 4),
            "accepted_node_count": accepted_count,
            "rejected_node_count": rejected_count,
            "retry_count": retry_count,
            "replan_count": replan_count,
            "branch_candidate_count": branch_candidate_count,
            "next_scaling_hint": next_hint,
            "trigger_reasons": trigger_reasons or ["no_scaling_change_signal"],
        }

    def _reflection_attribution(
        self,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
        status: str,
        replan_count: int,
        retry_count: int,
        dynamic_events: list[dict[str, Any]],
        integration_summary: dict[str, Any],
    ) -> dict[str, Any]:
        categories: list[str] = []
        signals: list[str] = []

        denied_or_blocked = [
            record
            for record in records.values()
            if record.status in {"NODE_BLOCKED", "NODE_ABORTED"}
            or record.failure_code in {"permission_denied", "network_not_grounded"}
        ]
        rejected = [
            record for record in records.values() if record.status == "NODE_REJECTED"
        ]
        verifier_findings = [
            record
            for record in records.values()
            if record.overlooker_verdict == "fail"
            or record.overlooker_recommended_action in {"retry", "request_director_replan"}
        ]

        if denied_or_blocked:
            categories.append("permission")
            signals.append("node blocked or aborted by permission/runtime guard")
        if replan_count:
            categories.append("workflow")
            signals.append("dynamic graph replan was required")
        if retry_count:
            categories.append("assignment")
            signals.append("retry path was needed for at least one node")
        if verifier_findings:
            categories.append("verification")
            signals.append("Overlooker or verifier rejected evidence")
        if integration_summary.get("conflict_count") or integration_summary.get("conflicts"):
            categories.append("assignment")
            signals.append("branch integration reported conflicts")
        if spec.scaling_policy:
            scaling_hint = spec.scaling_policy.get("next_scaling_hint")
            requested_level = spec.scaling_policy.get("requested_scale_level")
            if requested_level is not None or scaling_hint:
                categories.append("scaling")
                signals.append("Director declared scaling policy observations")
        if status != "accepted" and not categories:
            categories.append("task_profile")
            signals.append("failure had no grounded permission, verifier, retry, or replan signal")
        if status == "accepted" and not categories:
            categories.append("none")
            signals.append("workflow accepted without correction signal")

        unique_categories = []
        for category in categories:
            if category not in unique_categories:
                unique_categories.append(category)

        primary = unique_categories[0] if unique_categories else "none"
        recommended_learning = {
            "permission": "learn a narrower permission intent or add an explicit permission review gate",
            "workflow": "revise workflow structure and record the successful replan path",
            "assignment": "revise ownership, handoff, or failure takeover policy",
            "verification": "revise verifier evidence contract or Overlooker acceptance criteria",
            "scaling": "adjust scale level using observed candidate and validator signals",
            "task_profile": "improve pre-run task profiling and failure-mode prediction",
            "none": "promote the current planning pattern without structural revision",
        }.get(primary, "review attribution before updating experience patterns")

        return {
            "primary_category": primary,
            "categories": unique_categories,
            "signals": signals or ["no attribution signal"],
            "recommended_learning": recommended_learning,
            "dataset_quality_note": (
                "mark ambiguous_task when verifier evidence indicates objective ambiguity"
            ),
            "cost_acceptability": (
                "acceptable" if status == "accepted" and not retry_count and not replan_count else "review"
            ),
        }

    def _infer_scale_level(self, spec: GraphRunSpec, node_count: int) -> int:
        explicit = spec.scaling_policy.get("current_scale_level")
        if isinstance(explicit, int):
            return explicit
        if node_count <= 1:
            return 0
        if node_count <= 3:
            return 1
        if node_count <= 8:
            return 2
        if spec.scaling_policy.get("mutation_rounds") or spec.scaling_policy.get("evolution_loop"):
            return 3
        return 4

    def _workflow_experience_outcome(
        self,
        status: str,
        retry_events: list[dict[str, Any]],
        replan_count: int,
    ) -> str:
        if status == "accepted" and replan_count:
            return "replanned"
        if status == "accepted" and retry_events:
            return "retried"
        if status == "accepted":
            return "accepted"
        if status in {"aborted", "blocked"}:
            return "aborted"
        return "rejected"

    def _record_branch_candidate(
        self,
        run_id: str,
        graph_id: str,
        record: GraphNodeRecord,
        result: NodeExecutionResult,
    ) -> None:
        branch_name = f"{run_id}/{record.node_id}/attempt-{result.attempt}"
        candidate = {
            "schema": "egtc.phase_e.branch_candidate.v1",
            "run_id": run_id,
            "graph_id": graph_id,
            "node_id": record.node_id,
            "attempt": result.attempt,
            "branch_name": branch_name,
            "branch_workspace": result.workspace,
            "worker_id": result.worker_result.worker_id,
            "evidence_ref": result.evidence.evidence_ref.uri,
            "overlooker_report_ref": (
                result.overlooker_report.report_ref.uri
                if result.overlooker_report.report_ref
                else None
            ),
            "overlooker_recommended_action": result.overlooker_report.recommended_action,
            "validator_reports": to_plain_dict(result.validator_reports),
            "workspace_diff": result.workspace_diff,
        }
        candidate_ref = self.artifacts.put_json(
            candidate,
            {
                "kind": "phase_e_branch_candidate",
                "graph_id": graph_id,
                "node_id": record.node_id,
                "branch_name": branch_name,
            },
            self.runtime_actor,
            self.runtime_token,
        )
        record.branch_name = branch_name
        record.branch_workspace = result.workspace
        record.branch_candidate = candidate
        record.branch_candidate_ref = candidate_ref.uri
        self._record(
            run_id,
            record.node_id,
            "PhaseEBranchCandidateCreated",
            {
                "branch_candidate": candidate,
                "branch_candidate_ref": to_plain_dict(candidate_ref),
            },
        )

    def _run_phase_e_integration_gate(
        self,
        run_id: str,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
    ) -> dict[str, Any]:
        packet = {
            "schema": "egtc.phase_e.integration_packet.v1",
            "run_id": run_id,
            "graph_id": spec.graph_id,
            "policy": {
                "director_realtime_arbitration": False,
                "integration_owner": "overlooker",
                "permission_escalation_owner": "overlooker",
                "human_review_owner": "overlooker",
                "required_branch_state": PHASE_E_BRANCH_READY,
            },
            "branch_candidates": [
                {
                    "node_id": record.node_id,
                    "branch_name": record.branch_name,
                    "branch_workspace": record.branch_workspace,
                    "branch_candidate_ref": record.branch_candidate_ref,
                    "overlooker_report_ref": record.overlooker_report_ref,
                    "second_overlooker_report_ref": record.second_overlooker_report_ref,
                    "overlooker_recommended_action": record.overlooker_recommended_action,
                    "high_risk": record.high_risk,
                    "status": record.status,
                }
                for record in records.values()
            ],
        }
        if spec.integration_overlooker_mode in {"codex", "codex_phase_e"}:
            report = self._codex_phase_e_integration_review(run_id, spec, packet)
        elif spec.integration_overlooker_mode in {"model_agent", "model_phase_e"}:
            report = self._model_phase_e_integration_review(run_id, spec, packet)
        else:
            report = self._deterministic_phase_e_integration_review(spec, records, packet)
        report_ref = self.artifacts.put_json(
            report,
            {
                "kind": "phase_e_integration_overlooker_report",
                "graph_id": spec.graph_id,
                "run_id": run_id,
            },
            self.runtime_actor,
            self.runtime_token,
        )
        accepted = (
            report.get("verdict") == "pass"
            and report.get("recommended_action") == "advance"
            and not report.get("human_review_required")
            and not report.get("permission_escalation_required")
        )
        for record in records.values():
            record.integration_report_ref = report_ref.uri
            record.integration_decision = report
            record.human_review_required = bool(report.get("human_review_required"))
            record.permission_escalation_required = bool(
                report.get("permission_escalation_required")
            )
            if record.status == PHASE_E_BRANCH_READY:
                record.status = "NODE_ACCEPTED" if accepted else "NODE_BLOCKED"
                record.final_state = (
                    NodeState.NODE_ACCEPTED.value
                    if accepted
                    else NodeState.NODE_REJECTED.value
                )
                if not accepted:
                    record.failure_code = str(
                        report.get("failure_type") or "phase_e_integration_blocked"
                    )
        event = {
            "report": report,
            "report_ref": to_plain_dict(report_ref),
            "accepted": accepted,
        }
        self._record(run_id, spec.graph_id, "PhaseEIntegrationGateCompleted", event)
        return event

    def _deterministic_phase_e_integration_review(
        self,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        branch_records = [
            record for record in records.values() if record.status == PHASE_E_BRANCH_READY
        ]
        missing = [
            record.node_id
            for record in records.values()
            if record.status != PHASE_E_BRANCH_READY
        ]
        permission_nodes = [
            record.node_id
            for record in branch_records
            if record.overlooker_recommended_action == "request_permission_review"
        ]
        human_nodes = [
            record.node_id
            for record in branch_records
            if record.overlooker_recommended_action == "require_human_review"
        ]
        second_needed = [
            record.node_id
            for record in branch_records
            if record.high_risk and not record.second_overlooker_report_ref
        ]
        if permission_nodes:
            verdict = "blocked"
            action = "request_permission_review"
            failure_type = "permission_review_required"
        elif human_nodes:
            verdict = "blocked"
            action = "require_human_review"
            failure_type = "human_review_required"
        elif second_needed:
            verdict = "blocked"
            action = "require_second_overlooker"
            failure_type = "second_overlooker_required"
        elif missing:
            verdict = "blocked"
            action = "require_human_review"
            failure_type = "missing_branch_candidate"
        else:
            verdict = "pass"
            action = "advance"
            failure_type = None
        return {
            "schema": "egtc.phase_e.integration_overlooker_report.v1",
            "overlooker_id": f"integration-overlooker-{uuid.uuid4().hex[:12]}",
            "verdict": verdict,
            "recommended_action": action,
            "failure_type": failure_type,
            "rationale": (
                "All serial branch candidates completed and can be integrated."
                if verdict == "pass"
                else "Phase E integration is blocked by overlooker-owned review requirements."
            ),
            "branch_candidate_refs": [
                record.branch_candidate_ref
                for record in branch_records
                if record.branch_candidate_ref
            ],
            "permission_escalation_required": bool(permission_nodes),
            "human_review_required": bool(human_nodes or missing),
            "second_overlooker_required_for": second_needed,
            "permission_review_required_for": permission_nodes,
            "human_review_required_for": human_nodes,
            "packet": packet,
        }

    def _codex_phase_e_integration_review(
        self,
        run_id: str,
        spec: GraphRunSpec,
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        workspace = self.root / "runs" / run_id / "phase-e-integration-overlooker"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "integration_packet.json").write_text(
            json.dumps(packet, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        node = NodeCapsule(
            node_id=f"{spec.graph_id}-phase-e-integration-overlooker",
            phase="Phase E Integration Overlooker",
            goal="Review all branch candidates after serial nodes complete and decide integration, permission review, or human review.",
            command=[],
            acceptance_criteria=[
                "Integration Overlooker must review every branch_candidate_ref.",
                "Permission escalation and human review requests belong to the Overlooker.",
                "Director must not be used for realtime arbitration.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="codex_cli",
            prompt=self._phase_e_integration_prompt(),
            sandbox_profile={
                "backend": "codex_native",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": 240,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 48,
                    "max_command_count": 1,
                },
            },
        )
        result = self.wrapper.run(node, workspace, role="overlooker", run_id=run_id)
        report = self._read_integration_report(
            workspace / "integration_overlooker_report.json"
        )
        report.setdefault("overlooker_id", result.worker_id)
        report.setdefault("codex_exit_code", result.exit_code)
        report.setdefault(
            "codex_event_refs", [to_plain_dict(ref) for ref in result.event_refs]
        )
        if result.exit_code != 0 and report.get("verdict") == "pass":
            report["verdict"] = "blocked"
            report["recommended_action"] = "require_human_review"
            report["human_review_required"] = True
            report["failure_type"] = "integration_overlooker_failed"
        return report

    def _model_phase_e_integration_review(
        self,
        run_id: str,
        spec: GraphRunSpec,
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        workspace = self.root / "runs" / run_id / "phase-e-integration-overlooker"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "integration_packet.json").write_text(
            json.dumps(packet, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        node = NodeCapsule(
            node_id=f"{spec.graph_id}-phase-e-integration-overlooker",
            phase="Phase E Integration Overlooker",
            goal="Review all branch candidates after serial nodes complete and decide integration, permission review, or human review.",
            command=[],
            acceptance_criteria=[
                "Integration Overlooker must review every branch_candidate_ref.",
                "Permission escalation and human review requests belong to the Overlooker.",
                "Director must not be used for realtime arbitration.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="model_agent",
            prompt=self._phase_e_integration_prompt(),
            model_provider="deterministic",
            model_config={
                "input_files": ["integration_packet.json"],
                "output_file": "integration_overlooker_report.json",
                "output_json": True,
            },
            sandbox_profile={
                "backend": "model_agent",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": 240,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 1,
                    "max_command_count": 0,
                },
            },
        )
        result = self.wrapper.run(node, workspace, role="overlooker", run_id=run_id)
        report = self._read_integration_report(
            workspace / "integration_overlooker_report.json"
        )
        report.setdefault("overlooker_id", result.worker_id)
        report.setdefault("model_agent_exit_code", result.exit_code)
        report.setdefault(
            "model_agent_event_refs", [to_plain_dict(ref) for ref in result.event_refs]
        )
        if result.exit_code != 0 and report.get("verdict") == "pass":
            report["verdict"] = "blocked"
            report["recommended_action"] = "require_human_review"
            report["human_review_required"] = True
            report["failure_type"] = "integration_overlooker_failed"
        return report

    def _phase_e_integration_prompt(self) -> str:
        return """
You are the EGTC-PAW Phase E Integration Overlooker.

Read ./integration_packet.json and create ./integration_overlooker_report.json.

Output strict JSON:
{
  "verdict": "pass" | "blocked" | "uncertain",
  "recommended_action": "advance" | "request_permission_review" | "require_human_review" | "require_second_overlooker",
  "failure_type": null | "permission_review_required" | "human_review_required" | "second_overlooker_required" | "missing_branch_candidate" | "integration_overlooker_failed",
  "rationale": "short explanation",
  "branch_candidate_refs": ["artifact://..."],
  "permission_escalation_required": true | false,
  "human_review_required": true | false,
  "permission_review_required_for": ["node-id"],
  "human_review_required_for": ["node-id"],
  "second_overlooker_required_for": ["node-id"]
}

Rules:
- Do not ask Director to arbitrate realtime conflicts.
- Every serial node should already have a branch candidate.
- You own permission escalation and human review decisions.
- Pass only if every branch candidate is present and no permission/human/second-overlooker requirement remains.
- Do not clone repositories. Do not use network.
""".strip()

    def _read_integration_report(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {
                "verdict": "blocked",
                "recommended_action": "require_human_review",
                "failure_type": "integration_overlooker_failed",
                "rationale": "Integration Overlooker did not create integration_overlooker_report.json.",
                "branch_candidate_refs": [],
                "permission_escalation_required": False,
                "human_review_required": True,
                "permission_review_required_for": [],
                "human_review_required_for": [],
                "second_overlooker_required_for": [],
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "verdict": "blocked",
                "recommended_action": "require_human_review",
                "failure_type": "integration_overlooker_failed",
                "rationale": f"Integration Overlooker report was not valid JSON: {exc}",
                "branch_candidate_refs": [],
                "permission_escalation_required": False,
                "human_review_required": True,
                "permission_review_required_for": [],
                "human_review_required_for": [],
                "second_overlooker_required_for": [],
            }
        return data if isinstance(data, dict) else {
            "verdict": "blocked",
            "recommended_action": "require_human_review",
            "failure_type": "integration_overlooker_failed",
            "rationale": "Integration Overlooker report was not a JSON object.",
            "branch_candidate_refs": [],
            "permission_escalation_required": False,
            "human_review_required": True,
            "permission_review_required_for": [],
            "human_review_required_for": [],
            "second_overlooker_required_for": [],
        }

    def _select_fork_plan(
        self,
        run_id: str,
        node: NodeCapsule,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
        overlooker_mode: str,
    ) -> WorkspaceForkPlan:
        candidates = [
            parent_id
            for parent_id in record.depends_on
            if records[parent_id].status in {"NODE_ACCEPTED", PHASE_E_BRANCH_READY}
            and records[parent_id].accepted_workspace
        ]
        source_node_id: str | None = None
        source_workspace: Path | None = None
        if candidates:
            source_node_id = self._choose_fork_source(
                run_id,
                node,
                record,
                records,
                sorted(candidates),
                overlooker_mode,
            )
            source_workspace = Path(records[source_node_id].accepted_workspace or "")
            reason = (
                "retry_from_accepted_dependency"
                if record.attempts > 1
                else "initial_from_accepted_dependency"
            )
        elif node.workspace:
            source_workspace = Path(node.workspace)
            reason = (
                "retry_from_initial_workspace"
                if record.attempts > 1
                else "initial_from_node_workspace"
            )
        else:
            reason = "retry_from_empty_workspace" if record.attempts > 1 else "initial_empty_workspace"

        if source_workspace is not None and not source_workspace.exists():
            source_workspace = None
            source_node_id = None
            reason = "source_workspace_missing_empty_fallback"

        workspace = self._attempt_workspace(run_id, node.node_id, record.attempts)
        event = {
            "node_id": node.node_id,
            "attempt": record.attempts,
            "selected_by": f"{overlooker_mode}_overlooker_policy",
            "source_node_id": source_node_id,
            "source_workspace": str(source_workspace) if source_workspace else None,
            "target_workspace": str(workspace),
            "candidate_node_ids": sorted(candidates),
            "previous_failure_code": record.failure_code,
            "reason": reason,
        }
        record.current_workspace = str(workspace)
        record.fork_source_node_id = source_node_id
        record.fork_source_workspace = str(source_workspace) if source_workspace else None
        record.fork_history.append(event)
        self._record(run_id, node.node_id, "OverlookerForkPointSelected", event)
        return WorkspaceForkPlan(
            attempt=record.attempts,
            workspace=workspace,
            source_node_id=source_node_id,
            source_workspace=source_workspace,
            candidate_node_ids=sorted(candidates),
            reason=reason,
        )

    def _choose_fork_source(
        self,
        run_id: str,
        node: NodeCapsule,
        record: GraphNodeRecord,
        records: dict[str, GraphNodeRecord],
        candidates: list[str],
        overlooker_mode: str,
    ) -> str:
        default_choice = candidates[-1]
        if overlooker_mode not in {"codex", "model_agent"} or record.attempts <= 1:
            return default_choice
        advisor_workspace = (
            self.root
            / "runs"
            / run_id
            / "nodes"
            / node.node_id
            / f"attempt-{record.attempts}"
            / "fork-overlooker"
        )
        advisor_workspace.mkdir(parents=True, exist_ok=True)
        input_packet = {
            "node_id": node.node_id,
            "attempt": record.attempts,
            "previous_failure_code": record.failure_code,
            "candidate_nodes": [
                {
                    "node_id": candidate,
                    "status": records[candidate].status,
                    "accepted_workspace": records[candidate].accepted_workspace,
                    "failure_code": records[candidate].failure_code,
                }
                for candidate in candidates
            ],
            "policy": {
                "allowed_selection": "Choose one candidate with status NODE_ACCEPTED and a non-empty accepted_workspace.",
                "goal": "Fork the retry from a known-good upstream workspace, not from the failed attempt workspace.",
            },
        }
        (advisor_workspace / "fork_advisor_input.json").write_text(
            json.dumps(input_packet, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        advisor_node = NodeCapsule(
            node_id=f"{node.node_id}-fork-overlooker",
            phase="Phase D Fork Overlooker",
            goal="Select the accepted upstream workspace to fork for retry.",
            command=[],
            acceptance_criteria=[
                "Fork advisor must choose an accepted candidate only.",
                "Fork advisor must write strict JSON.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="model_agent" if overlooker_mode == "model_agent" else "codex_cli",
            prompt=self._fork_advisor_prompt(),
            model_provider="deterministic" if overlooker_mode == "model_agent" else None,
            model_config=(
                {
                    "input_files": ["fork_advisor_input.json"],
                    "output_file": "fork_decision.json",
                    "output_json": True,
                }
                if overlooker_mode == "model_agent"
                else {}
            ),
            sandbox_profile={
                "backend": "model_agent" if overlooker_mode == "model_agent" else "codex_native",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": 120,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 48,
                    "max_command_count": 1,
                },
            },
        )
        advisor_result = self.wrapper.run(
            advisor_node,
            advisor_workspace,
            role="overlooker",
            run_id=run_id,
        )
        decision = self._read_fork_decision(advisor_workspace / "fork_decision.json")
        selected = str(decision.get("selected_node_id") or default_choice)
        if selected not in candidates:
            selected = default_choice
        if records[selected].status != "NODE_ACCEPTED" or not records[selected].accepted_workspace:
            selected = default_choice
        event = {
            "node_id": node.node_id,
            "attempt": record.attempts,
            "overlooker_id": advisor_result.worker_id,
            "overlooker_exit_code": advisor_result.exit_code,
            "candidate_node_ids": candidates,
            "selected_node_id": selected,
            "decision": decision,
            "workspace": str(advisor_workspace),
        }
        record.fork_advisor_history.append(event)
        self._record(run_id, node.node_id, "OverlookerForkDecision", event)
        return selected

    def _fork_advisor_prompt(self) -> str:
        return """
You are the Phase D Codex overlooker for retry fork selection.

Read ./fork_advisor_input.json and create ./fork_decision.json.

Output strict JSON:
{
  "selected_node_id": "...",
  "rationale": "short reason",
  "confidence": "low" | "medium" | "high"
}

Rules:
- Select only one candidate whose status is NODE_ACCEPTED and accepted_workspace is present.
- Prefer a direct upstream accepted workspace over any failed attempt workspace.
- Do not clone repositories.
- Do not use network.
- Do not write outside this workspace.
""".strip()

    def _read_fork_decision(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _validate_spec(self, spec: GraphRunSpec) -> None:
        if spec.max_parallelism < 1:
            raise ValueError("max_parallelism must be >= 1")
        if spec.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        node_ids = [node.node_id for node in spec.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Graph node ids must be unique")
        known = set(node_ids)
        for source, target in spec.edges:
            if source not in known or target not in known:
                raise ValueError(f"Unknown graph edge: {source!r} -> {target!r}")
        self._topological_order(spec)

    def _topological_order(self, spec: GraphRunSpec) -> list[str]:
        dependencies: dict[str, set[str]] = {node.node_id: set() for node in spec.nodes}
        dependents: dict[str, set[str]] = {node.node_id: set() for node in spec.nodes}
        for source, target in spec.edges:
            dependencies[target].add(source)
            dependents[source].add(target)
        ready = sorted(node_id for node_id, deps in dependencies.items() if not deps)
        ordered: list[str] = []
        while ready:
            node_id = ready.pop(0)
            ordered.append(node_id)
            for child in sorted(dependents[node_id]):
                dependencies[child].remove(node_id)
                if not dependencies[child]:
                    ready.append(child)
                    ready.sort()
        if len(ordered) != len(spec.nodes):
            raise ValueError("Workflow graph must be a DAG")
        return ordered

    def _next_runnable(
        self,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
    ) -> str | None:
        for node_id in sorted(records):
            record = records[node_id]
            if record.status != "NODE_PLANNED":
                continue
            if all(
                self._dependency_satisfied(spec, records[parent].status)
                for parent in record.depends_on
            ):
                return node_id
        return None

    def _dependency_satisfied(self, spec: GraphRunSpec, status: str) -> bool:
        if status == "NODE_ACCEPTED":
            return True
        return self._phase_e_enabled(spec.phase, spec.overlooker_mode) and status == PHASE_E_BRANCH_READY

    def _can_start(
        self,
        record: GraphNodeRecord,
        active: dict[Future[NodeExecutionResult], str],
        active_writer: str | None,
    ) -> bool:
        if record.read_only:
            return active_writer is None
        return not active and active_writer is None

    def _mark_blocked(self, run_id: str, records: dict[str, GraphNodeRecord]) -> None:
        for record in records.values():
            if record.status == "NODE_PLANNED":
                record.status = "NODE_BLOCKED"
                record.failure_code = "dependency_or_scheduler_deadlock"
                self._record(
                    run_id,
                    record.node_id,
                    "DeadlockDetected",
                    {
                        "depends_on": record.depends_on,
                        "dependency_statuses": {
                            parent: records[parent].status for parent in record.depends_on
                        },
                    },
                )

    def _all_terminal(self, records: dict[str, GraphNodeRecord]) -> bool:
        return all(
            record.status in TERMINAL_STATUSES or record.status == PHASE_E_BRANCH_READY
            for record in records.values()
        )

    def _ready_for_phase_e_integration(self, records: dict[str, GraphNodeRecord]) -> bool:
        return bool(records) and all(
            record.status == PHASE_E_BRANCH_READY for record in records.values()
        )

    def _graph_status(self, records: dict[str, GraphNodeRecord]) -> str:
        if all(record.status == "NODE_ACCEPTED" for record in records.values()):
            return "accepted"
        if all(record.status == PHASE_E_BRANCH_READY for record in records.values()):
            return "integration_pending"
        if any(record.status == "NODE_ABORTED" for record in records.values()):
            return "aborted"
        if any(record.status == "NODE_BLOCKED" for record in records.values()):
            return "blocked"
        if any(record.status == "NODE_REJECTED" for record in records.values()):
            return "rejected"
        return "running"

    def _failure_code(
        self,
        worker_result: WorkerResult,
        validator_reports: list[ValidatorReport],
    ) -> str:
        if worker_result.exit_code != 0:
            return f"worker_exit_{worker_result.exit_code}"
        failed = [report.validator_id for report in validator_reports if not report.passed]
        return failed[0] if failed else "overlooker_rejected"

    def _is_read_only(self, node: NodeCapsule) -> bool:
        return not self._write_paths(node)

    def _write_paths(self, node: NodeCapsule) -> list[str]:
        profile = node.sandbox_profile or {}
        raw = profile.get("allowed_write_paths")
        if isinstance(raw, list):
            return sorted(str(path) for path in raw if str(path))
        if node.phase.lower() == "implementation":
            return ["."]
        return []

    def _attempt_workspace(self, run_id: str, node_id: str, attempt: int) -> Path:
        return self.root / "runs" / run_id / "nodes" / node_id / f"attempt-{attempt}" / "workspace"

    def _prepare_workspace(self, node: NodeCapsule, fork_plan: WorkspaceForkPlan) -> Path:
        workspace = fork_plan.workspace
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        source = fork_plan.source_workspace
        if source is None and node.workspace:
            source = Path(node.workspace)
        if source and source.exists():
            shutil.copytree(source, workspace, dirs_exist_ok=True)
        (workspace / ".egtc_attempt.json").write_text(
            json.dumps(
                {
                    "attempt": fork_plan.attempt,
                    "source_node_id": fork_plan.source_node_id,
                    "source_workspace": str(fork_plan.source_workspace)
                    if fork_plan.source_workspace
                    else None,
                    "candidate_node_ids": fork_plan.candidate_node_ids,
                    "fork_reason": fork_plan.reason,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return workspace

    def _checkpoint_path(self, run_id: str) -> Path:
        return self.root / "checkpoints" / f"{run_id}.json"

    def _write_checkpoint(
        self,
        run_id: str,
        spec: GraphRunSpec,
        records: dict[str, GraphNodeRecord],
        status: str,
    ) -> None:
        checkpoint = {
            "run_id": run_id,
            "status": status,
            "updated_at": time.time(),
            "spec": self._spec_to_plain(spec),
            "nodes": {node_id: asdict(record) for node_id, record in records.items()},
        }
        path = self._checkpoint_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(checkpoint, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _read_checkpoint(self, run_id: str) -> dict[str, Any]:
        path = self._checkpoint_path(run_id)
        if not path.exists():
            raise FileNotFoundError(f"No checkpoint for run_id {run_id}: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _spec_to_plain(self, spec: GraphRunSpec) -> dict[str, Any]:
        return {
            "graph_id": spec.graph_id,
            "nodes": [to_plain_dict(node) for node in spec.nodes],
            "edges": [list(edge) for edge in spec.edges],
            "max_parallelism": spec.max_parallelism,
            "max_attempts": spec.max_attempts,
            "retry_budget": spec.retry_budget,
            "max_same_failure_retries": spec.max_same_failure_retries,
            "overlooker_mode": spec.overlooker_mode,
            "director_mode": spec.director_mode,
            "replan_budget": spec.replan_budget,
            "phase": spec.phase,
            "second_overlooker_mode": spec.second_overlooker_mode,
            "integration_overlooker_mode": spec.integration_overlooker_mode,
            "scaling_policy": spec.scaling_policy,
        }

    def _spec_from_checkpoint(self, checkpoint: dict[str, Any]) -> GraphRunSpec:
        raw = checkpoint["spec"]
        return GraphRunSpec(
            graph_id=raw["graph_id"],
            nodes=[self._node_from_plain(node) for node in raw["nodes"]],
            edges=[tuple(edge) for edge in raw["edges"]],
            max_parallelism=int(raw["max_parallelism"]),
            max_attempts=int(raw["max_attempts"]),
            retry_budget=int(raw["retry_budget"]),
            max_same_failure_retries=int(raw["max_same_failure_retries"]),
            overlooker_mode=str(raw["overlooker_mode"]),
            director_mode=str(raw.get("director_mode", "deterministic")),
            replan_budget=int(raw.get("replan_budget", 0)),
            phase=str(raw.get("phase", "D")),
            second_overlooker_mode=str(raw.get("second_overlooker_mode", "deterministic")),
            integration_overlooker_mode=str(raw.get("integration_overlooker_mode", "deterministic")),
            scaling_policy=(
                raw.get("scaling_policy")
                if isinstance(raw.get("scaling_policy"), dict)
                else {}
            ),
        )

    def _node_from_plain(self, raw: dict[str, Any]) -> NodeCapsule:
        allowed = {item.name for item in fields(NodeCapsule)}
        return NodeCapsule(**{key: value for key, value in raw.items() if key in allowed})

    def _record(
        self,
        run_id: str,
        node_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.event_log.append(run_id, node_id, event_type, payload)
