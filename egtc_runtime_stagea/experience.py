from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import to_plain_dict


PATTERN_TYPES = {
    "agent_generation",
    "aggregation",
    "dynamic_topology",
    "governance",
    "topology",
    "memory_planning",
    "role_template",
    "handoff",
    "routing",
    "failure_policy",
    "review_loop",
    "scaling_policy",
    "security_policy",
    "tool_planning",
}

PATTERN_STATUSES = {"draft", "active", "deprecated", "rejected"}

OBSERVATION_OUTCOMES = {"accepted", "rejected", "retried", "replanned", "aborted"}

UPDATE_TYPES = {"promote", "demote", "revise", "deprecate", "add_new"}

REVIEW_STATUSES = {"proposed", "accepted", "rejected"}

EVIDENCE_WEIGHT = {
    "direct": 3,
    "strong": 3,
    "mixed": 2,
    "medium": 2,
    "inference": 1,
    "weak": 1,
}


@dataclass
class ExperiencePattern:
    pattern_id: str
    pattern_type: str
    description: str
    applicability_signals: list[str]
    anti_signals: list[str]
    recommended_structure: dict[str, Any]
    required_evidence: list[str]
    risk_notes: list[str]
    source_refs: list[str]
    evidence_level: str
    confidence_score: float
    success_count: int = 0
    failure_count: int = 0
    version: int = 1
    status: str = "active"
    tags: list[str] = field(default_factory=list)
    last_updated_at: float = field(default_factory=time.time)


@dataclass
class ExperienceObservation:
    observation_id: str
    run_id: str
    graph_id: str
    node_id: str
    pattern_ids_used: list[str]
    outcome: str
    validator_findings: list[dict[str, Any]]
    overlooker_verdict: str | None
    failure_type: str | None
    recommended_update: str
    evidence_refs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class WorkflowExperienceObservation:
    observation_id: str
    run_id: str
    graph_id: str
    phase: str
    outcome: str
    pattern_ids_used: list[str]
    node_outcomes: dict[str, str]
    dynamic_workflow_events: list[dict[str, Any]]
    integration_summary: dict[str, Any]
    retry_count: int
    replan_count: int
    branch_candidate_count: int
    recommended_update: str
    scaling_observations: dict[str, Any] = field(default_factory=dict)
    reflection_attribution: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class ExperienceUpdateProposal:
    proposal_id: str
    proposed_by: str
    target_pattern_id: str
    update_type: str
    evidence_refs: list[str]
    rationale: str
    review_status: str = "proposed"
    created_at: float = field(default_factory=time.time)


@dataclass
class ExperienceMatch:
    pattern: ExperiencePattern
    score: float
    matched_signals: list[str]


