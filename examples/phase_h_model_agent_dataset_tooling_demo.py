from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.compiler import WorkflowCompiler
from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule
from egtc_runtime_stagea.phaseb_models import (
    PermissionGroundingReport,
    RepoPolicy,
    SandboxProfile,
    TaskDiagnosis,
    WorkflowBlueprint,
    WorkflowSkeleton,
    WorkflowSkeletonNode,
    NodeInstantiation,
    structured,
)
from egtc_runtime_stagea.tool_registry import swe_dataset_model_config


def build_node(seed_workspace: Path) -> NodeCapsule:
    config = swe_dataset_model_config()
    config.update(
        {
            "input_files": ["case_request.json"],
            "output_file": "dataset_tooling_report.json",
            "output_json": True,
        }
    )
    return NodeCapsule(
        node_id="model-swe-dataset-tooling",
        phase="Phase H dataset tooling",
        goal="Verify that a provider-backed unit agent receives SWE dataset tools and MCP descriptors.",
        command=[],
        acceptance_criteria=[
            "Worker must run through executor_kind=model_agent.",
            "Worker output must list SWE dataset tool ids.",
            "Compiler must preserve MCP descriptors and warn when network-only dataset tools are planned under network:none.",
        ],
        required_evidence=["log", "test", "sandbox_events", "resource_report"],
        workspace=str(seed_workspace),
        executor_kind="model_agent",
        prompt=(
            "Inspect case_request.json and the available tooling profile. "
            "Emit JSON proving which dataset, filesystem, git, python, and MCP capabilities are visible."
        ),
        model_provider="deterministic",
        model="deterministic-dataset-tooling",
        model_config=config,
        sandbox_profile={
            "backend": "model_agent",
            "sandbox_mode": "workspace_write",
            "network": "none",
            "allowed_read_paths": ["."],
            "allowed_write_paths": ["."],
            "resource_limits": {
                "wall_time_sec": 120,
                "memory_mb": 512,
                "disk_mb": 256,
                "max_processes": 1,
                "max_command_count": 0,
            },
        },
    )


def compile_node(node: NodeCapsule) -> dict[str, object]:
    repo_policy = RepoPolicy(
        repo_root=str(ROOT),
        package_managers=["python"],
        test_commands=[],
        allowed_read_paths=["."],
        allowed_write_paths=["."],
        sensitive_paths=[".git"],
        network_allowed_by_default=False,
    )
    blueprint = WorkflowBlueprint(
        blueprint_id="phaseh-dataset-tooling-blueprint",
        director_id="director-agent-v1",
        task_diagnosis=TaskDiagnosis(
            task_id="phaseh-dataset-tooling-task",
            objective=node.goal,
            task_kind="tooling_validation",
            risk_level="medium",
            repo_touchpoints=["examples", "egtc_runtime_stagea"],
            requires_code_change=False,
            requires_tests=True,
        ),
        repo_policy=repo_policy,
        workflow_skeleton=WorkflowSkeleton(
            skeleton_id="phaseh-dataset-tooling-skeleton",
            topology="single_model_agent_tooling_probe",
            nodes=[
                WorkflowSkeletonNode(
                    node_id="tooling-probe",
                    phase=node.phase,
                    role="worker",
                    goal=node.goal,
                    expected_outputs=["dataset_tooling_report"],
                )
            ],
            edges=[],
            rationale="A single probe is enough to verify tool and MCP injection.",
            agent_allocation={"total_agents": 1, "roles": {"worker": 1}},
            alternative_skeletons=[
                {"name": "single_probe", "estimated_agents": 1, "selected": True},
                {"name": "split_probe_and_review", "estimated_agents": 2, "selected": False},
                {"name": "dataset_download_probe", "estimated_agents": 2, "selected": False},
            ],
        ),
        node_instantiations=[
            NodeInstantiation(
                node=node,
                skeleton_node_id="tooling-probe",
                permission_grounding=PermissionGroundingReport(
                    node_id=node.node_id,
                    sandbox_profile=SandboxProfile(
                        network="none",
                        allowed_read_paths=["."],
                        allowed_write_paths=["."],
                        allowed_commands=[],
                        justification="This probe validates manifest injection only; dataset network tools must remain gated.",
                    ),
                    grounded_by=[
                        "repo_policy.allowed_read_paths",
                        "repo_policy.allowed_write_paths",
                        "repo_policy.network_allowed_by_default",
                    ],
                ),
            )
        ],
        director_mode="deterministic",
    )
    compiled = WorkflowCompiler().compile(blueprint)
    return structured(compiled)


def main() -> int:
    runtime_root = ROOT / "phaseh_model_agent_dataset_tooling_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    seed_workspace = runtime_root / "seed_workspace"
    seed_workspace.mkdir(parents=True)
    (seed_workspace / "case_request.json").write_text(
        json.dumps(
            {
                "dataset": "AI-ModelScope/SWE-bench",
                "requested_modes": ["simple", "complex"],
                "note": "No network download is performed in this demo.",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    node = build_node(seed_workspace)
    compiled = compile_node(node)
    runtime = GraphRuntime(runtime_root)
    result = runtime.run_graph(
        GraphRunSpec(
            graph_id="phaseh-model-agent-dataset-tooling",
            nodes=[node],
            edges=[],
            max_parallelism=1,
            overlooker_mode="model_agent",
            director_mode="model_agent",
            phase="H",
        )
    )

    accepted_workspace = Path(result["nodes"][node.node_id]["accepted_workspace"])
    report = json.loads((accepted_workspace / "dataset_tooling_report.json").read_text(encoding="utf-8"))
    tool_ids = set(report.get("tool_ids", []))
    mcp_server_ids = set(report.get("mcp_server_ids", []))
    warning_codes = {
        finding["code"]
        for finding in compiled.get("findings", [])
        if finding.get("severity") == "warning"
    }
    output = {
        "compiled": compiled,
        "runtime_accepted": result["accepted"],
        "tooling_profile": report.get("tooling_profile"),
        "tool_ids": sorted(tool_ids),
        "mcp_server_ids": sorted(mcp_server_ids),
        "warning_codes": sorted(warning_codes),
        "accepted_workspace": str(accepted_workspace),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    required_tools = {
        "dataset.modelscope_swe_stream",
        "dataset.select_swe_cases",
        "filesystem.read_text",
        "git.inspect",
        "python.compileall",
    }
    required_mcp_servers = {
        "egtc.dataset",
        "egtc.filesystem",
        "egtc.git",
        "egtc.python",
    }
    return 0 if (
        compiled.get("accepted") is True
        and "model_agent_tool_requires_network" in warning_codes
        and "model_agent_mcp_requires_network" in warning_codes
        and result["accepted"]
        and required_tools.issubset(tool_ids)
        and required_mcp_servers.issubset(mcp_server_ids)
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
