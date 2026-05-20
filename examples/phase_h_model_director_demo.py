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
    runtime_root = ROOT / "phaseh_model_director_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    workspace = runtime_root / "director"
    library = ExperienceLibrary(runtime_root / "experience")
    library.seed_defaults()
    objective = (
        "替换掉Codex绑定，设计能使用其他模型的Director、Worker和Overlooker agent单元，"
        "要求Director比较经验库模式后决定节点和agent数量。"
    )
    repo_policy = RepoPolicyInferencer().infer(ROOT)
    director = DirectorAgentV1(experience_library=library)
    blueprint = director.plan_with_model_director(
        objective,
        repo_policy,
        workspace,
        model_provider="deterministic",
        model="deterministic-director",
        timeout_sec=240,
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
        "draft_plan_review": blueprint.workflow_skeleton.draft_plan_review,
        "experience_pattern_ids": blueprint.experience_pattern_ids,
        "node_executor_kinds": {
            inst.node.node_id: inst.node.executor_kind
            for inst in blueprint.node_instantiations
        },
        "node_model_providers": {
            inst.node.node_id: inst.node.model_provider
            for inst in blueprint.node_instantiations
        },
        "workspace": str(workspace),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    selected = [
        item for item in output["alternative_skeletons"] if item.get("selected")
    ]
    return 0 if (
        compiled.accepted
        and output["director_mode"] == "model_agent"
        and str(output["director_session_id"] or "").startswith("director-")
        and output["director_skill_usage"].get("loaded") is True
        and output["experience_pattern_ids"]
        and output["agent_allocation"].get("total_agents") == len(output["node_executor_kinds"])
        and len(output["alternative_skeletons"]) >= 3
        and len(selected) == 1
        and output["draft_plan_review"].get("structure_findings")
        and all(kind == "model_agent" for kind in output["node_executor_kinds"].values())
        and all(provider == "deterministic" for provider in output["node_model_providers"].values())
        and (workspace / "director_output.json").exists()
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
