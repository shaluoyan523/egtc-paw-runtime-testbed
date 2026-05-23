from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.models import NodeCapsule
from egtc_runtime_stagea.phaseb_models import (
    NodeInstantiation,
    PermissionGroundingReport,
    RepoPolicy,
    SandboxProfile,
    TaskDiagnosis,
    WorkflowBlueprint,
    WorkflowSkeleton,
    WorkflowSkeletonNode,
    structured,
)


def profile() -> dict[str, object]:
    return {
        "primary_task_family": "retrieval",
        "task_families": ["retrieval"],
        "verification_methods": ["external_fact_evidence", "source_citation"],
        "knowledge_sources": ["prompt", "network_or_local_corpus"],
        "predicted_failure_modes": ["ambiguity", "source_mismatch"],
        "estimated_difficulty": "medium",
        "estimated_budget": {
            "estimated_agents": 4,
            "estimated_tokens": 12000,
            "estimated_wall_time_sec": 420,
            "worth_multi_candidate": False,
        },
        "budget_gate": {
            "max_agents_before_replan": 4,
            "max_tokens_before_replan": 24000,
            "max_wall_time_sec_before_replan": 840,
        },
        "stop_condition": "Stop when cited source evidence supports the answer.",
        "escalation_condition": "Escalate when local sources are insufficient.",
        "cheaper_alternative": "Use one source-answer agent when prompt evidence is enough.",
    }


def basis(basis_id: str, target: str) -> dict[str, object]:
    return {
        "basis_id": basis_id,
        "source_refs": ["objective", "task_profile"],
        "matched_signals": ["negative alignment test"],
        "assumptions": ["This intentionally malformed plan should be rejected."],
        "invalidation_signals": ["Compiler accepts the mismatched plan."],
        "confidence": "high",
        "correction_target": target,
        "correction_action": "Reject and request a task-family-specific Director replan.",
    }


def node(node_id: str, phase: str, role: str, depends_on: list[str]) -> WorkflowSkeletonNode:
    return WorkflowSkeletonNode(
        node_id=node_id,
        phase=phase,
        role=role,
        goal=f"Malformed {node_id} node for retrieval task.",
        depends_on=depends_on,
        expected_outputs=["agent_report"],
        node_selection_principles={
            "stage_id": "stage-1",
            "selected_for": ["Negative demo keeps code-repair fallback nodes."],
            "role_principle": "This is intentionally mismatched with retrieval.",
            "dependency_principle": "Dependencies mirror the old code-repair fallback.",
            "parallelism_principle": "Old fallback shape is intentionally preserved.",
            "evidence_principle": "agent_report is intentionally too generic.",
            "decision_basis": basis(f"basis-node-{node_id}", f"workflow_skeleton.nodes[{node_id}]"),
        },
    )


def inst(skeleton_node_id: str, phase: str, write: bool) -> NodeInstantiation:
    node_capsule = NodeCapsule(
        node_id=f"model-{skeleton_node_id}",
        phase=phase,
        goal=f"Malformed instantiation for {skeleton_node_id}.",
        command=[],
        acceptance_criteria=["Overlooker acceptance must cite evidence_ref."],
        required_evidence=["log", "sandbox_events", "resource_report"],
        executor_kind="codex_cli",
        prompt="Malformed task-family alignment negative demo.",
    )
    return NodeInstantiation(
        node=node_capsule,
        skeleton_node_id=skeleton_node_id,
        permission_grounding=PermissionGroundingReport(
            node_id=node_capsule.node_id,
            sandbox_profile=SandboxProfile(
                network="none",
                allowed_read_paths=["."],
                allowed_write_paths=["."] if write else [],
                allowed_commands=[],
                justification="Negative demo intentionally grants mismatched write permission.",
            ),
            grounded_by=["repo_policy.allowed_read_paths", "repo_policy.allowed_write_paths"],
        ),
        instantiation_principles={
            "stage_id": "stage-1",
            "skeleton_node_id": skeleton_node_id,
            "executor_principle": "codex_cli is used for a minimal compiler negative demo.",
            "prompt_principle": "Prompt scope is intentionally malformed.",
            "permission_principle": "Permission scope intentionally mismatches retrieval.",
            "evidence_principle": "Evidence is intentionally generic.",
            "handoff_principle": "Handoff mirrors old code-repair fallback.",
            "decision_basis": basis(
                f"basis-instantiation-{skeleton_node_id}",
                f"node_instantiations[model-{skeleton_node_id}]",
            ),
        },
    )


