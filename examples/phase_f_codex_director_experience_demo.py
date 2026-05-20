from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.director import DirectorAgentV1
from egtc_runtime_stagea.experience import ExperienceLibrary
from egtc_runtime_stagea.phaseb_models import structured
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer


def main() -> int:
    runtime_root = ROOT / "phasef_codex_director_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    workspace = runtime_root / "director"
    library = ExperienceLibrary(runtime_root / "experience")
    library.seed_defaults()
    objective = (
        "实现一个SWE复杂项目修改并进行多agent测试，需要Director根据经验库选择agent数量、"
        "并行explorer、worker、overlooker校验、retry预算和artifact evidence。"
    )
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    director = DirectorAgentV1(experience_library=library)
    blueprint = director.plan_with_codex_director(
        objective,
        repo_policy,
        workspace,
        timeout_sec=600,
    )
    compiled = WorkflowCompiler().compile(blueprint, experience_library=library)
    output = {
        "compiled": structured(compiled),
        "director_mode": blueprint.director_mode,
        "director_session_id": blueprint.director_session_id,
        "director_skill_usage": blueprint.director_skill_usage,
        "topology": blueprint.workflow_skeleton.topology,
        "agent_allocation": blueprint.workflow_skeleton.agent_allocation,
        "alternative_skeletons": blueprint.workflow_skeleton.alternative_skeletons,
        "scaling_policy": blueprint.workflow_skeleton.scaling_policy,
        "deliberation_trace": blueprint.workflow_skeleton.deliberation_trace,
        "linear_requirement_flow": blueprint.workflow_skeleton.linear_requirement_flow,
        "stage_structure_decisions": blueprint.workflow_skeleton.stage_structure_decisions,
        "research_route_decisions": blueprint.workflow_skeleton.research_route_decisions,
        "per_stage_agent_allocation": blueprint.workflow_skeleton.per_stage_agent_allocation,
        "plan_derivation_trace": blueprint.workflow_skeleton.plan_derivation_trace,
        "draft_plan_review": blueprint.workflow_skeleton.draft_plan_review,
        "experience_rationale": blueprint.workflow_skeleton.experience_rationale,
        "experience_pattern_ids": blueprint.experience_pattern_ids,
        "node_count": len(blueprint.workflow_skeleton.nodes),
        "node_roles": {
            node.node_id: node.role for node in blueprint.workflow_skeleton.nodes
        },
        "node_selection_principles": {
            node.node_id: node.node_selection_principles
            for node in blueprint.workflow_skeleton.nodes
        },
        "instantiation_executor_kinds": {
            inst.skeleton_node_id: inst.node.executor_kind
            for inst in blueprint.node_instantiations
        },
        "instantiation_principles": {
            inst.node.node_id: inst.instantiation_principles
            for inst in blueprint.node_instantiations
        },
        "workspace": str(workspace),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    role_values = set(output["node_roles"].values())
    total_agents = int(output["agent_allocation"].get("total_agents", 0))
    selected_alternatives = [
        item for item in output["alternative_skeletons"] if item.get("selected")
    ]
    stage_agent_total = sum(
        item.get("agent_count", 0)
        for item in output["per_stage_agent_allocation"]
        if isinstance(item, dict)
    )
    trace_text = "\n".join(output["plan_derivation_trace"])
    basis_keys = {
        "basis_id",
        "source_refs",
        "matched_signals",
        "assumptions",
        "invalidation_signals",
        "confidence",
        "correction_target",
        "correction_action",
    }

    def has_basis(item: object) -> bool:
        if not isinstance(item, dict):
            return False
        basis = item.get("decision_basis")
        return isinstance(basis, dict) and basis_keys.issubset(basis)

    allocation_agents = [
        agent
        for allocation in output["per_stage_agent_allocation"]
        if isinstance(allocation, dict)
        for agent in allocation.get("agents", [])
        if isinstance(agent, dict)
    ]
    node_principles = list(output["node_selection_principles"].values())
    instantiation_principles = list(output["instantiation_principles"].values())
    draft_review = output["draft_plan_review"]
    reviewed_fields = set(draft_review.get("reviewed_draft_fields", []))
    required_reviewed_fields = {
        "linear_requirement_flow",
        "stage_structure_decisions",
        "research_route_decisions",
        "per_stage_agent_allocation",
        "nodes",
        "edges",
        "node_instantiations",
        "experience_pattern_ids",
    }
    review_findings = draft_review.get("structure_findings", [])
    return 0 if (
        compiled.accepted
        and output["director_mode"] == "codex"
        and str(output["director_session_id"] or "").startswith("director-")
        and output["director_skill_usage"].get("skill_name") == "director-deliberative-planning"
        and output["director_skill_usage"].get("loaded") is True
        and output["director_skill_usage"].get("skill_sha256")
        and output["director_skill_usage"].get("schema_sha256")
        and output["experience_pattern_ids"]
        and output["node_count"] >= 3
        and total_agents == output["node_count"]
        and len(output["alternative_skeletons"]) >= 3
        and len(selected_alternatives) == 1
        and len(output["deliberation_trace"]) >= 2
        and len(output["linear_requirement_flow"]) >= 3
        and len(output["stage_structure_decisions"]) >= len(output["linear_requirement_flow"])
        and len(output["research_route_decisions"]) >= len(output["linear_requirement_flow"])
        and stage_agent_total == total_agents
        and all(node_id in trace_text for node_id in output["node_roles"])
        and all(has_basis(item) for item in output["linear_requirement_flow"])
        and all(has_basis(item) for item in output["stage_structure_decisions"])
        and all(has_basis(item) for item in output["research_route_decisions"])
        and all(has_basis(item) for item in output["per_stage_agent_allocation"])
        and all(has_basis(item) for item in allocation_agents)
        and required_reviewed_fields.issubset(reviewed_fields)
        and draft_review.get("structural_verdict") in {"pass", "revise_before_final"}
        and review_findings
        and all(has_basis(item) for item in review_findings if isinstance(item, dict))
        and draft_review.get("recommended_changes")
        and draft_review.get("applied_changes")
        and isinstance(draft_review.get("rejected_changes"), list)
        and draft_review.get("final_structure_summary")
        and len(node_principles) == output["node_count"]
        and all(has_basis(item) for item in node_principles)
        and all(item.get("selected_for") for item in node_principles if isinstance(item, dict))
        and all(item.get("role_principle") for item in node_principles if isinstance(item, dict))
        and all(item.get("dependency_principle") for item in node_principles if isinstance(item, dict))
        and all(item.get("parallelism_principle") for item in node_principles if isinstance(item, dict))
        and all(item.get("evidence_principle") for item in node_principles if isinstance(item, dict))
        and len(instantiation_principles) == output["node_count"]
        and all(has_basis(item) for item in instantiation_principles)
        and all(item.get("executor_principle") for item in instantiation_principles if isinstance(item, dict))
        and all(item.get("prompt_principle") for item in instantiation_principles if isinstance(item, dict))
        and all(item.get("permission_principle") for item in instantiation_principles if isinstance(item, dict))
        and all(item.get("handoff_principle") for item in instantiation_principles if isinstance(item, dict))
        and output["scaling_policy"].get("scale_triggers")
        and output["scaling_policy"].get("expansion_strategy")
        and len(output["experience_rationale"]) >= 2
        and "explorer" in role_values
        and ("worker" in role_values or "coder" in role_values)
        and any(role in role_values for role in {"verifier", "worker"})
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
