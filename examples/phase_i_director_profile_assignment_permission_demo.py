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
from egtc_runtime_stagea.repo_policy import RepoPolicyInferencer


CASES = [
    (
        "browsecomp",
        "BrowseComp style retrieval task: find externally evidenced answer with source citations.",
        "retrieval",
        {"external_fact_evidence"},
        {"network_or_local_corpus"},
        {"research-sources", "answer-synthesis", "source-verify"},
        set(),
        {"dataset_read", "browser"},
        False,
    ),
    (
        "finance",
        "Finance-Agent task: compute portfolio return using formula and calculator evidence.",
        "finance_calculation",
        {"formula_check"},
        {"network_or_local_corpus"},
        {"collect-financial-inputs", "calculate-answer", "formula-verify"},
        set(),
        {"finance_calculator"},
        False,
    ),
    (
        "swe",
        "SWE-bench complex code repair task: inspect repo, patch bug, and run tests.",
        "code_repair",
        {"unit_tests"},
        {"repo", "tool_execution"},
        {"explore-context", "explore-tests", "implement", "verify"},
        set(),
        {"write_patch", "run_tests"},
        False,
    ),
    (
        "terminal",
        "Terminal-Bench task: use shell/container checkpoint to complete environment work.",
        "terminal_execution",
        {"container_test"},
        {"tool_execution"},
        {"plan-terminal-actions", "execute-checkpoint", "verify-checkpoint"},
        set(),
        {"run_shell", "container_exec"},
        False,
    ),
    (
        "plancraft",
        "PlanCraft planning state transition task with possible ambiguity.",
        "planning_state_transition",
        {"state_transition_check"},
        {"prompt"},
        {"ambiguity-check", "state-plan", "transition-verify"},
        set(),
        {"read_repo"},
        False,
    ),
    (
        "opendeepthink",
        "OpenDeepThink hard contest reasoning task with judge and pairwise candidates.",
        "contest_reasoning",
        {"judge"},
        {"tool_execution"},
        {"candidate-generate", "candidate-judge", "solution-synthesis", "final-verify"},
        set(),
        {"run_shell"},
        True,
    ),
    (
        "analysis",
        "Prompt-local summary task: compare two provided options using only prompt facts.",
        "analysis",
        {"human_judgment"},
        {"prompt"},
        {"analyze-task", "verify-answer-plan"},
        set(),
        {"read_repo"},
        False,
    ),
]


def main() -> int:
    runtime_root = ROOT / "phasei_director_profile_assignment_permission_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    library = ExperienceLibrary(runtime_root / "experience")
    library.seed_defaults()
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    director = DirectorAgentV1(experience_library=library)

    profile_results: dict[str, dict[str, object]] = {}
    all_profiles_ok = True
    for case_id, objective, expected_family, expected_verification, expected_sources, _, _, _, expected_multi in CASES:
        diagnosis = director.diagnose(objective, repo_policy)
        profile = diagnosis.task_profile
        families = set(profile.get("task_families", []))
        verification = set(profile.get("verification_methods", []))
        sources = set(profile.get("knowledge_sources", []))
        case_ok = (
            expected_family in families
            and bool(expected_verification & verification)
            and bool(expected_sources & sources)
            and bool(profile.get("predicted_failure_modes"))
            and profile.get("estimated_budget", {}).get("estimated_agents", 0) >= 1
            and isinstance(profile.get("estimated_budget", {}).get("worth_multi_candidate"), bool)
            and profile.get("estimated_budget", {}).get("worth_multi_candidate") is expected_multi
        )
        all_profiles_ok = all_profiles_ok and case_ok
        profile_results[case_id] = {
            "ok": case_ok,
            "task_families": sorted(families),
            "verification_methods": sorted(verification),
            "knowledge_sources": sorted(sources),
            "predicted_failure_modes": profile.get("predicted_failure_modes"),
            "estimated_budget": profile.get("estimated_budget"),
        }

    fallback_results: dict[str, dict[str, object]] = {}
    all_fallbacks_ok = True
    compiler = WorkflowCompiler()
    first_compiled = None
    first_blueprint = None
    for case_id, objective, expected_family, _, _, expected_nodes, forbidden_nodes, expected_intents, _ in CASES:
        workspace = runtime_root / f"model_director_{case_id}"
        blueprint = director.plan_with_model_director(
            objective,
            repo_policy,
            workspace,
            model_provider="deterministic",
            model="deterministic-director",
            timeout_sec=240,
        )
        compiled = compiler.compile(blueprint, experience_library=library)
        if first_compiled is None:
            first_compiled = compiled
            first_blueprint = blueprint
        node_ids = {node.node_id for node in blueprint.workflow_skeleton.nodes}
        skeleton_ids_by_inst = {
            inst.skeleton_node_id: inst.node.node_id
            for inst in blueprint.node_instantiations
        }
        intents = {
            intent
            for item in blueprint.permission_plan
            for intent in item.get("permission_intents", [])
        }
        write_intents = [
            item
            for item in blueprint.permission_plan
            if "write_patch" in item.get("permission_intents", [])
        ]
        case_ok = (
            compiled.accepted
            and blueprint.task_profile.get("primary_task_family") == expected_family
            and expected_nodes.issubset(node_ids)
            and not forbidden_nodes.intersection(node_ids)
            and expected_intents.issubset(intents)
            and (expected_family == "code_repair" or not write_intents)
        )
        all_fallbacks_ok = all_fallbacks_ok and case_ok
        fallback_results[case_id] = {
            "ok": case_ok,
            "compiled": compiled.accepted,
            "task_family": blueprint.task_profile.get("primary_task_family"),
            "topology": blueprint.workflow_skeleton.topology,
            "node_ids": sorted(node_ids),
            "instantiation_map": skeleton_ids_by_inst,
            "permission_intents": sorted(intents),
            "write_intent_nodes": [item.get("skeleton_node_id") for item in write_intents],
            "finding_codes": [finding.code for finding in compiled.findings],
        }

    compiled = first_compiled
    blueprint = first_blueprint
    finding_codes = [finding.code for finding in compiled.findings] if compiled else []

    output = {
        "profiles": profile_results,
        "fallback_model_director": fallback_results,
        "compiled_summary": {
            "accepted": compiled.accepted if compiled else False,
            "blueprint_id": compiled.blueprint_id if compiled else "",
            "finding_codes": finding_codes,
        },
        "task_profile": blueprint.task_profile,
        "execution_estimate": blueprint.workflow_skeleton.execution_estimate,
        "work_assignment_count": len(blueprint.work_assignment_plan),
        "permission_plan_count": len(blueprint.permission_plan),
        "node_count": len(blueprint.workflow_skeleton.nodes),
        "permission_secret_flags": [
            item.get("minimum_boundary", {}).get("secret_access")
            for item in blueprint.permission_plan
        ],
        "finding_codes": finding_codes,
        "workspace": str(runtime_root),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if (
        all_profiles_ok
        and all_fallbacks_ok
        and compiled.accepted
        and blueprint.task_profile
        and blueprint.workflow_skeleton.execution_estimate
        and len(blueprint.work_assignment_plan) == len(blueprint.workflow_skeleton.nodes)
        and len(blueprint.permission_plan) == len(blueprint.node_instantiations)
        and all(flag is False for flag in output["permission_secret_flags"])
        and not any(code.startswith("director_missing") for code in finding_codes)
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