class ExperienceLibrary:
    """Versioned JSONL-backed experience library for Director planning."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.pattern_path = self.root / "patterns.jsonl"
        self.observation_path = self.root / "observations.jsonl"
        self.workflow_observation_path = self.root / "workflow_observations.jsonl"
        self.proposal_path = self.root / "update_proposals.jsonl"

    def seed_defaults(self) -> list[ExperiencePattern]:
        existing = {pattern.pattern_id for pattern in self.load_patterns(include_inactive=True)}
        seeded: list[ExperiencePattern] = []
        for pattern in default_seed_patterns():
            if pattern.pattern_id in existing:
                continue
            self.add_pattern(pattern)
            seeded.append(pattern)
        return seeded

    def add_pattern(self, pattern: ExperiencePattern) -> None:
        self._validate_pattern(pattern)
        self._append_jsonl(self.pattern_path, to_plain_dict(pattern))

    def load_patterns(self, include_inactive: bool = False) -> list[ExperiencePattern]:
        latest: dict[str, ExperiencePattern] = {}
        for raw in self._read_jsonl(self.pattern_path):
            pattern = ExperiencePattern(**raw)
            current = latest.get(pattern.pattern_id)
            if current is None or (
                pattern.version,
                pattern.last_updated_at,
            ) >= (
                current.version,
                current.last_updated_at,
            ):
                latest[pattern.pattern_id] = pattern
        patterns = sorted(latest.values(), key=lambda pattern: pattern.pattern_id)
        if include_inactive:
            return patterns
        return [pattern for pattern in patterns if pattern.status == "active"]

    def export_agent_catalog(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        """Return the active experience catalog in the same structure agents consume."""

        return [
            to_plain_dict(pattern)
            for pattern in self.load_patterns(include_inactive=include_inactive)
        ]

    def get_patterns(self, pattern_ids: list[str]) -> list[ExperiencePattern]:
        wanted = set(pattern_ids)
        return [pattern for pattern in self.load_patterns() if pattern.pattern_id in wanted]

    def retrieve(
        self,
        objective: str,
        *,
        limit: int = 5,
        pattern_types: set[str] | None = None,
    ) -> list[ExperienceMatch]:
        text = objective.lower()
        matches: list[ExperienceMatch] = []
        for pattern in self.load_patterns():
            if pattern_types and pattern.pattern_type not in pattern_types:
                continue
            anti_hits = [signal for signal in pattern.anti_signals if signal.lower() in text]
            if anti_hits:
                continue
            signal_hits = [
                signal for signal in pattern.applicability_signals if signal.lower() in text
            ]
            tag_hits = [tag for tag in pattern.tags if tag.lower() in text]
            inferred_hits = self._inferred_signal_hits(text, pattern)
            if not signal_hits and not tag_hits and not inferred_hits:
                continue
            evidence = EVIDENCE_WEIGHT.get(pattern.evidence_level.lower(), 1)
            outcome_score = pattern.success_count - pattern.failure_count
            score = (
                len(signal_hits) * 2
                + len(tag_hits)
                + len(inferred_hits) * 1.5
                + evidence
                + pattern.confidence_score
                + max(-3, min(3, outcome_score))
            )
            matches.append(
                ExperienceMatch(
                    pattern=pattern,
                    score=score,
                    matched_signals=signal_hits + tag_hits + inferred_hits,
                )
            )
        matches.sort(key=lambda match: (-match.score, match.pattern.pattern_id))
        return matches[:limit]

    def _inferred_signal_hits(
        self,
        text: str,
        pattern: ExperiencePattern,
    ) -> list[str]:
        if pattern.pattern_id != "seed-scaling-adaptive-population-curriculum":
            return []

        complexity_terms = [
            "complex",
            "difficult",
            "hard",
            "hardest",
            "benchmark",
            "最难",
            "复杂",
            "困难",
            "难题",
        ]
        verifiable_terms = [
            "accuracy",
            "pass rate",
            "validator",
            "benchmark",
            "test",
            "compile",
            "judge",
            "准确率",
            "通过率",
            "校验",
            "验证",
            "测试",
            "判题",
        ]
        scaling_terms = [
            "scaling",
            "scale",
            "population",
            "candidate",
            "parallel",
            "multiagent",
            "multi-agent",
            "token",
            "latency",
            "budget",
            "扩展",
            "扩大",
            "候选",
            "并行",
            "多agent",
            "多 agent",
            "预算",
            "耗时",
        ]
        learning_terms = [
            "learn",
            "learning",
            "experience",
            "improve",
            "performance",
            "paper",
            "自我学习",
            "经验",
            "提升",
            "性能",
            "论文",
        ]

        has_complexity = any(term in text for term in complexity_terms)
        has_verifiable = any(term in text for term in verifiable_terms)
        has_scaling = any(term in text for term in scaling_terms)
        has_learning = any(term in text for term in learning_terms)

        hits: list[str] = []
        if has_complexity and has_verifiable:
            hits.append("inferred:complex_verifiable_task")
        if has_complexity and has_scaling:
            hits.append("inferred:complex_task_with_scaling_pressure")
        if has_verifiable and has_learning:
            hits.append("inferred:measured_learning_loop")
        if has_scaling and has_learning:
            hits.append("inferred:self_learning_scaling_request")
        return hits

    def record_observation(self, observation: ExperienceObservation) -> None:
        self._validate_observation(observation)
        self._append_jsonl(self.observation_path, to_plain_dict(observation))

    def record_workflow_observation(
        self,
        observation: WorkflowExperienceObservation,
    ) -> None:
        self._validate_workflow_observation(observation)
        self._append_jsonl(self.workflow_observation_path, to_plain_dict(observation))

    def propose_update(self, proposal: ExperienceUpdateProposal) -> None:
        self._validate_proposal(proposal)
        self._append_jsonl(self.proposal_path, to_plain_dict(proposal))

    def load_observations(self) -> list[ExperienceObservation]:
        return [
            ExperienceObservation(**raw)
            for raw in self._read_jsonl(self.observation_path)
        ]

    def load_workflow_observations(self) -> list[WorkflowExperienceObservation]:
        return [
            WorkflowExperienceObservation(**raw)
            for raw in self._read_jsonl(self.workflow_observation_path)
        ]

    def load_update_proposals(self) -> list[ExperienceUpdateProposal]:
        return [
            ExperienceUpdateProposal(**raw)
            for raw in self._read_jsonl(self.proposal_path)
        ]

    def accept_update_proposal(
        self,
        proposal: ExperienceUpdateProposal,
        *,
        reviewer: str,
    ) -> ExperiencePattern | None:
        if proposal.review_status != "proposed":
            raise ValueError("Only proposed experience updates can be accepted")
        active = {pattern.pattern_id: pattern for pattern in self.load_patterns()}
        target = active.get(proposal.target_pattern_id)
        if target is None:
            if proposal.update_type != "add_new":
                raise ValueError(f"Unknown active pattern: {proposal.target_pattern_id}")
            return None

        confidence_delta = {
            "promote": 0.5,
            "demote": -1.0,
            "revise": -0.25,
            "deprecate": -2.0,
            "add_new": 0.0,
        }[proposal.update_type]
        status = "deprecated" if proposal.update_type == "deprecate" else target.status
        updated = ExperiencePattern(
            pattern_id=target.pattern_id,
            pattern_type=target.pattern_type,
            description=target.description,
            applicability_signals=target.applicability_signals,
            anti_signals=target.anti_signals,
            recommended_structure=target.recommended_structure,
            required_evidence=target.required_evidence,
            risk_notes=target.risk_notes
            + [
                f"Accepted {proposal.update_type} by {reviewer}: {proposal.rationale}",
            ],
            source_refs=target.source_refs + proposal.evidence_refs,
            evidence_level=target.evidence_level,
            confidence_score=max(
                0.0,
                min(10.0, target.confidence_score + confidence_delta),
            ),
            success_count=target.success_count
            + (1 if proposal.update_type == "promote" else 0),
            failure_count=target.failure_count
            + (1 if proposal.update_type in {"demote", "deprecate"} else 0),
            version=target.version + 1,
            status=status,
            tags=target.tags,
            last_updated_at=time.time(),
        )
        self.add_pattern(updated)
        reviewed = ExperienceUpdateProposal(
            proposal_id=proposal.proposal_id,
            proposed_by=proposal.proposed_by,
            target_pattern_id=proposal.target_pattern_id,
            update_type=proposal.update_type,
            evidence_refs=proposal.evidence_refs,
            rationale=f"{proposal.rationale} Reviewed by {reviewer}.",
            review_status="accepted",
            created_at=time.time(),
        )
        self.propose_update(reviewed)
        return updated

    def update_from_observation(
        self,
        observation: ExperienceObservation,
        *,
        proposed_by: str = "runtime",
    ) -> list[ExperienceUpdateProposal]:
        proposals: list[ExperienceUpdateProposal] = []
        if not observation.pattern_ids_used:
            return proposals
        if observation.outcome in {"accepted", "retried"}:
            update_type = "promote"
            rationale = "Pattern usage produced an accepted or recoverable node outcome."
        elif observation.outcome in {"rejected", "aborted"}:
            update_type = "demote"
            rationale = "Pattern usage contributed to a rejected or aborted node outcome."
        else:
            update_type = "revise"
            rationale = "Pattern usage required replanning and should be reviewed."
        if observation.failure_type:
            rationale += f" failure_type={observation.failure_type}."
        for pattern_id in observation.pattern_ids_used:
            proposal = ExperienceUpdateProposal(
                proposal_id=f"exp-proposal-{uuid.uuid4().hex[:12]}",
                proposed_by=proposed_by,
                target_pattern_id=pattern_id,
                update_type=update_type,
                evidence_refs=observation.evidence_refs,
                rationale=rationale,
            )
            self.propose_update(proposal)
            proposals.append(proposal)
        return proposals

    def update_from_workflow_observation(
        self,
        observation: WorkflowExperienceObservation,
        *,
        proposed_by: str = "workflow-runtime",
    ) -> list[ExperienceUpdateProposal]:
        proposals: list[ExperienceUpdateProposal] = []
        if not observation.pattern_ids_used:
            return proposals
        if observation.outcome == "accepted" and observation.replan_count == 0:
            update_type = "promote"
            rationale = "Workflow completed without dynamic replanning."
        elif observation.outcome == "accepted":
            update_type = "revise"
            rationale = (
                "Workflow completed after dynamic workflow updates; pattern should learn "
                "the successful replan path."
            )
        elif observation.outcome in {"replanned", "retried"}:
            update_type = "revise"
            rationale = "Workflow required dynamic correction and should be reviewed."
        else:
            update_type = "demote"
            rationale = "Workflow did not reach acceptance."
        rationale += (
            f" retry_count={observation.retry_count};"
            f" replan_count={observation.replan_count};"
            f" branch_candidate_count={observation.branch_candidate_count}."
        )
        if observation.scaling_observations:
            level = observation.scaling_observations.get("current_scale_level")
            planned_agents = observation.scaling_observations.get("planned_agent_count")
            next_hint = observation.scaling_observations.get("next_scaling_hint")
            rationale += (
                f" scaling_level={level};"
                f" planned_agent_count={planned_agents};"
                f" next_scaling_hint={next_hint}."
            )
        if observation.reflection_attribution:
            categories = observation.reflection_attribution.get("categories")
            primary = observation.reflection_attribution.get("primary_category")
            recommended_learning = observation.reflection_attribution.get(
                "recommended_learning"
            )
            rationale += (
                f" reflection_primary={primary};"
                f" reflection_categories={categories};"
                f" reflection_learning={recommended_learning}."
            )
        for pattern_id in observation.pattern_ids_used:
            proposal = ExperienceUpdateProposal(
                proposal_id=f"workflow-exp-proposal-{uuid.uuid4().hex[:12]}",
                proposed_by=proposed_by,
                target_pattern_id=pattern_id,
                update_type=update_type,
                evidence_refs=observation.evidence_refs,
                rationale=rationale,
            )
            self.propose_update(proposal)
            proposals.append(proposal)
        return proposals

    def _validate_pattern(self, pattern: ExperiencePattern) -> None:
        if pattern.pattern_type not in PATTERN_TYPES:
            raise ValueError(f"Unknown experience pattern_type: {pattern.pattern_type}")
        if pattern.status not in PATTERN_STATUSES:
            raise ValueError(f"Unknown experience status: {pattern.status}")
        if not pattern.pattern_id:
            raise ValueError("ExperiencePattern requires pattern_id")
        if not pattern.description:
            raise ValueError("ExperiencePattern requires description")
        if not 0 <= pattern.confidence_score <= 10:
            raise ValueError("confidence_score must be between 0 and 10")

    def _validate_observation(self, observation: ExperienceObservation) -> None:
        if observation.outcome not in OBSERVATION_OUTCOMES:
            raise ValueError(f"Unknown experience outcome: {observation.outcome}")
        if not observation.observation_id:
            raise ValueError("ExperienceObservation requires observation_id")

    def _validate_workflow_observation(
        self,
        observation: WorkflowExperienceObservation,
    ) -> None:
        if observation.outcome not in OBSERVATION_OUTCOMES:
            raise ValueError(f"Unknown workflow experience outcome: {observation.outcome}")
        if not observation.observation_id:
            raise ValueError("WorkflowExperienceObservation requires observation_id")
        if not observation.run_id or not observation.graph_id:
            raise ValueError("WorkflowExperienceObservation requires run_id and graph_id")

    def _validate_proposal(self, proposal: ExperienceUpdateProposal) -> None:
        if proposal.update_type not in UPDATE_TYPES:
            raise ValueError(f"Unknown experience update_type: {proposal.update_type}")
        if proposal.review_status not in REVIEW_STATUSES:
            raise ValueError(f"Unknown experience review_status: {proposal.review_status}")
        if not proposal.target_pattern_id:
            raise ValueError("ExperienceUpdateProposal requires target_pattern_id")

    def _append_jsonl(self, path: Path, item: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if isinstance(raw, dict):
                rows.append(raw)
        return rows


def default_seed_patterns() -> list[ExperiencePattern]:
    """Seed patterns distilled from the bundled multi-agent orchestration report."""

    source = "多agent编排报告_20260421.zip"
    return [
        ExperiencePattern(
            pattern_id="seed-topology-parallel-explore-implement-verify",
            pattern_type="topology",
            description="Use parallel read-only exploration before a single writer and verification gate for complex engineering work.",
            applicability_signals=[
                "复杂",
                "complex",
                "swe",
                "多 worker",
                "多agent",
                "测试",
                "验证",
                "实现",
                "修改",
            ],
            anti_signals=["简单", "single file", "只读总结"],
            recommended_structure={
                "topology": "parallel_explore_then_single_writer_then_verify",
                "nodes": ["explore", "implement", "verify"],
                "join_policy": "all_explorers_before_writer",
            },
            required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
            risk_notes=[
                "Parallelism should be read-only before the writer lock.",
                "Do not create extra workers when the task has no dependency or uncertainty signal.",
            ],
            source_refs=[
                f"{source}: research/comparison_matrix_source.md",
                f"{source}: site/open_source_router_scheduler_task_graph.html",
            ],
            evidence_level="strong",
            confidence_score=7.0,
            tags=["director", "workflow", "parallel", "worker"],
        ),
        ExperiencePattern(
            pattern_id="seed-role-overlooker-review-rework",
            pattern_type="review_loop",
            description="Treat review and rework as a first-class loop with explicit evidence citation and re-entry target.",
            applicability_signals=[
                "review",
                "overlooker",
                "校验",
                "验证",
                "返工",
                "失败",
                "retry",
                "rework",
            ],
            anti_signals=["无需测试", "no review"],
            recommended_structure={
                "review_object": "evidence_bundle",
                "gate": "overlooker_acceptance",
                "reentry": ["retry_same_node", "request_director_replan"],
            },
            required_evidence=["evidence_ref", "validator_refs", "overlooker_report"],
            risk_notes=[
                "Review must cite evidence_ref.",
                "Repeated same-failure retries should demote the pattern or request replan.",
            ],
            source_refs=[
                f"{source}: site/engineering_review_and_rework_loops.html",
                f"{source}: site/open_source_failure_escalation_retry_and_fallback.html",
            ],
            evidence_level="strong",
            confidence_score=8.0,
            tags=["overlooker", "review", "retry", "validation"],
        ),
        ExperiencePattern(
            pattern_id="seed-handoff-artifact-chain",
            pattern_type="handoff",
            description="Use artifact-chain handoff for software engineering nodes where the next step consumes code, logs, test output, and evidence refs.",
            applicability_signals=[
                "artifact",
                "evidence",
                "工件",
                "证据",
                "swe",
                "patch",
                "代码",
            ],
            anti_signals=["pure chat", "无产物"],
            recommended_structure={
                "handoff_unit": "artifact_chain",
                "state_transfer": ["workspace_diff", "test_report", "evidence_ref"],
                "receiver": "next_graph_node_or_overlooker",
            },
            required_evidence=["diff", "test", "log"],
            risk_notes=[
                "Artifact handoff is not a substitute for checkpointed workspace state.",
            ],
            source_refs=[
                f"{source}: site/handoff_contracts_and_state_transfer.html",
                f"{source}: research/software_company_orchestration_source.md",
            ],
            evidence_level="mixed",
            confidence_score=6.0,
            tags=["handoff", "artifact", "state"],
        ),
        ExperiencePattern(
            pattern_id="seed-failure-watchdog-budget",
            pattern_type="failure_policy",
            description="Bound retries with same-failure counters, attempt limits, timeout/resource reports, and explicit stop or replan events.",
            applicability_signals=[
                "retry",
                "失败",
                "超时",
                "budget",
                "livelock",
                "deadlock",
                "重试",
                "重规划",
            ],
            anti_signals=["one shot", "不重试"],
            recommended_structure={
                "guards": ["max_attempts", "retry_budget", "max_same_failure_retries"],
                "escalation": ["abort_livelock", "request_director_replan"],
            },
            required_evidence=["resource_report", "sandbox_events", "failure_type"],
            risk_notes=[
                "Do not retry indefinitely.",
                "Fork retries from accepted upstream workspaces when possible.",
            ],
            source_refs=[
                f"{source}: site/open_source_stop_timeout_watchdog_and_budget_guards.html",
                f"{source}: site/open_source_failure_escalation_retry_and_fallback.html",
            ],
            evidence_level="strong",
            confidence_score=8.0,
            tags=["retry", "watchdog", "budget", "replan"],
        ),
        ExperiencePattern(
            pattern_id="seed-routing-message-or-graph-edge",
            pattern_type="routing",
            description="Select routing surfaces by task shape: graph edges for explicit dependencies, message/team routing for deliberation, planner recipient routing for tool-specialized workers.",
            applicability_signals=[
                "routing",
                "调度",
                "任务图",
                "graph",
                "依赖",
                "worker",
                "specialist",
            ],
            anti_signals=["单步", "linear only"],
            recommended_structure={
                "dependency_tasks": "graph_edges",
                "deliberation_tasks": "message_publication",
                "tool_specialized_tasks": "planner_recipient_set",
            },
            required_evidence=["workflow_skeleton", "edges", "node_roles"],
            risk_notes=[
                "Routing choice must be compiled against known node ids and DAG rules.",
            ],
            source_refs=[
                f"{source}: site/open_source_router_scheduler_task_graph.html",
                f"{source}: research/open_source_control_loops_source.md",
            ],
            evidence_level="mixed",
            confidence_score=6.5,
            tags=["routing", "scheduler", "graph"],
        ),
        ExperiencePattern(
            pattern_id="seed-aggregation-layered-proposer-aggregator",
            pattern_type="aggregation",
            description="Use layered proposer agents followed by aggregator agents when answer quality benefits from independent proposals and synthesis.",
            applicability_signals=[
                "mixture",
                "moa",
                "aggregate",
                "aggregator",
                "ensemble",
                "多模型",
                "多候选",
                "综合",
                "汇总",
                "评审多个方案",
            ],
            anti_signals=["single source of truth", "唯一可执行补丁", "低延迟"],
            recommended_structure={
                "topology": "layered_proposer_aggregator",
                "layers": ["proposal_layer", "aggregation_layer", "final_selection"],
                "join_policy": "aggregate_all_or_quorum_before_next_layer",
                "agent_count_policy": "derive proposers from hypothesis diversity and aggregators from synthesis risk",
            },
            required_evidence=["candidate_outputs", "aggregation_rationale", "selected_output_ref"],
            risk_notes=[
                "Aggregation can amplify correlated mistakes when proposers share the same blind spots.",
                "Use bounded layers when latency or token budget is strict.",
            ],
            source_refs=[
                f"{source}: site/mixture_of_agents_enhances_large_language_model_capabilities.html",
                f"{source}: site/tumix_multi_agent_test_time_scaling_with_tool_use_mixture.html",
            ],
            evidence_level="direct",
            confidence_score=7.0,
            tags=["aggregation", "ensemble", "proposer", "synthesis", "moa"],
        ),
        ExperiencePattern(
            pattern_id="seed-aggregation-opendeepthink-population-bt-evolution",
            pattern_type="aggregation",
            description=(
                "Use OpenDeepThink-style population evolution: sample candidate solutions, "
                "compare them pairwise, aggregate noisy preferences with Bradley-Terry ranking, "
                "preserve elites, discard the bottom quartile, mutate the top three quarters "
                "with negative pairwise feedback, and finish with a denser BT selection round."
            ),
            applicability_signals=[
                "OpenDeepThink",
                "Bradley-Terry",
                "BT aggregation",
                "population",
                "parallel reasoning",
                "pairwise comparison",
                "mutation",
                "candidate evolution",
                "competitive programming",
                "verifiable",
                "多候选",
                "种群",
                "并行推理",
                "两两比较",
                "进化",
                "变异",
                "客观判题",
            ],
            anti_signals=[
                "low budget",
                "低预算",
                "subjective judgment",
                "主观题",
                "pairwise judge unreliable",
                "single deterministic patch",
                "不可并行生成候选",
            ],
            recommended_structure={
                "workflow": [
                    "candidate_sampling_n",
                    "pairwise_comparison_K_regular_matching",
                    "bradley_terry_ranking",
                    "elite_preservation_top_25",
                    "discard_bottom_25",
                    "feedback_mutation_top_75",
                    "repeat_T_generations",
                    "final_dense_pairwise_comparison_M",
                    "final_bradley_terry_top1_selection",
                    "external_or_local_validator",
                ],
                "default_hyperparameters": {
                    "population_size_n": 20,
                    "K_pairwise_peers_per_candidate": 4,
                    "T_generations": 3,
                    "M_final_pairs_per_candidate": 10,
                    "lambda_l2": 0.01,
                },
                "selection_policy": {
                    "aggregator": "regularized_bradley_terry",
                    "elite_preservation": "top_25_percent_carried_forward_unchanged",
                    "discard": "bottom_25_percent",
                    "tie_policy": "half_win_each_side",
                    "presentation_order": "randomized_per_pair_to_reduce_position_bias",
                },
                "feedback_policy": {
                    "critique_source": "pairwise_judge_rationales",
                    "mutation_scope": "top_75_percent_including_elites",
                    "negative_feedback_priority": True,
                    "restart_permission": "mutator_may_abandon_current_approach",
                },
            },
            required_evidence=[
                "population_candidates",
                "pairwise_comparison_matrix",
                "pairwise_feedback_by_candidate",
                "bt_scores_per_generation",
                "elite_set",
                "discard_set",
                "mutation_outputs",
                "final_bt_scores",
                "validator_result",
            ],
            risk_notes=[
                "This pattern is expensive: the main OpenDeepThink setting uses about 285 calls per problem.",
                "It amplifies partial competence rather than creating entirely new capability when no candidate is near correct.",
                "It works best when pairwise judging can distinguish objectively correct from incorrect candidates.",
                "On subjective or ambiguous tasks, BT selection can amplify judge noise.",
                "A small two-proposer implementation is only a budget-limited approximation, not the full pattern.",
            ],
            source_refs=[
                "https://arxiv.org/abs/2605.15177",
                "https://github.com/ZhouShang0817/CF-73",
                "OpenDeepThink: Parallel Reasoning via Bradley-Terry Aggregation, Algorithm 1 and Sections 3-5",
            ],
            evidence_level="direct",
            confidence_score=8.0,
            tags=[
                "opendeepthink",
                "bradley-terry",
                "bt",
                "population",
                "evolution",
                "pairwise",
                "mutation",
                "aggregation",
                "test-time-scaling",
            ],
        ),
        ExperiencePattern(
            pattern_id="seed-topology-graph-of-agents-message-passing",
            pattern_type="topology",
            description="Represent collaboration as a typed agent graph with message passing when dependencies are non-linear and intermediate reasoning must be recombined.",
            applicability_signals=[
                "graph of agents",
                "message passing",
                "非线性",
                "多依赖",
                "图结构",
                "协作推理",
                "intermediate",
            ],
            anti_signals=["线性即可", "single chain", "无依赖"],
            recommended_structure={
                "topology": "graph_message_passing",
                "nodes": ["specialist_agents", "intermediate_aggregation", "final_solver"],
                "edge_policy": "typed_messages_between_dependency_neighbors",
                "join_policy": "compile_edges_before_execution",
            },
            required_evidence=["workflow_skeleton", "edges", "message_contracts"],
            risk_notes=[
                "Graph edges must remain acyclic or have bounded iteration semantics.",
                "Message contracts should prevent hidden state from leaking across unrelated branches.",
            ],
            source_refs=[
                f"{source}: site/graph_of_agents_a_graph_based_framework_for_multi_agent_llm_collaboration.html",
                f"{source}: site/topology_matters_measuring_memory_leakage_in_multi_agent_llms.html",
            ],
            evidence_level="direct",
            confidence_score=7.0,
            tags=["graph", "topology", "message", "collaboration"],
        ),
        ExperiencePattern(
            pattern_id="seed-dynamic-topology-semantic-routing",
            pattern_type="dynamic_topology",
            description="Adapt the collaboration topology during reasoning by semantically routing work to agents whose prior outputs best match the current subproblem.",
            applicability_signals=[
                "dytopo",
                "dynamic topology",
                "semantic routing",
                "动态拓扑",
                "动态路由",
                "语义匹配",
                "uncertain route",
            ],
            anti_signals=["固定流程", "static dag only", "合规审计要求固定路径"],
            recommended_structure={
                "routing_policy": "semantic_match_to_active_agent_state",
                "topology_update": "bounded_edge_rewrite_with_trace",
                "stability_guard": "require_replan_on_repeated_route_failure",
                "agent_count_policy": "start_small_then_expand_when_route_entropy_remains_high",
            },
            required_evidence=["route_scores", "topology_changes", "deliberation_trace"],
            risk_notes=[
                "Dynamic routing needs explicit trace evidence so later validation can replay why an edge changed.",
                "Repeated route churn should trigger Director replan instead of unbounded self-adjustment.",
            ],
            source_refs=[
                f"{source}: site/dytopo_dynamic_topology_routing_for_multi_agent_reasoning_via_semantic_matching.html",
                f"{source}: site/masrouter_learning_to_route_llms_for_multi_agent_systems.html",
            ],
            evidence_level="direct",
            confidence_score=6.5,
            tags=["routing", "dynamic", "topology", "semantic"],
        ),
        ExperiencePattern(
            pattern_id="seed-routing-learned-mas-gates",
            pattern_type="routing",
            description="Use learned or lightweight gate routing when the main decision is which agent, model, or tool specialist should receive the next task.",
            applicability_signals=[
                "masrouter",
                "agentgate",
                "latentgate",
                "router",
                "gate",
                "路由器",
                "低延迟",
                "专家选择",
                "specialist selection",
            ],
            anti_signals=["需要完整人工审计", "no training data", "固定角色就足够"],
            recommended_structure={
                "router_input": ["task_features", "agent_capability_summary", "recent_outcome_stats"],
                "router_output": ["recipient_agent_ids", "routing_confidence", "fallback_route"],
                "fallback_policy": "use Director deterministic graph edge when routing confidence is low",
            },
            required_evidence=["routing_decision", "confidence_score", "fallback_reason"],
            risk_notes=[
                "Learned routers should not be allowed to expand authority; they only choose among compiled recipients.",
                "Low-confidence routing must fall back to Director or compiler-approved graph edges.",
            ],
            source_refs=[
                f"{source}: site/masrouter_learning_to_route_llms_for_multi_agent_systems.html",
                f"{source}: site/agentgate_a_lightweight_structured_routing_engine_for_the_internet_of_agents.html",
                f"{source}: site/latentgate_low_latency_semantic_routing_via_frozen_backbone_probing_of_small_language_models.html",
            ],
            evidence_level="direct",
            confidence_score=6.5,
            tags=["routing", "gate", "specialist", "low-latency"],
        ),
        ExperiencePattern(
            pattern_id="seed-memory-hierarchical-task-experience-planner",
            pattern_type="memory_planning",
            description="Use a centralized hierarchical planner backed by task experience memory when the system must decompose work and reuse prior plans.",
            applicability_signals=[
                "stackplanner",
                "hierarchical",
                "memory",
                "经验",
                "经验库",
                "层级规划",
                "任务分解",
                "reuse prior",
            ],
            anti_signals=["stateless", "一次性", "no historical evidence"],
            recommended_structure={
                "planner_layers": ["task_diagnosis", "subtask_tree", "experience_retrieval", "execution_plan"],
                "memory_use": ["retrieve_patterns", "compare_prior_outcomes", "record_observations"],
                "replan_policy": "update subtask tree when memory evidence conflicts with current validation",
            },
            required_evidence=["retrieved_experience_refs", "subtask_tree", "memory_update_refs"],
            risk_notes=[
                "Experience memory should influence structure but must not bypass compiler checks.",
                "Stale experience needs versioning and demotion when runtime evidence degrades.",
            ],
            source_refs=[
                f"{source}: site/stackplanner_a_centralized_hierarchical_multi_agent_system_with_task_experience_memory_management.html",
                f"{source}: site/scaling_teams_or_scaling_time_memory_enabled_lifelong_learning_in_llm_multi_agent_systems.html",
            ],
            evidence_level="direct",
            confidence_score=7.0,
            tags=["memory", "hierarchical", "planner", "experience"],
        ),
        ExperiencePattern(
            pattern_id="seed-scaling-large-dynamic-hierarchy",
            pattern_type="scaling_policy",
            description="Scale to large agent teams through staged hierarchy, delegated subgroups, and promotion of local results to higher-level synthesis.",
            applicability_signals=[
                "megaagent",
                "large scale",
                "hundreds",
                "dozens",
                "大规模",
                "上百",
                "数十",
                "复杂到超出预期",
                "scale",
            ],
            anti_signals=["小任务", "single file", "低风险局部改动"],
            recommended_structure={
                "scaling_levels": ["single_team", "multi_team", "hierarchical_program"],
                "expansion_triggers": ["module_count_growth", "independent_subproblem_growth", "validation_surface_growth"],
                "coordination_policy": "subteam_leads_summarize_to_single_director",
                "agent_count_policy": "Director estimates current need and declares replan thresholds for larger tiers",
            },
            required_evidence=["agent_allocation", "scale_triggers", "subteam_boundaries"],
            risk_notes=[
                "Large teams need explicit subteam boundaries to avoid duplicate writes and uncontrolled communication.",
                "Scaling should be staged; a larger plan is held as a replan option until triggers are met.",
            ],
            source_refs=[
                f"{source}: site/megaagent_a_large_scale_autonomous_llm_based_multi_agent_system_without_predefined_sops.html",
                f"{source}: site/scaling_large_language_model_based_multi_agent_collaboration.html",
                f"{source}: site/towards_a_science_of_scaling_agent_systems.html",
            ],
            evidence_level="direct",
            confidence_score=6.5,
            tags=["scaling", "hierarchy", "large-team", "director"],
        ),
        ExperiencePattern(
            pattern_id="seed-scaling-adaptive-population-curriculum",
            pattern_type="scaling_policy",
            description=(
                "Scale a multi-agent workflow through a learned curriculum rather than a dedicated "
                "hard-coded algorithm: start with a small candidate pool, measure candidate quality "
                "and selection uncertainty, then expand population size, comparison density, mutation "
                "depth, verifier strength, or expert routing only when observations justify the extra budget."
            ),
            applicability_signals=[
                "adaptive scaling",
                "self learning",
                "test-time scaling",
                "population",
                "candidate pool",
                "benchmark",
                "accuracy",
                "hard cases",
                "difficult",
                "verifiable",
                "reasoning",
                "competitive programming",
                "algorithm",
                "validator",
                "token",
                "latency",
                "budget",
                "performance",
                "pairwise judge",
                "Bradley-Terry",
                "mutation",
                "scale up",
                "gradually expand",
                "自我学习",
                "逐步扩大",
                "动态扩容",
                "候选池",
                "最难",
                "准确率",
                "题目",
                "竞赛",
                "推理",
                "判题",
                "复杂任务",
                "性能",
                "论文",
                "两两比较",
                "变异",
                "经验驱动",
            ],
            anti_signals=[
                "strict low latency",
                "低延迟",
                "single deterministic operation",
                "不可并行",
                "no measurable validator",
                "无评价指标",
                "unbounded cost",
            ],
            recommended_structure={
                "controller_node": "Director owns the scaling policy and updates it from observations; no separate hard-coded OpenDeepThink executor is required.",
                "generic_primitives": [
                    "candidate_sampling",
                    "candidate_diversity_check",
                    "pairwise_preference_judging",
                    "ranking_aggregation",
                    "elite_preservation",
                    "weak_candidate_discard",
                    "negative_feedback_mutation",
                    "repair_or_rewrite",
                    "external_or_local_validation",
                    "experience_update",
                ],
                "ladder": [
                    {"level": 0, "shape": "single_candidate_baseline", "use_when": "task appears easy or budget is extremely tight"},
                    {"level": 1, "shape": "small_pool_2_to_3_candidates_all_pairs", "use_when": "uncertainty exists but no evidence yet justifies large scale"},
                    {"level": 2, "shape": "medium_pool_5_to_8_candidates_pairwise_K2_or_K3", "use_when": "compile succeeds but semantic/sample failures persist, or candidates disagree materially"},
                    {"level": 3, "shape": "evolution_loop_with_elites_and_mutation", "use_when": "at least one candidate is near-correct or pairwise critique identifies actionable failures"},
                    {"level": 4, "shape": "large_population_BT_style_n12_to_n20_K4_T2_to_T3_M8_to_M10", "use_when": "task is objectively verifiable, budget supports breadth, and earlier levels show partial competence"},
                ],
                "scale_up_triggers": [
                    "candidate_diversity_low_add_role_or_sampling_temperature",
                    "selection_uncertainty_high_increase_pairwise_density",
                    "some_candidates_compile_but_fail_samples_add_mutation_depth",
                    "one_or_more_candidates_pass_samples_preserve_elites_and_mutate_neighbors",
                    "all_candidates_fail_compile_add_compile_repair_before_more_population",
                    "repeated_semantic_failure_route_to_retrieval_or_domain_expert_before_more_generation",
                ],
                "scale_down_triggers": [
                    "no_candidate_near_correct_after_budget_tier",
                    "pairwise_judge_disagrees_with_validator",
                    "token_or_latency_budget_exceeded",
                    "candidate_pool_collapses_to_duplicate_strategies",
                ],
                "observations_to_record": [
                    "candidate_count",
                    "comparison_count",
                    "ranking_entropy",
                    "candidate_diversity",
                    "compile_rate",
                    "validator_pass_rate",
                    "repair_rescue_rate",
                    "mutation_rescue_rate",
                    "judge_validator_alignment",
                    "token_cost",
                    "latency",
                    "successful_scale_level",
                ],
            },
            required_evidence=[
                "scale_level",
                "scaling_decision_basis",
                "candidate_metrics",
                "pairwise_or_validator_metrics",
                "budget_report",
                "experience_update",
            ],
            risk_notes=[
                "A scaling ladder can waste budget if it expands before detecting partial competence.",
                "More candidates do not help when every candidate shares the same missing domain knowledge.",
                "If repeated semantic failures occur, route to retrieval/editorial/domain expertise before increasing population.",
                "The Director must explicitly compare scale-up and scale-down options after each round.",
                "OpenDeepThink is treated as a high-scale point on this ladder, not as a special dedicated executor.",
            ],
            source_refs=[
                "https://arxiv.org/abs/2605.15177",
                "https://github.com/ZhouShang0817/CF-73",
                "batch_multiagent_eval_20260518/runs/opendeepthink-pattern-only-comparison/PATTERN_ONLY_COMPARISON.md",
                "batch_multiagent_eval_20260518/runs/opendeepthink-learning-loop/LEARNING_LOOP_REPORT.md",
            ],
            evidence_level="direct",
            confidence_score=7.5,
            tags=[
                "adaptive-scaling",
                "curriculum",
                "population",
                "bt",
                "mutation",
                "director",
                "experience-learning",
                "test-time-scaling",
            ],
        ),
        ExperiencePattern(
            pattern_id="seed-generation-evolutionary-agent-search",
            pattern_type="agent_generation",
            description="Generate and select agent variants through evolutionary search when the right role composition is unknown and can be evaluated by a metric.",
            applicability_signals=[
                "evoagent",
                "evolutionary",
                "agent generation",
                "自动生成agent",
                "角色搜索",
                "优化编排",
                "metric",
            ],
            anti_signals=["无评价指标", "不可重复实验", "生产高风险热路径"],
            recommended_structure={
                "generation_loop": ["propose_agent_variants", "evaluate_variants", "select_or_mutate", "freeze_winner"],
                "selection_policy": "metric_improvement_with_budget_limit",
                "promotion_gate": "compiler_and_overlooker_acceptance_before_runtime_use",
            },
            required_evidence=["variant_specs", "evaluation_metric", "selection_rationale"],
            risk_notes=[
                "Generated agents remain draft until promoted by review and compiler validation.",
                "Evolutionary search needs a clear metric; otherwise it becomes expensive prompt churn.",
            ],
            source_refs=[
                f"{source}: site/evoagent_towards_automatic_multi_agent_generation_via_evolutionary_algorithms.html",
                f"{source}: site/comas_co_evolving_multi_agent_systems_via_interaction_rewards.html",
            ],
            evidence_level="direct",
            confidence_score=5.5,
            tags=["agent-generation", "evolution", "optimization", "metric"],
        ),
        ExperiencePattern(
            pattern_id="seed-generation-self-configuring-rectifying-mas",
            pattern_type="agent_generation",
            description="Let the system propose configuration, execution, and rectification changes, but require explicit validation gates before accepting self-changes.",
            applicability_signals=[
                "mas2",
                "self-generative",
                "self-configuring",
                "self-rectifying",
                "自生成",
                "自配置",
                "自纠错",
                "自动修正",
            ],
            anti_signals=["strict manual only", "不可自修改", "审计不可缺失"],
            recommended_structure={
                "self_cycle": ["generate_structure", "configure_roles", "execute", "rectify_from_feedback"],
                "acceptance_gate": "structured_patch_compiler_then_overlooker",
                "rollback_policy": "retain_last_accepted_configuration_checkpoint",
            },
            required_evidence=["configuration_delta", "feedback_refs", "acceptance_gate_result"],
            risk_notes=[
                "Self-configuration must create proposals, not directly mutate trusted runtime policy.",
                "Repeated self-rectification with the same failure type should force Director replan.",
            ],
            source_refs=[
                f"{source}: site/mas2_self_generative_self_configuring_self_rectifying_multi_agent_systems.html",
                f"{source}: site/dover_intervention_driven_auto_debugging_for_llm_multi_agent_systems.html",
            ],
            evidence_level="direct",
            confidence_score=5.5,
            tags=["self-configuring", "rectification", "feedback", "proposal"],
        ),
        ExperiencePattern(
            pattern_id="seed-governance-cross-team-orchestration",
            pattern_type="governance",
            description="Use cross-team orchestration when independent teams must solve different parts and exchange bounded summaries through team leads.",
            applicability_signals=[
                "cross-team",
                "team",
                "多团队",
                "跨团队",
                "subteam",
                "团队协作",
                "large project",
            ],
            anti_signals=["单人任务", "无模块边界", "tight write conflict"],
            recommended_structure={
                "team_units": ["team_lead", "specialist_workers", "team_verifier"],
                "handoff_policy": "team_summary_and_artifact_refs_only_across_boundaries",
                "coordination_layer": "Director receives team lead reports and emits graph patches when needed",
            },
            required_evidence=["team_boundary_map", "team_reports", "cross_team_handoff_refs"],
            risk_notes=[
                "Cross-team work needs clear ownership boundaries before write nodes run.",
                "Team leads should summarize evidence rather than exposing every private scratch note.",
            ],
            source_refs=[
                f"{source}: site/multi_agent_collaboration_via_cross_team_orchestration.html",
                f"{source}: site/open_source_coordinator_shells_and_team_hierarchies.html",
            ],
            evidence_level="direct",
            confidence_score=6.0,
            tags=["team", "governance", "hierarchy", "handoff"],
        ),
        ExperiencePattern(
            pattern_id="seed-governance-company-role-lifecycle",
            pattern_type="governance",
            description="Model a software task as company-like roles with staged artifacts, role-specific responsibilities, and review/promotion boundaries.",
            applicability_signals=[
                "orgagent",
                "company",
                "metagpt",
                "chatdev",
                "软件公司",
                "角色分工",
                "产品",
                "工程",
                "qa",
            ],
            anti_signals=["无角色区分", "只需单步修复", "freeform brainstorm"],
            recommended_structure={
                "role_lifecycle": ["planning", "design", "implementation", "qa", "release_review"],
                "artifact_promotions": ["requirements", "design_notes", "patch", "test_report", "release_summary"],
                "ownership_policy": "each role owns outputs but Director controls workflow promotion",
            },
            required_evidence=["role_assignments", "artifact_promotions", "review_records"],
            risk_notes=[
                "Company metaphors can add ceremony; use only when staged artifacts reduce ambiguity.",
                "Role separation should not create multiple unsynchronized writers over the same files.",
            ],
            source_refs=[
                f"{source}: site/orgagent_organize_your_multi_agent_system_like_a_company.html",
                f"{source}: research/software_company_orchestration_source.md",
                f"{source}: site/metagpt_orchestration_and_agent_structure.html",
                f"{source}: site/chatdev_orchestration_and_agent_structure.html",
            ],
            evidence_level="mixed",
            confidence_score=6.0,
            tags=["role-template", "company", "artifact", "qa"],
        ),
        ExperiencePattern(
            pattern_id="seed-memory-scale-time-before-team-size",
            pattern_type="memory_planning",
            description="Prefer richer memory and staged time scaling before adding many agents when the bottleneck is continuity rather than independent parallel work.",
            applicability_signals=[
                "scaling time",
                "lifelong",
                "memory enabled",
                "长期任务",
                "持续学习",
                "上下文连续",
                "历史记忆",
            ],
            anti_signals=["高度并行", "独立模块很多", "urgent wall clock"],
            recommended_structure={
                "scaling_choice": "extend_memory_and_iteration_depth_before_team_size",
                "memory_layers": ["run_trace", "accepted_artifacts", "experience_observations"],
                "promotion_policy": "only accepted observations update reusable memory",
            },
            required_evidence=["memory_refs", "iteration_trace", "accepted_observations"],
            risk_notes=[
                "Adding more agents can hurt if the task mostly needs continuity and accumulated context.",
                "Memory growth needs pruning and confidence decay to avoid stale plans dominating.",
            ],
            source_refs=[
                f"{source}: site/scaling_teams_or_scaling_time_memory_enabled_lifelong_learning_in_llm_multi_agent_systems.html",
                f"{source}: site/on_the_uncertainty_of_large_language_model_based_multi_agent_systems.html",
            ],
            evidence_level="direct",
            confidence_score=6.0,
            tags=["memory", "scaling", "lifelong", "context"],
        ),
        ExperiencePattern(
            pattern_id="seed-security-topology-privacy-leakage-control",
            pattern_type="security_policy",
            description="Choose topology and communication exposure with privacy leakage, collusion, and communication-attack risks in mind.",
            applicability_signals=[
                "security",
                "privacy",
                "leakage",
                "collusion",
                "attack",
                "隐私",
                "泄漏",
                "安全",
                "通信攻击",
            ],
            anti_signals=["公开数据", "no trust boundary", "toy benchmark"],
            recommended_structure={
                "risk_model": ["memory_leakage", "collusion", "communication_attack", "sensitive_artifact_exposure"],
                "topology_preference": "least_exposure_edges_with_auditable_handoffs",
                "review_gate": "security_overlooker_for_sensitive_cross_agent_handoffs",
            },
            required_evidence=["trust_boundary_map", "handoff_refs", "security_review_result"],
            risk_notes=[
                "Do not broadcast sensitive artifacts when a point-to-point handoff is enough.",
                "Security patterns constrain communication shape; they do not grant new authority.",
            ],
            source_refs=[
                f"{source}: site/topology_matters_measuring_memory_leakage_in_multi_agent_llms.html",
                f"{source}: site/g_safeguard_a_topology_guided_security_lens_and_treatment_on_llm_based_multi_agent_systems.html",
                f"{source}: site/agentleak_a_full_stack_benchmark_for_privacy_leakage_in_multi_agent_llm_systems.html",
                f"{source}: site/red_teaming_llm_multi_agent_systems_via_communication_attacks.html",
            ],
            evidence_level="direct",
            confidence_score=6.5,
            tags=["security", "privacy", "topology", "handoff"],
        ),
        ExperiencePattern(
            pattern_id="seed-review-verification-aware-planning",
            pattern_type="review_loop",
            description="Plan tasks around verifiability by creating evidence targets before worker execution and routing uncertain claims to explicit checks.",
            applicability_signals=[
                "verification-aware",
                "verifiable",
                "验证优先",
                "可验证",
                "测试证据",
                "校验",
                "acceptance",
            ],
            anti_signals=["不可测试", "纯主观创作", "no evaluator"],
            recommended_structure={
                "planning_order": ["define_acceptance", "map_evidence_targets", "execute", "verify", "repair_or_accept"],
                "uncertainty_policy": "claims_without_evidence_become_verification_nodes",
                "gate": "overlooker_acceptance_after_validator",
            },
            required_evidence=["acceptance_criteria", "evidence_targets", "validator_findings"],
            risk_notes=[
                "Verification-aware planning can add overhead for low-risk exploratory work.",
                "Claims without evidence should not be promoted into accepted artifacts.",
            ],
            source_refs=[
                f"{source}: site/verification_aware_planning_for_multi_agent_systems.html",
                f"{source}: site/benchmark_judge_and_evaluator_loop.html",
                f"{source}: site/engineering_review_queues_and_validation_gates.html",
            ],
            evidence_level="direct",
            confidence_score=7.0,
            tags=["verification", "review", "evidence", "acceptance"],
        ),
        ExperiencePattern(
            pattern_id="seed-failure-intervention-debugging-loop",
            pattern_type="failure_policy",
            description="Use intervention-driven debugging when a multi-agent run fails: classify failure, patch the workflow or node prompt, and retry from the last sound checkpoint.",
            applicability_signals=[
                "dover",
                "debugging",
                "auto debugging",
                "intervention",
                "失败分析",
                "自动调试",
                "修复循环",
                "checkpoint",
            ],
            anti_signals=["无检查点", "不能重跑", "irreproducible"],
            recommended_structure={
                "debug_loop": ["failure_classification", "intervention_patch", "checkpoint_fork", "targeted_retry"],
                "fork_policy": "reuse_accepted_upstream_state_and_replace_only_failed_branch",
                "stop_policy": "escalate_after_same_failure_budget",
            },
            required_evidence=["failure_type", "intervention_patch", "checkpoint_ref", "retry_result"],
            risk_notes=[
                "Debug interventions should be narrow and evidence-linked.",
                "Retrying from the failed workspace instead of a sound checkpoint can preserve bad state.",
            ],
            source_refs=[
                f"{source}: site/dover_intervention_driven_auto_debugging_for_llm_multi_agent_systems.html",
                f"{source}: site/open_source_failure_escalation_retry_and_fallback.html",
            ],
            evidence_level="direct",
            confidence_score=6.5,
            tags=["debugging", "retry", "checkpoint", "overlooker"],
        ),
        ExperiencePattern(
            pattern_id="seed-tool-planning-context-efficient-search",
            pattern_type="tool_planning",
            description="Use context-efficient depth-first tool planning when the task requires many tool calls and only a narrow frontier should stay in context.",
            applicability_signals=[
                "tool use",
                "tool planning",
                "dfsdt",
                "context efficient",
                "工具调用",
                "工具规划",
                "长链工具",
                "搜索",
            ],
            anti_signals=["no tools", "pure reasoning", "small context"],
            recommended_structure={
                "tool_plan": ["select_tool_frontier", "execute_bounded_step", "summarize_state", "continue_or_backtrack"],
                "context_policy": "keep_active_frontier_and_artifact_refs_not_full_trace",
                "handoff_policy": "tool_results_become_artifact_refs_for_next_agent",
            },
            required_evidence=["tool_plan", "tool_result_refs", "state_summary"],
            risk_notes=[
                "Tool planning needs strict result summarization or context will fill with stale traces.",
                "Backtracking must not discard accepted artifacts without an explicit rollback event.",
            ],
            source_refs=[
                f"{source}: site/smurfs_multi_agent_system_using_context_efficient_dfsdt_for_tool_planning.html",
                f"{source}: site/tool_and_message_routing.html",
            ],
            evidence_level="direct",
            confidence_score=5.5,
            tags=["tool-planning", "context", "search", "handoff"],
        ),
        ExperiencePattern(
            pattern_id="seed-routing-disagreement-based-recruitment",
            pattern_type="routing",
            description="Recruit additional agents or tools when independent agents disagree in a way that exposes missing evidence or capability gaps.",
            applicability_signals=[
                "disagreement",
                "分歧",
                "争议",
                "tool recruitment",
                "招募",
                "uncertainty",
                "hidden profile",
            ],
            anti_signals=["一致证据充分", "low uncertainty", "no extra budget"],
            recommended_structure={
                "trigger": "material_disagreement_on_required_output_or_evidence",
                "recruitment_policy": "add_specialist_or_tool_node_for_missing_capability",
                "resolution_gate": "aggregator_or_overlooker_compares_new_evidence_before_acceptance",
            },
            required_evidence=["disagreement_summary", "recruitment_reason", "resolution_evidence"],
            risk_notes=[
                "Disagreement alone is not a reason to expand; the Director should identify missing evidence or capability.",
                "Recruitment should be bounded by budget and compiled into the graph.",
            ],
            source_refs=[
                f"{source}: site/dart_leveraging_multi_agent_disagreement_for_tool_recruitment_in_multimodal_reasoning.html",
                f"{source}: site/hiddenbench_assessing_collective_reasoning_in_multi_agent_llms_via_hidden_profile_tasks.html",
                f"{source}: site/on_the_uncertainty_of_large_language_model_based_multi_agent_systems.html",
            ],
            evidence_level="direct",
            confidence_score=5.5,
            tags=["disagreement", "routing", "recruitment", "uncertainty"],
        ),
        ExperiencePattern(
            pattern_id="seed-governance-protocol-aware-agent-communication",
            pattern_type="governance",
            description="Use explicit interaction protocols for agent-to-agent communication when correctness depends on legal message order, role commitments, or protocol safety.",
            applicability_signals=[
                "protocol",
                "agent-to-agent",
                "a2a",
                "message order",
                "通信协议",
                "协议",
                "角色承诺",
            ],
            anti_signals=["自由聊天即可", "single agent", "no interaction contract"],
            recommended_structure={
                "protocol_spec": ["roles", "allowed_message_types", "turn_order", "termination_condition"],
                "runtime_check": "reject_messages_outside_protocol_contract",
                "audit_policy": "persist_protocol_events_as_trace_evidence",
            },
            required_evidence=["protocol_spec", "message_trace", "termination_event"],
            risk_notes=[
                "Protocol constraints help reproducibility but can slow exploratory collaboration.",
                "Protocol validation should be separate from model judgment.",
            ],
            source_refs=[
                f"{source}: site/ahoy_llms_enacting_multiagent_interaction_protocols.html",
                f"{source}: site/a2asecbench_a_protocol_aware_security_benchmark_for_agent_to_agent_multi_agent_systems.html",
                f"{source}: site/marble_coordination_modes_and_iteration_control.html",
            ],
            evidence_level="direct",
            confidence_score=5.5,
            tags=["protocol", "communication", "audit", "a2a"],
        ),
    ]
