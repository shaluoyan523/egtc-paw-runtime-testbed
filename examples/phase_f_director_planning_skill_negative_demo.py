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
        node_id="implement",
        phase="implementation",
        role="worker",
        goal="Implement without staged Director planning.",
    )
    node = NodeCapsule(
        node_id="phasef-implement",
        phase="implementation",
        goal=skeleton_node.goal,
        command=[],
        acceptance_criteria=["Worker may only submit results."],
        executor_kind="codex_cli",
    )
    blueprint = WorkflowBlueprint(
        blueprint_id="blueprint-missing-director-planning-skill",
        director_id="director-agent-v1",
        task_diagnosis=TaskDiagnosis(
            task_id="task-missing-planning",
            objective="Complex Codex Director task without staged planning artifacts.",
            task_kind="implementation",
            risk_level="medium",
            repo_touchpoints=["."],
            requires_code_change=True,
            requires_tests=True,
        ),
        repo_policy=repo_policy,
        workflow_skeleton=WorkflowSkeleton(
            skeleton_id="skeleton-missing-planning",
            topology="direct",
            nodes=[skeleton_node],
            edges=[],
            rationale="This intentionally skips deliberative planning skill outputs.",
            agent_allocation={"total_agents": 1, "roles": {"worker": 1}},
            alternative_skeletons=[
                {"name": "small", "estimated_agents": 1, "selected": False},
                {"name": "selected", "estimated_agents": 1, "selected": True},
                {"name": "large", "estimated_agents": 3, "selected": False},
            ],
            scaling_policy={
                "scale_triggers": ["broader write surface"],
                "max_planned_agents_for_current_task": 1,
                "expansion_strategy": ["replan"],
                "requires_replan_when": ["missing evidence"],
            },
            deliberation_trace=["direct plan", "no staged planning"],
            linear_requirement_flow=[
                {
                    "stage_id": "stage-1",
                    "order": 1,
                    "name": "Direct implementation",
                    "purpose": "Negative test with missing basis.",
                    "inputs": ["objective"],
                    "outputs": ["diff"],
                    "risk_level": "medium",
                    "acceptance_evidence": ["diff"],
                }
            ],
            stage_structure_decisions=[
                {
                    "stage_id": "stage-1",
                    "candidate_structures": [
                        {
                            "structure": "single_agent",
                            "fit": "high",
                            "reason": "Negative test.",
                        }
                    ],
                    "selected_structure": "single_agent",
                    "selection_reason": "Negative test.",
                    "anti_signals": ["parallel surface appears"],
                }
            ],
            research_route_decisions=[
                {
                    "stage_id": "stage-1",
                    "research_needed": False,
                    "reason": "Negative test.",
                    "available_sources": ["objective"],
                    "blocked_sources": ["external_web"],
                    "planned_queries_or_searches": ["none"],
                    "adopted_expert_route": "none",
                    "fallback_if_research_blocked": "replan",
                }
            ],
            per_stage_agent_allocation=[
                {
                    "stage_id": "stage-1",
                    "agent_count": 1,
                    "count_reason": "Negative test.",
                    "agents": [
                        {
                            "role": "worker",
                            "task": "Implement directly.",
                            "inputs": ["objective"],
                            "outputs": ["diff"],
                            "ownership_boundary": "repo",
                            "write_authority": "bounded write path",
                            "handoff_target": "complete",
                        }
                    ],
                }
            ],
            plan_derivation_trace=[
                "stage-1 selected single_agent, producing final node implement."
            ],
            experience_rationale=["No pattern selected.", "This is a negative test."],
        ),
        node_instantiations=[
            NodeInstantiation(
                node=node,
                skeleton_node_id="implement",
                permission_grounding=PermissionGroundingReport(
                    node_id=node.node_id,
                    sandbox_profile=SandboxProfile(
                        network="none",
                        allowed_read_paths=["."],
                        allowed_write_paths=["."],
                        allowed_commands=[],
                        justification="Negative test grounding.",
                    ),
                    grounded_by=["repo_policy.allowed_write_paths"],
                ),
            )
        ],
        director_mode="codex",
        director_session_id="director-negative",
    )
    compiled = WorkflowCompiler().compile(blueprint)
    finding_codes = [finding.code for finding in compiled.findings]
    print(json.dumps(structured(compiled), indent=2, sort_keys=True))
    expected = {
        "director_missing_skill_usage",
        "director_missing_decision_basis",
        "director_missing_draft_plan_review",
        "director_node_missing_selection_principles",
        "director_node_missing_instantiation_principles",
    }
    return 0 if not compiled.accepted and expected.issubset(finding_codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
