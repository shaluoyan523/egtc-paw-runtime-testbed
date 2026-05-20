from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class NodeState(str, Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    WORKER_SUBMITTED = "WorkerSubmitted"
    NODE_ACCEPTED = "NodeAccepted"
    NODE_REJECTED = "NodeRejected"


@dataclass(frozen=True)
class ActorIdentity:
    actor_id: str
    actor_type: str


@dataclass(frozen=True)
class CapabilityToken:
    token_id: str
    actor_id: str
    scopes: list[str]
    expires_at: str
    signature: str


@dataclass(frozen=True)
class ArtifactRef:
    uri: str
    sha256: str
    size_bytes: int
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NodeCapsule:
    node_id: str
    phase: str
    goal: str
    command: list[str]
    acceptance_criteria: list[str]
    required_evidence: list[str] = field(default_factory=lambda: ["diff", "test", "log"])
    experience_pattern_ids: list[str] = field(default_factory=list)
    workspace: str | None = None
    executor_kind: str = "subprocess"
    prompt: str | None = None
    codex_binary: str | None = None
    codex_sandbox: str = "workspace-write"
    model_provider: str | None = None
    model: str | None = None
    model_config: dict[str, Any] = field(default_factory=dict)
    sandbox_profile: dict[str, Any] | None = None


@dataclass
class WorkerResult:
    worker_id: str
    status: str
    exit_code: int
    event_refs: list[ArtifactRef]
    stdout_ref: ArtifactRef
    stderr_ref: ArtifactRef
    parsed_events: list[dict[str, Any]]
    sandbox_event_refs: list[ArtifactRef] = field(default_factory=list)
    resource_report_ref: ArtifactRef | None = None


@dataclass
class EvidenceBundle:
    evidence_id: str
    node_id: str
    worker_id: str
    evidence_ref: ArtifactRef
    artifacts: dict[str, ArtifactRef]
    state: NodeState = NodeState.WORKER_SUBMITTED


@dataclass
class ValidatorReport:
    validator_id: str
    passed: bool
    findings: list[str]
    evidence_ref: str | None


@dataclass
class OverlookerReport:
    overlooker_id: str
    verdict: str
    rationale: str
    evidence_ref: str | None
    validator_refs: list[str]
    report_ref: ArtifactRef | None = None
    agent_event_refs: list[ArtifactRef] = field(default_factory=list)
    codex_event_refs: list[ArtifactRef] = field(default_factory=list)
    confidence: str = "medium"
    cited_evidence: list[str] = field(default_factory=list)
    failure_type: str | None = None
    recommended_action: str = "advance"
    release_overlooker: bool = False


@dataclass
class GraphPatchOperation:
    op: str
    node_id: str | None = None
    source_node_id: str | None = None
    target_node_id: str | None = None
    value: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class GraphPatch:
    patch_id: str
    director_id: str
    graph_id: str
    triggering_node_id: str
    triggering_event: str
    overlooker_report_ref: str | None
    operations: list[GraphPatchOperation]
    rationale: str
    replan_budget_cost: int = 1


@dataclass
class CompiledGraphPatch:
    accepted: bool
    patch_id: str
    graph_id: str
    operations: list[GraphPatchOperation]
    findings: list[dict[str, Any]]


@dataclass
class DecisionConflict:
    conflict_id: str
    run_id: str
    node_id: str
    conflict_type: str
    policy_findings: list[dict[str, Any]]
    validator_reports: list[ValidatorReport]
    overlooker_reports: list[OverlookerReport]
    director_intent: str
    risk_level: str = "normal"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConflictResolution:
    conflict_id: str
    node_id: str
    decision: str
    rationale: str
    priority: str
    required_action: str
    accepted_overlooker_id: str | None = None
    release_node: bool = False
    human_review_required: bool = False
    permission_escalation_required: bool = False


def to_plain_dict(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_plain_dict(val) for key, val in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_plain_dict(val) for key, val in value.items()}
    if isinstance(value, list):
        return [to_plain_dict(item) for item in value]
    return value
