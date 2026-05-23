from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import NodeCapsule, to_plain_dict


@dataclass
class RepoPolicy:
    repo_root: str
    package_managers: list[str]
    test_commands: list[list[str]]
    allowed_read_paths: list[str]
    allowed_write_paths: list[str]
    sensitive_paths: list[str]
    network_allowed_by_default: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class TaskDiagnosis:
    task_id: str
    objective: str
    task_kind: str
    risk_level: str
    repo_touchpoints: list[str]
    requires_code_change: bool
    requires_tests: bool
    task_profile: dict[str, Any] = field(default_factory=dict)
    unknowns: list[str] = field(default_factory=list)
    experience_matches: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WorkflowSkeletonNode:
    node_id: str
    phase: str
    role: str
    goal: str
    depends_on: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    experience_pattern_ids: list[str] = field(default_factory=list)
    node_selection_principles: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowSkeleton:
    skeleton_id: str
    topology: str
    nodes: list[WorkflowSkeletonNode]
    edges: list[tuple[str, str]]
    rationale: str
    agent_allocation: dict[str, Any] = field(default_factory=dict)
    alternative_skeletons: list[dict[str, Any]] = field(default_factory=list)
    scaling_policy: dict[str, Any] = field(default_factory=dict)
    execution_estimate: dict[str, Any] = field(default_factory=dict)
    deliberation_trace: list[str] = field(default_factory=list)
    linear_requirement_flow: list[dict[str, Any]] = field(default_factory=list)
    stage_structure_decisions: list[dict[str, Any]] = field(default_factory=list)
    research_route_decisions: list[dict[str, Any]] = field(default_factory=list)
    per_stage_agent_allocation: list[dict[str, Any]] = field(default_factory=list)
    plan_derivation_trace: list[str] = field(default_factory=list)
    draft_plan_review: dict[str, Any] = field(default_factory=dict)
    experience_pattern_ids: list[str] = field(default_factory=list)
    experience_rationale: list[str] = field(default_factory=list)


@dataclass
class SandboxProfile:
    network: str
    allowed_read_paths: list[str]
    allowed_write_paths: list[str]
    allowed_commands: list[list[str]]
    justification: str
    backend: str = "codex_native"
    sandbox_mode: str = "workspace_write"
    resource_limits: dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionGroundingReport:
    node_id: str
    sandbox_profile: SandboxProfile
    grounded_by: list[str]
    denied_requests: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class NodeInstantiation:
    node: NodeCapsule
    skeleton_node_id: str
    permission_grounding: PermissionGroundingReport
    instantiation_principles: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowBlueprint:
    blueprint_id: str
    director_id: str
    task_diagnosis: TaskDiagnosis
    repo_policy: RepoPolicy
    workflow_skeleton: WorkflowSkeleton
    node_instantiations: list[NodeInstantiation]
    task_profile: dict[str, Any] = field(default_factory=dict)
    work_assignment_plan: list[dict[str, Any]] = field(default_factory=list)
    permission_plan: list[dict[str, Any]] = field(default_factory=list)
    experience_pattern_ids: list[str] = field(default_factory=list)
    director_mode: str = "deterministic"
    director_session_id: str | None = None
    director_skill_usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompilerFinding:
    severity: str
    code: str
    message: str
    node_id: str | None = None


@dataclass
class CompiledWorkflow:
    accepted: bool
    blueprint_id: str
    executable_nodes: list[NodeCapsule]
    findings: list[CompilerFinding]


def structured(value: Any) -> dict[str, Any]:
    return to_plain_dict(value)