def main() -> int:
    task_profile = profile()
    repo_policy = RepoPolicy(
        repo_root=str(ROOT),
        package_managers=[],
        test_commands=[],
        allowed_read_paths=["."],
        allowed_write_paths=["."],
        sensitive_paths=[".git", ".env"],
    )
    nodes = [
        node("explore-context", "exploration", "explorer", []),
        node("explore-tests", "exploration", "explorer", []),
        node("implement", "implementation", "worker", ["explore-context", "explore-tests"]),
        node("verify", "verification", "verifier", ["implement"]),
    ]
    blueprint = WorkflowBlueprint(
        blueprint_id="blueprint-task-family-alignment-negative",
        director_id="director-agent-v1",
        task_diagnosis=TaskDiagnosis(
            task_id="task-alignment-negative",
            objective="BrowseComp retrieval task that should not use a code repair fallback.",
            task_kind="retrieval",
            risk_level="medium",
            repo_touchpoints=["."],
            requires_code_change=False,
            requires_tests=False,
            task_profile=task_profile,
        ),
        repo_policy=repo_policy,
        workflow_skeleton=WorkflowSkeleton(
            skeleton_id="skeleton-alignment-negative",
            topology="parallel_explore_single_writer_verify_model_agents",
            nodes=nodes,
            edges=[
                ("explore-context", "implement"),
                ("explore-tests", "implement"),
                ("implement", "verify"),
            ],
            rationale="Negative demo intentionally uses old code-repair fallback for retrieval.",
            agent_allocation={"total_agents": 4, "roles": {"explorer": 2, "worker": 1, "verifier": 1}},
            alternative_skeletons=[
                {"name": "small", "estimated_agents": 1, "selected": False, "rejection_reason": "Negative demo."},
                {"name": "selected", "estimated_agents": 4, "selected": True, "rejection_reason": ""},
                {"name": "large", "estimated_agents": 8, "selected": False, "rejection_reason": "Negative demo."},
            ],
            scaling_policy={
                "scale_triggers": ["negative demo"],
                "scale_down_triggers": ["negative demo"],
                "max_planned_agents_for_current_task": 4,
                "expansion_strategy": ["negative demo"],
                "requires_replan_when": ["compiler detects task-family mismatch"],
                "observations_to_record": [
                    "candidate_count",
                    "comparison_count",
                    "validator_pass_rate",
                    "retry_count",
                    "replan_count",
                    "next_scaling_hint",
                ],
                "decision_basis": basis("basis-scaling-policy", "workflow_skeleton.scaling_policy"),
            },
            execution_estimate={
                "estimated_agents": 4,
                "estimated_tokens": 12000,
                "estimated_wall_time_sec": 420,
                "expected_success_probability": 0.1,
                "budget_gate": {"max_agents_before_replan": 4},
                "stop_condition": "Negative demo should not execute.",
                "escalation_condition": "Compiler should reject.",
                "cheaper_alternative": "Use retrieval-specific source verification.",
            },
            deliberation_trace=[
                "Negative demo pretends to compare alternatives.",
                "Negative demo intentionally selects a mismatched fallback.",
            ],
            linear_requirement_flow=[
                {
                    "stage_id": "stage-1",
                    "order": 1,
                    "name": "Malformed code repair flow",
                    "purpose": "Negative task-family alignment test.",
                    "inputs": ["objective"],
                    "outputs": ["diff"],
                    "risk_level": "medium",
                    "acceptance_evidence": ["compiler rejection"],
                    "decision_basis": basis("basis-stage-1", "linear_requirement_flow[stage-1]"),
                }
            ],
            stage_structure_decisions=[
                {
                    "stage_id": "stage-1",
                    "candidate_structures": [{"structure": "single_agent", "fit": "low", "reason": "Negative demo."}],
                    "selected_structure": "single_agent",
                    "selection_reason": "Negative demo.",
                    "anti_signals": ["retrieval task family"],
                    "decision_basis": basis("basis-structure-stage-1", "stage_structure_decisions[stage-1]"),
                }
            ],
            research_route_decisions=[
                {
                    "stage_id": "stage-1",
                    "research_needed": False,
                    "reason": "Negative demo intentionally omits retrieval research.",
                    "available_sources": ["none"],
                    "blocked_sources": ["external_web"],
                    "planned_queries_or_searches": ["none"],
                    "adopted_expert_route": "none",
                    "fallback_if_research_blocked": "compiler rejection",
                    "decision_basis": basis("basis-research-stage-1", "research_route_decisions[stage-1]"),
                }
            ],
            per_stage_agent_allocation=[
                {
                    "stage_id": "stage-1",
                    "agent_count": 4,
                    "count_reason": "Negative demo mirrors old fallback.",
                    "decision_basis": basis("basis-allocation-stage-1", "per_stage_agent_allocation[stage-1]"),
                    "agents": [
                        {
                            "role": item.role,
                            "task": item.goal,
                            "inputs": ["objective"],
                            "outputs": ["agent_report"],
                            "ownership_boundary": "Negative demo.",
                            "write_authority": "bounded write path" if item.node_id == "implement" else "none",
                            "handoff_target": item.node_id,
                            "decision_basis": basis(f"basis-agent-{item.node_id}", f"node:{item.node_id}"),
                        }
                        for item in nodes
                    ],
                }
            ],
            plan_derivation_trace=[
                "basis-structure-stage-1: producing nodes explore-context, explore-tests, implement, verify."
            ],
            draft_plan_review={
                "review_id": "draft-review-negative",
                "reviewed_draft_fields": [
                    "linear_requirement_flow",
                    "stage_structure_decisions",
                    "research_route_decisions",
                    "per_stage_agent_allocation",
                    "nodes",
                    "edges",
                    "node_instantiations",
                    "experience_pattern_ids",
                ],
                "structural_verdict": "pass",
                "structure_findings": [
                    {
                        "finding_id": "negative-finding",
                        "severity": "info",
                        "target": "workflow_skeleton.nodes",
                        "finding": "Negative demo intentionally misses retrieval-specific nodes.",
                        "recommendation": "Compiler should reject.",
                        "decision_basis": basis("basis-draft-review-negative", "workflow_skeleton.nodes"),
                    }
                ],
                "missing_capabilities": ["none"],
                "recommended_changes": [{"change_id": "negative-change", "change_type": "no_change"}],
                "applied_changes": [{"change_id": "negative-change", "applied": True}],
                "rejected_changes": [],
                "final_structure_summary": "Negative demo should be rejected by task-family alignment.",
            },
            experience_rationale=["Negative demo.", "Compiler alignment should reject."],
        ),
        task_profile=task_profile,
        work_assignment_plan=[
            {
                "node_id": item.node_id,
                "role": item.role,
                "stage_id": "stage-1",
                "agent_type": "generalist",
                "input_schema": {"required": ["objective"], "upstream_nodes": item.depends_on},
                "output_schema": {"required": ["agent_report"]},
                "ownership_boundary": "Negative demo.",
                "parallel_safe": False,
                "parallel_safety_reason": "Negative demo.",
                "failure_takeover": "Compiler rejection.",
                "selection_basis": "Negative demo.",
                "capability_needs": ["repo_read", "write_patch"] if item.node_id == "implement" else ["repo_read"],
            }
            for item in nodes
        ],
        permission_plan=[
            {
                "node_id": f"model-{item.node_id}",
                "skeleton_node_id": item.node_id,
                "permission_intents": ["read_repo", "write_patch"] if item.node_id == "implement" else ["read_repo"],
                "minimum_boundary": {
                    "read_paths": ["."],
                    "write_paths": ["."] if item.node_id == "implement" else [],
                    "allowed_commands": [],
                    "network": "none",
                    "secret_access": False,
                },
                "why_needed": "Negative demo.",
                "fallback_if_denied": "Compiler rejection.",
            }
            for item in nodes
        ],
        node_instantiations=[
            inst("explore-context", "exploration", False),
            inst("explore-tests", "exploration", False),
            inst("implement", "implementation", True),
            inst("verify", "verification", False),
        ],
        director_mode="codex",
        director_session_id="director-negative",
        director_skill_usage={
            "skill_name": "director-deliberative-planning",
            "skill_path": "skills/director-deliberative-planning/SKILL.md",
            "schema_path": "skills/director-deliberative-planning/references/planning_schema.md",
            "skill_sha256": "demo-skill-sha",
            "schema_sha256": "demo-schema-sha",
            "loaded": True,
            "applied_required_fields": [
                "task_profile",
                "work_assignment_plan",
                "permission_plan",
                "linear_requirement_flow",
                "stage_structure_decisions",
                "research_route_decisions",
                "per_stage_agent_allocation",
                "scaling_policy",
                "plan_derivation_trace",
                "node_selection_principles",
                "instantiation_principles",
                "draft_plan_review",
                "decision_basis",
            ],
        },
    )
    compiled = WorkflowCompiler().compile(blueprint)
    result = structured(compiled)
    print(json.dumps(result, indent=2, sort_keys=True))
    codes = {finding.code for finding in compiled.findings}
    expected = {
        "director_task_family_uses_code_repair_template",
        "director_task_family_unexpected_write_patch",
        "director_task_family_missing_nodes",
        "director_task_family_missing_roles",
        "director_task_family_missing_permission_intents",
    }
    return 0 if not compiled.accepted and expected.issubset(codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
