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


def basis(basis_id: str, target: str) -> dict[str, object]:
    return {
        "basis_id": basis_id,
        "source_refs": ["objective", "task_profile"],
        "matched_signals": ["negative grounding demo"],
        "assumptions": ["The compiler should reject ungrounded Director choices."],
        "invalidation_signals": ["Compiler accepts an ungrounded permission or unmapped node."],
        "confidence": "high",
        "correction_target": target,
        "correction_action": "Require Director to replan with grounded capability and selected alternative evidence.",
    }


def skeleton_node(node_id: str, depends_on: list[str]) -> WorkflowSkeletonNode:
    return WorkflowSkeletonNode(
        node_id=node_id,
        phase="analysis" if not depends_on else "verification",
        role="analyst" if not depends_on else "verifier",
        goal=f"Negative demo node {node_id}.",
        depends_on=depends_on,
        expected_outputs=["agent_report"],
        node_selection_principles={
            "stage_id": "stage-1" if not depends_on else "stage-2",
            "selected_for": ["Negative demo keeps a structurally valid node record."],
            "role_principle": "Role is present so the failure is about choice grounding.",
            "dependency_principle": "Dependencies are explicit.",
            "parallelism_principle": "The demo is serial.",
            "evidence_principle": "agent_report is the handoff artifact.",
            "decision_basis": basis(f"basis-node-{node_id}", f"workflow_skeleton.nodes[{node_id}]"),
        },
    )


def instantiation(skeleton_node_id: str, phase: str) -> NodeInstantiation:
    node = NodeCapsule(
        node_id=f"model-{skeleton_node_id}",
        phase=phase,
        goal=f"Negative demo instantiation for {skeleton_node_id}.",
        command=[],
        acceptance_criteria=["Overlooker acceptance must cite evidence_ref."],
        required_evidence=["log", "sandbox_events", "resource_report"],
        executor_kind="model_agent",
        prompt="Negative Director choice grounding demo.",
        model_provider="deterministic",
        model="deterministic-director",
    )
    return NodeInstantiation(
        node=node,
        skeleton_node_id=skeleton_node_id,
        permission_grounding=PermissionGroundingReport(
            node_id=node.node_id,
            sandbox_profile=SandboxProfile(
                network="none",
                allowed_read_paths=["."],
                allowed_write_paths=[],
                allowed_commands=[],
                justification="Negative demo permission grounding.",
            ),
            grounded_by=["repo_policy.allowed_read_paths"],
        ),
        instantiation_principles={
            "stage_id": "stage-1" if phase == "analysis" else "stage-2",
            "skeleton_node_id": skeleton_node_id,
            "executor_principle": "model_agent is used for a provider-agnostic compiler demo.",
            "prompt_principle": "Prompt is intentionally minimal.",
            "permission_principle": "Permission is intentionally checked by compiler choice grounding.",
            "evidence_principle": "Evidence contract is present.",
            "handoff_principle": "Handoff follows skeleton dependencies.",
            "decision_basis": basis(
                f"basis-instantiation-{skeleton_node_id}",
                f"node_instantiations[model-{skeleton_node_id}]",
            ),
        },
    )


