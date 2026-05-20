from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule
from egtc_runtime_stagea.tool_registry import swe_dataset_model_config


def build_success_node(seed_workspace: Path) -> NodeCapsule:
    config = swe_dataset_model_config()
    config.update(
        {
            "output_file": "tool_call_worker_output.json",
            "output_json": True,
            "deterministic_tool_calls": [
                {
                    "id": "read-input",
                    "tool_id": "filesystem.read_text",
                    "arguments": {"path": "input.txt", "max_bytes": 2000},
                },
                {
                    "id": "search-marker",
                    "tool_id": "filesystem.search",
                    "arguments": {"root": ".", "query": "SWE_TOOL_MARKER", "max_matches": 10},
                },
                {
                    "id": "write-report",
                    "tool_id": "filesystem.write_artifact",
                    "arguments": {
                        "path": "tool_written_report.json",
                        "json": {"created_by": "filesystem.write_artifact", "passed": True},
                    },
                },
                {
                    "id": "compile-seed",
                    "tool_id": "python.compileall",
                    "arguments": {"paths": ["sample_pkg"], "quiet": 1, "timeout_sec": 60},
                },
            ],
        }
    )
    return NodeCapsule(
        node_id="model-tool-success",
        phase="Phase H tool runtime",
        goal="Actually call runtime tools from a model-agent unit.",
        command=[],
        acceptance_criteria=[
            "Tool calls must execute through runtime dispatch.",
            "Tool audit and tool evidence artifacts must be collected.",
            "Python command count must be reflected in the resource report.",
        ],
        required_evidence=[
            "log",
            "test",
            "sandbox_events",
            "resource_report",
            "tool_audit",
            "tool_evidence",
        ],
        workspace=str(seed_workspace),
        executor_kind="model_agent",
        prompt="Use the configured deterministic tool calls and emit structured evidence.",
        model_provider="deterministic",
        model="deterministic-tool-runtime",
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
                "max_processes": 8,
                "max_command_count": 8,
            },
        },
    )


def build_network_denied_node(seed_workspace: Path) -> NodeCapsule:
    config = swe_dataset_model_config()
    config.update(
        {
            "output_file": "network_denied_output.json",
            "output_json": True,
            "deterministic_tool_calls": [
                {
                    "id": "stream-swe-denied",
                    "tool_id": "dataset.modelscope_swe_stream",
                    "arguments": {"split": "train", "scan_limit": 1},
                }
            ],
        }
    )
    return NodeCapsule(
        node_id="model-tool-network-denied",
        phase="Phase H tool runtime",
        goal="Verify that network dataset tool calls require permission review.",
        command=[],
        acceptance_criteria=[
            "Network dataset calls under network:none must be denied.",
            "Tool evidence must mark permission_review_required.",
        ],
        required_evidence=["log", "sandbox_events", "resource_report", "tool_evidence"],
        workspace=str(seed_workspace),
        executor_kind="model_agent",
        prompt="Attempt the configured dataset stream tool call.",
        model_provider="deterministic",
        model="deterministic-tool-runtime",
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


def main() -> int:
    runtime_root = ROOT / "phaseh_model_agent_tool_call_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    seed_workspace = runtime_root / "seed_workspace"
    (seed_workspace / "sample_pkg").mkdir(parents=True)
    (seed_workspace / "input.txt").write_text(
        "SWE_TOOL_MARKER: model-agent runtime tool call smoke\n",
        encoding="utf-8",
    )
    (seed_workspace / "sample_pkg" / "__init__.py").write_text(
        "VALUE = 'SWE_TOOL_MARKER'\n",
        encoding="utf-8",
    )

    success_node = build_success_node(seed_workspace)
    network_node = build_network_denied_node(seed_workspace)

    success_runtime = GraphRuntime(runtime_root / "success")
    success = success_runtime.run_graph(
        GraphRunSpec(
            graph_id="phaseh-model-agent-tool-success",
            nodes=[success_node],
            edges=[],
            max_parallelism=1,
            overlooker_mode="model_agent",
            director_mode="model_agent",
            phase="H",
        )
    )
    success_workspace = Path(success["nodes"][success_node.node_id]["accepted_workspace"])
    success_tool_evidence = json.loads((success_workspace / "tool_evidence.json").read_text(encoding="utf-8"))
    success_output = json.loads((success_workspace / "tool_call_worker_output.json").read_text(encoding="utf-8"))

    denied_runtime = GraphRuntime(runtime_root / "network_denied")
    denied = denied_runtime.run_graph(
        GraphRunSpec(
            graph_id="phaseh-model-agent-tool-network-denied",
            nodes=[network_node],
            edges=[],
            max_parallelism=1,
            max_attempts=1,
            retry_budget=0,
            overlooker_mode="deterministic",
            director_mode="deterministic",
            phase="H",
        )
    )
    denied_workspace = Path(denied["nodes"][network_node.node_id]["current_workspace"])
    denied_tool_evidence = json.loads((denied_workspace / "tool_evidence.json").read_text(encoding="utf-8"))

    resource_report = success["nodes"][success_node.node_id]["resource_report_ref"]
    evidence_artifacts = success["nodes"][success_node.node_id]["evidence_ref"]
    output = {
        "success_accepted": success["accepted"],
        "success_tool_call_count": success_tool_evidence["call_count"],
        "success_command_count": success_tool_evidence["command_count"],
        "success_output_tool_runtime": success_output.get("tool_runtime", {}),
        "written_report_exists": (success_workspace / "tool_written_report.json").exists(),
        "denied_status": denied["nodes"][network_node.node_id]["status"],
        "denied_permission_review_required": denied_tool_evidence["permission_review_required"],
        "denied_result_status": denied_tool_evidence["results"][0]["status"],
        "resource_report_ref": resource_report,
        "evidence_ref": evidence_artifacts,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if (
        success["accepted"]
        and success_tool_evidence["call_count"] == 4
        and success_tool_evidence["ok_count"] == 4
        and success_tool_evidence["command_count"] >= 1
        and (success_workspace / "tool_written_report.json").exists()
        and denied_tool_evidence["permission_review_required"] is True
        and denied_tool_evidence["results"][0]["status"] == "denied"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
