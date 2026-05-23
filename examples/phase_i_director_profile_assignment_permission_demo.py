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


CASES = [
    (
        "browsecomp",
        "BrowseComp style retrieval task: find externally evidenced answer with source citations.",
        "retrieval",
        {"external_fact_evidence"},
        {"network_or_local_corpus"},
    ),
    (
        "finance",
        "Finance-Agent task: compute portfolio return using formula and calculator evidence.",
        "finance_calculation",
        {"formula_check"},
        {"network_or_local_corpus"},
    ),
    (
        "swe",
        "SWE-bench complex code repair task: inspect repo, patch bug, and run tests.",
        "code_repair",
        {"unit_tests"},
        {"repo", "tool_execution"},
    ),
    (
        "terminal",
        "Terminal-Bench task: use shell/container checkpoint to complete environment work.",
        "terminal_execution",
        {"container_test"},
        {"tool_execution"},
    ),
    (
        "plancraft",
        "PlanCraft planning state transition task with possible ambiguity.",
        "planning_state_transition",
        {"state_transition_check"},
        {"prompt"},
    ),
    (
        "opendeepthink",
        "OpenDeepThink hard contest reasoning task with judge and pairwise candidates.",
        "contest_reasoning",
        {"judge"},
        {"tool_execution"},
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
    for case_id, objective, expected_family, expected_verification, expected_sources in CASES:
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

    workspace = runtime_root / "model_director"
    objective = (
        "SWE-bench complex code repair task: replace Codex-only agent binding with provider-backed "
        "Director, Worker, Overlooker units, with repo tests and bounded write ownership."
    )
    blueprint = director.plan_with_model_director(
        objective,
        repo_policy,
        workspace,
        model_provider="deterministic",
        model="deterministic-director",
        timeout_sec=240,
    )
    compiled = WorkflowCompiler().compile(blueprint, experience_library=library)
    finding_codes = [finding.code for finding in compiled.findings]

    output = {
        "profiles": profile_results,
        "compiled": structured(compiled),
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
        "workspace": str(workspace),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if (
        all_profiles_ok
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