def main() -> int:
    profile = {
        "primary_task_family": "retrieval",
        "task_families": ["retrieval"],
        "verification_methods": ["external_fact_evidence", "source_citation"],
        "knowledge_sources": ["prompt", "network_or_local_corpus"],
        "predicted_failure_modes": ["ambiguity", "source_mismatch"],
        "estimated_difficulty": "medium",
        "estimated_budget": {
            "estimated_agents": 2,
            "estimated_tokens": 8000,
            "estimated_wall_time_sec": 300,
            "worth_multi_candidate": False,
        },
        "budget_gate": {
            "max_agents_before_replan": 2,
            "max_tokens_before_replan": 16000,
            "max_wall_time_sec_before_replan": 600,
        },
        "stop_condition": "Stop when source evidence supports the answer.",
        "escalation_condition": "Escalate when evidence is unavailable.",
        "cheaper_alternative": "Use one evidence-answer agent when prompt evidence is enough.",
    }
    repo_policy = RepoPolicy(
        repo_root=str(ROOT),
        package_managers=[],
        test_commands=[],
        allowed_read_paths=["."],
        allowed_write_paths=["."],
        sensitive_paths=[".git", ".env"],
    )
    nodes = [
        skeleton_node("task-framing", []),
        skeleton_node("outcome-verification", ["task-framing"]),
    ]
    blueprint = WorkflowBlueprint(
        blueprint_id="blueprint-director-choice-grounding-negative",
        director_id="director-agent-v1",
        task_diagnosis=TaskDiagnosis(
            task_id="task-choice-grounding-negative",
            objective="BrowseComp retrieval task with intentionally ungrounded permission and alternative mapping.",
            task_kind="retrieval",
            risk_level="medium",
            repo_touchpoints=["."],
            requires_code_change=False,
            requires_tests=False,
            task_profile=profile,
        ),
        repo_policy=repo_policy,
        workflow_skeleton=WorkflowSkeleton(
            skeleton_id="skeleton-choice-grounding-negative",
            topology="profile_driven_capability_pipeline",
            nodes=nodes,
            edges=[("task-framing", "outcome-verification")],
            rationale="Negative demo should fail generic Director choice grounding.",
            agent_allocation={"total_agents": 2, "roles": {"analyst": 1, "verifier": 1}},
            alternative_skeletons=[
                {"name": "small", "estimated_agents": 1, "selected": False, "stage_mapping": {"all": ["task-framing"]}},
                {
                    "name": "selected",
                    "estimated_agents": 2,
                    "selected": True,
                    "stage_mapping": {"stage-1": ["task-framing"]},
                },
                {"name": "large", "estimated_agents": 8, "selected": False, "stage_mapping": {"stage-1": ["pool"]}},
            ],
            scaling_policy={
                "scale_triggers": ["evidence remains missing"],
                "scale_down_triggers": ["prompt evidence is sufficient"],
                "max_planned_agents_for_current_task": 2,
                "expansion_strategy": ["add grounded evidence worker after replan"],
                "requires_replan_when": ["compiler detects ungrounded permission intent"],
                "observations_to_record": [
                    "candidate_count",
                    "comparison_count",
                    "validator_pass_rate",
                    "retry_count",
                    "replan_count",
                    "next_scaling_hint",
                ],
                "decision_basis": basis("basis-scaling", "workflow_skeleton.scaling_policy"),
            },
            execution_estimate={
                "estimated_agents": 2,
                "estimated_tokens": 8000,
                "estimated_wall_time_sec": 300,
                "expected_success_probability": 0.2,
                "budget_gate": {"max_agents_before_replan": 2},
                "stop_condition": "Negative demo should not execute.",
                "escalation_condition": "Compiler should reject.",
                "cheaper_alternative": "Replan with grounded evidence permissions.",
            },
            deliberation_trace=[
                "Negative demo compares alternatives but maps the selected alternative incompletely.",
                "Negative demo requests a risky permission without capability grounding.",
            ],
            linear_requirement_flow=[
                {
                    "stage_id": "stage-1",
                    "order": 1,
                    "name": "Frame task",
                    "purpose": "Frame the retrieval task.",
                    "inputs": ["objective"],
                    "outputs": ["task_frame"],
                    "risk_level": "medium",
                    "acceptance_evidence": ["task_frame"],
                    "decision_basis": basis("basis-stage-1", "linear_requirement_flow[stage-1]"),
                },
                {
                    "stage_id": "stage-2",
                    "order": 2,
                    "name": "Verify answer",
                    "purpose": "Verify the answer.",
                    "inputs": ["task_frame"],
                    "outputs": ["verification_report"],
                    "risk_level": "medium",
                    "acceptance_evidence": ["verification_report"],
                    "decision_basis": basis("basis-stage-2", "linear_requirement_flow[stage-2]"),
                },
            ],
            stage_structure_decisions=[
                {
                    "stage_id": "stage-1",
                    "candidate_structures": [{"structure": "single_agent", "fit": "medium", "reason": "Demo."}],
                    "selected_structure": "single_agent",
                    "selection_reason": "Demo.",
                    "anti_signals": ["permission mismatch"],
                    "decision_basis": basis("basis-structure-1", "stage_structure_decisions[stage-1]"),
                },
                {
                    "stage_id": "stage-2",
                    "candidate_structures": [{"structure": "review_gate", "fit": "medium", "reason": "Demo."}],
                    "selected_structure": "review_gate",
                    "selection_reason": "Demo.",
                    "anti_signals": ["permission mismatch"],
                    "decision_basis": basis("basis-structure-2", "stage_structure_decisions[stage-2]"),
                },
            ],
            research_route_decisions=[
                {
                    "stage_id": "stage-1",
                    "research_needed": False,
                    "reason": "Negative demo intentionally does not ground dataset_read.",
                    "available_sources": ["prompt"],
                    "blocked_sources": ["external_web"],
                    "planned_queries_or_searches": [],
                    "adopted_expert_route": "none",
                    "fallback_if_research_blocked": "compiler rejection",
                    "decision_basis": basis("basis-research-1", "research_route_decisions[stage-1]"),
                },
                {
                    "stage_id": "stage-2",
                    "research_needed": False,
                    "reason": "Negative demo intentionally does not ground dataset_read.",
                    "available_sources": ["prompt"],
                    "blocked_sources": ["external_web"],
                    "planned_queries_or_searches": [],
                    "adopted_expert_route": "none",
                    "fallback_if_research_blocked": "compiler rejection",
                    "decision_basis": basis("basis-research-2", "research_route_decisions[stage-2]"),
                },
            ],
            per_stage_agent_allocation=[
                {
                    "stage_id": "stage-1",
                    "agent_count": 1,
                    "count_reason": "One framing agent.",
                    "decision_basis": basis("basis-allocation-1", "per_stage_agent_allocation[stage-1]"),
                    "agents": [
                        {
                            "role": "analyst",
                            "task": "Frame task.",
                            "inputs": ["objective"],
                            "outputs": ["task_frame"],
                            "ownership_boundary": "Own framing only.",
                            "write_authority": "none",
                            "handoff_target": "task-framing",
                            "decision_basis": basis("basis-agent-1", "node:task-framing"),
                        }
                    ],
                },
                {
                    "stage_id": "stage-2",
                    "agent_count": 1,
                    "count_reason": "One verifier.",
                    "decision_basis": basis("basis-allocation-2", "per_stage_agent_allocation[stage-2]"),
                    "agents": [
                        {
                            "role": "verifier",
                            "task": "Verify answer.",
                            "inputs": ["task_frame"],
                            "outputs": ["verification_report"],
                            "ownership_boundary": "Own verification only.",
                            "write_authority": "none",
                            "handoff_target": "outcome-verification",
                            "decision_basis": basis("basis-agent-2", "node:outcome-verification"),
                        }
                    ],
                },
            ],
            plan_derivation_trace=[
                "basis-structure-stage-1: producing nodes task-framing.",
                "basis-structure-stage-2: producing nodes outcome-verification.",
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
                        "finding": "Negative demo intentionally leaves selected alternative and permission grounding inconsistent.",
                        "recommendation": "Compiler should reject.",
                        "decision_basis": basis("basis-review", "draft_plan_review.structure_findings[0]"),
                    }
                ],
                "missing_capabilities": ["dataset_read"],
                "recommended_changes": [{"change_id": "negative-change", "change_type": "replan"}],
                "applied_changes": [{"change_id": "negative-change", "applied": False}],
                "rejected_changes": [],
                "final_structure_summary": "Negative demo should be rejected by choice grounding.",
            },
            experience_rationale=["Negative demo.", "Compiler choice grounding should reject."],
        ),
        task_profile=profile,
        work_assignment_plan=[
            {
                "node_id": "task-framing",
                "role": "analyst",
                "stage_id": "stage-1",
                "agent_type": "specialist",
                "input_schema": {"required": ["objective"], "upstream_nodes": []},
                "output_schema": {"required": ["task_frame"]},
                "ownership_boundary": "Own framing only.",
                "parallel_safe": True,
                "parallel_safety_reason": "No dependencies.",
                "failure_takeover": "Overlooker requests Director replan.",
                "selection_basis": "Frame the task.",
                "capability_needs": ["analysis"],
            },
            {
                "node_id": "outcome-verification",
                "role": "verifier",
                "stage_id": "stage-2",
                "agent_type": "verification",
                "input_schema": {"required": ["task_frame"], "upstream_nodes": ["task-framing"]},
                "output_schema": {"required": ["verification_report"]},
                "ownership_boundary": "Own verification only.",
                "parallel_safe": False,
                "parallel_safety_reason": "Depends on framing.",
                "failure_takeover": "Overlooker requests Director replan.",
                "selection_basis": "Verify the answer.",
                "capability_needs": ["verification"],
            },
        ],
        permission_plan=[
            {
                "node_id": "model-task-framing",
                "skeleton_node_id": "task-framing",
                "permission_intents": ["read_repo"],
                "minimum_boundary": {
                    "read_paths": ["."],
                    "write_paths": [],
                    "allowed_commands": [],
                    "network": "none",
                    "secret_access": False,
                },
                "why_needed": "Read-only framing.",
                "fallback_if_denied": "Use prompt-only framing.",
            },
            {
                "node_id": "model-outcome-verification",
                "skeleton_node_id": "outcome-verification",
                "permission_intents": ["read_repo", "finance_calculator"],
                "minimum_boundary": {
                    "read_paths": ["."],
                    "write_paths": [],
                    "allowed_commands": [],
                    "network": "none",
                    "secret_access": False,
                },
                "why_needed": "Negative demo.",
                "fallback_if_denied": "Compiler rejection.",
            },
        ],
        node_instantiations=[
            instantiation("task-framing", "analysis"),
            instantiation("outcome-verification", "verification"),
        ],
        director_mode="codex",
        director_session_id="director-choice-grounding-negative",
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
        "director_selected_alternative_missing_nodes",
        "director_permission_intent_not_grounded_by_assignment",
    }
    return 0 if not compiled.accepted and expected.issubset(codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
