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
        "source_refs": ["objective", "repo_policy", "draft_plan"],
        "matched_signals": ["complex task", "requires tests"],
        "assumptions": ["The selected structure has enough coverage for the current task."],
        "invalidation_signals": ["Exploration discovers broader independent ownership."],
        "confidence": "medium",
        "correction_target": target,
        "correction_action": "Replan the target field and recompile the graph.",
    }


def main() -> int:
    repo_policy = RepoPolicy(
        repo_root=str(ROOT),
        package_managers=[],
        test_commands=[["python3", "-m", "compileall", "egtc_runtime_stagea"]],
        allowed_read_paths=["."],
        allowed_write_paths=["."],
        sensitive_paths=[".git", ".env"],
    )
    skeleton_node = WorkflowSkeletonNode(
        node_id="verify",
        phase="verification",
        role="verifier",
        goal="Verify the Director-selected plan with read-only evidence.",
        expected_outputs=["test_report"],
        node_selection_principles={
            "stage_id": "stage-1",
            "selected_for": ["The final graph needs an explicit verification gate."],
            "role_principle": "Verifier is selected because this node must inspect evidence without writing.",
            "dependency_principle": "No predecessor is needed in this single-node compiler demo.",
            "parallelism_principle": "This is a serial terminal gate in the minimal graph.",
            "evidence_principle": "test_report is sufficient for compiler-level verification.",
            "decision_basis": basis("basis-node-verify", "workflow_skeleton.nodes[verify]"),
        },
    )
    node = NodeCapsule(
        node_id="phasef-verify",
        phase="verification",
        goal=skeleton_node.goal,
        command=[],
        acceptance_criteria=["Overlooker acceptance must cite evidence_ref."],
        executor_kind="codex_cli",
        required_evidence=["log", "sandbox_events", "resource_report"],
        prompt="Verify the plan with read-only evidence.",
    )
    blueprint = WorkflowBlueprint(
        blueprint_id="blueprint-director-draft-review-positive",
        director_id="director-agent-v1",
        task_diagnosis=TaskDiagnosis(
            task_id="task-draft-review-positive",
            objective="Complex Codex Director task with draft plan self-review.",
            task_kind="implementation",
            risk_level="medium",
            repo_touchpoints=["."],
            requires_code_change=True,
            requires_tests=True,
        ),
        repo_policy=repo_policy,
        workflow_skeleton=WorkflowSkeleton(
            skeleton_id="skeleton-draft-review-positive",
            topology="review_gate",
            nodes=[skeleton_node],
            edges=[],
            rationale="Positive compiler demo for Director draft plan self-review.",
            agent_allocation={"total_agents": 1, "roles": {"verifier": 1}},
            alternative_skeletons=[
                {"name": "small", "estimated_agents": 1, "selected": False, "rejection_reason": "Insufficient review trace."},
                {"name": "selected", "estimated_agents": 1, "selected": True, "rejection_reason": ""},
                {"name": "large", "estimated_agents": 3, "selected": False, "rejection_reason": "Unneeded for compiler demo."},
            ],
            scaling_policy={
                "scale_triggers": ["multiple independent write surfaces"],
                "max_planned_agents_for_current_task": 1,
                "expansion_strategy": ["add explorer and implementer stages"],
                "requires_replan_when": ["verification finds missing implementation evidence"],
            },
            deliberation_trace=[
                "Compared small, selected, and larger-scalable structures.",
                "Selected a minimal review gate because this is a compiler demo.",
            ],
            linear_requirement_flow=[
                {
                    "stage_id": "stage-1",
                    "order": 1,
                    "name": "Verify structure",
                    "purpose": "Ensure the Director output has a review gate.",
                    "inputs": ["objective"],
                    "outputs": ["test_report"],
                    "risk_level": "medium",
                    "acceptance_evidence": ["test_report"],
                    "decision_basis": basis("basis-stage-1", "linear_requirement_flow[stage-1]"),
                }
            ],
            stage_structure_decisions=[
                {
                    "stage_id": "stage-1",
                    "candidate_structures": [
                        {"structure": "review_gate", "fit": "high", "reason": "The demo validates review structure."}
                    ],
                    "selected_structure": "review_gate",
                    "selection_reason": "A review gate directly tests the self-review requirement.",
                    "anti_signals": ["Task requires implementation edits."],
                    "decision_basis": basis("basis-structure-stage-1", "stage_structure_decisions[stage-1]"),
                }
            ],
            research_route_decisions=[
                {
                    "stage_id": "stage-1",
                    "research_needed": False,
                    "reason": "Compiler demo uses local schema only.",
                    "available_sources": ["planning_schema.md"],
                    "blocked_sources": ["external_web"],
                    "planned_queries_or_searches": ["read local schema"],
                    "adopted_expert_route": "Use director-deliberative-planning schema.",
                    "fallback_if_research_blocked": "Fail compiler validation.",
                    "decision_basis": basis("basis-research-stage-1", "research_route_decisions[stage-1]"),
                }
            ],
            per_stage_agent_allocation=[
                {
                    "stage_id": "stage-1",
                    "agent_count": 1,
                    "count_reason": "One verifier is enough for this minimal positive demo.",
                    "decision_basis": basis("basis-allocation-stage-1", "per_stage_agent_allocation[stage-1]"),
                    "agents": [
                        {
                            "role": "verifier",
                            "task": "Verify structure.",
                            "inputs": ["objective"],
                            "outputs": ["test_report"],
                            "ownership_boundary": "Read-only schema validation.",
                            "write_authority": "none",
                            "handoff_target": "complete",
                            "decision_basis": basis("basis-agent-stage-1-verifier", "node:verify"),
                        }
                    ],
                }
            ],
            plan_derivation_trace=[
                "basis-structure-stage-1: stage-1 selected review_gate, producing final node verify."
            ],
            draft_plan_review={
                "review_id": "draft-review-1",
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
                        "finding_id": "draft-finding-1",
                        "severity": "info",
                        "target": "workflow_skeleton.nodes[verify]",
                        "finding": "The draft has a terminal read-only verification gate and no unsafe edge.",
                        "recommendation": "Keep the single verifier for this compiler demo.",
                        "decision_basis": basis("basis-draft-review-1", "workflow_skeleton.nodes[verify]"),
                    }
                ],
                "missing_capabilities": ["none"],
                "recommended_changes": [
                    {
                        "change_id": "draft-change-1",
                        "change_type": "no_change",
                        "target": "workflow_skeleton.nodes[verify]",
                        "rationale": "The draft structure is sufficient for this positive compiler demo.",
                    }
                ],
                "applied_changes": [
                    {
                        "change_id": "draft-change-1",
                        "applied": True,
                        "final_targets": ["workflow_skeleton.nodes[verify]"],
                        "result": "Final workflow keeps the reviewed verifier node.",
                    }
                ],
                "rejected_changes": [],
                "final_structure_summary": "The final graph has one read-only verifier and complete review evidence.",
            },
            experience_rationale=["No experience pattern is required for this compiler demo.", "The self-review schema is local."],
        ),
        node_instantiations=[
            NodeInstantiation(
                node=node,
                skeleton_node_id="verify",
                permission_grounding=PermissionGroundingReport(
                    node_id=node.node_id,
                    sandbox_profile=SandboxProfile(
                        network="none",
                        allowed_read_paths=["."],
                        allowed_write_paths=[],
                        allowed_commands=[],
                        justification="Verification is read-only.",
                    ),
                    grounded_by=["repo_policy.allowed_read_paths"],
                ),
                instantiation_principles={
                    "stage_id": "stage-1",
                    "skeleton_node_id": "verify",
                    "executor_principle": "codex_cli is selected because the runtime workers are agents.",
                    "prompt_principle": "Prompt scope is limited to read-only verification.",
                    "permission_principle": "Only read access is grounded by repo policy.",
                    "evidence_principle": "log, sandbox_events, and resource_report show execution evidence.",
                    "handoff_principle": "The verifier is terminal in this compiler demo.",
                    "decision_basis": basis("basis-instantiation-verify", "node_instantiations[phasef-verify]"),
                },
            )
        ],
        director_mode="codex",
        director_session_id="director-positive",
        director_skill_usage={
            "skill_name": "director-deliberative-planning",
            "skill_path": "skills/director-deliberative-planning/SKILL.md",
            "schema_path": "skills/director-deliberative-planning/references/planning_schema.md",
            "skill_sha256": "demo-skill-sha",
            "schema_sha256": "demo-schema-sha",
            "loaded": True,
            "applied_required_fields": [
                "linear_requirement_flow",
                "stage_structure_decisions",
                "research_route_decisions",
                "per_stage_agent_allocation",
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
    return 0 if compiled.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
