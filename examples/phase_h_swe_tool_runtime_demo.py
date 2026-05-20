from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule
from egtc_runtime_stagea.tool_registry import swe_dataset_model_config


CASE_PATH = ROOT / "swe_smoke_data" / "cases" / "DataDog__integrations-core-14649.json"


def prepare_seed_workspace(runtime_root: Path) -> Path:
    seed = runtime_root / "seed_workspace"
    if seed.exists():
        shutil.rmtree(seed)
    seed.mkdir(parents=True)
    case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    (seed / "swe_case.json").write_text(
        json.dumps(case, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (seed / "README.md").write_text(
        "# SWE tool runtime seed\n\n"
        "Contains DataDog__integrations-core-14649 for model-agent tool runtime testing.\n",
        encoding="utf-8",
    )
    (seed / "sample_pkg").mkdir()
    (seed / "sample_pkg" / "__init__.py").write_text(
        'CASE_ID = "DataDog__integrations-core-14649"\n',
        encoding="utf-8",
    )
    (seed / "swe_case_contract.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import json",
                "from pathlib import Path",
                "",
                "case = json.loads(Path('swe_case.json').read_text(encoding='utf-8'))",
                "summary = {",
                "    'instance_id': case.get('instance_id'),",
                "    'repo': case.get('repo'),",
                "    'patch_lines': len((case.get('patch') or '').splitlines()),",
                "    'problem_chars': len(case.get('problem_statement') or ''),",
                "    'has_patch': bool(case.get('patch')),",
                "}",
                "Path('swe_case_contract_result.json').write_text(",
                "    json.dumps(summary, indent=2, sort_keys=True),",
                "    encoding='utf-8',",
                ")",
                "print(json.dumps({'type': 'test_result', 'name': 'swe_case_contract', 'passed': bool(summary['instance_id'] and summary['repo'] and summary['has_patch']), **summary}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=seed, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=egtc@example.invalid",
            "-c",
            "user.name=EGTC Test",
            "commit",
            "-m",
            "seed SWE tool runtime workspace",
        ],
        cwd=seed,
        check=True,
        capture_output=True,
        text=True,
    )
    return seed


def build_swe_tool_node(seed_workspace: Path) -> NodeCapsule:
    case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    config = swe_dataset_model_config()
    config.update(
        {
            "output_file": "swe_tool_worker_output.json",
            "output_json": True,
            "test_name": "phase_h_swe_tool_runtime",
            "deterministic_response_json": {
                "status": "ok",
                "case_id": case["instance_id"],
                "repo": case["repo"],
                "validation_scope": "SWE case local tool runtime and evidence path",
            },
            "deterministic_tool_calls": [
                {
                    "id": "read-swe-case",
                    "tool_id": "filesystem.read_text",
                    "arguments": {"path": "swe_case.json", "max_bytes": 12_000},
                },
                {
                    "id": "search-problem-marker",
                    "tool_id": "filesystem.search",
                    "arguments": {
                        "root": ".",
                        "query": "templates.count",
                        "max_matches": 10,
                    },
                },
                {
                    "id": "select-swe-case",
                    "tool_id": "dataset.select_swe_cases",
                    "arguments": {"rows": [case], "mode": "simple", "count": 1},
                },
                {
                    "id": "inspect-git-status",
                    "tool_id": "git.inspect",
                    "arguments": {"action": "status", "timeout_sec": 30},
                },
                {
                    "id": "compile-sample-package",
                    "tool_id": "python.compileall",
                    "arguments": {
                        "paths": ["sample_pkg"],
                        "quiet": 1,
                        "timeout_sec": 60,
                    },
                },
                {
                    "id": "run-swe-contract",
                    "tool_id": "python.run_script",
                    "arguments": {
                        "script": "swe_case_contract.py",
                        "timeout_sec": 60,
                    },
                },
                {
                    "id": "write-swe-report",
                    "tool_id": "filesystem.write_artifact",
                    "arguments": {
                        "path": "swe_tool_runtime_report.json",
                        "json": {
                            "case_id": case["instance_id"],
                            "repo": case["repo"],
                            "tool_runtime_checked": True,
                        },
                    },
                },
            ],
        }
    )
    return NodeCapsule(
        node_id="swe-tool-runtime-datadog-14649",
        phase="Phase H SWE tool runtime",
        goal=(
            "Use a real cached SWE case to validate model-agent MCP tool dispatch, "
            "permission grounding, evidence collection, and Overlooker acceptance."
        ),
        command=[],
        acceptance_criteria=[
            "Model-agent must execute filesystem, dataset selection, git, and python tools.",
            "Tool evidence must show all configured calls completed without denial or runtime error.",
            "GraphRuntime must collect tool audit/evidence artifacts and release Overlooker acceptance.",
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
        prompt="Run the configured SWE tool calls and return structured evidence.",
        model_provider="deterministic",
        model="deterministic-swe-tool-runtime",
        model_config=config,
        sandbox_profile={
            "backend": "model_agent",
            "sandbox_mode": "workspace_write",
            "network": "none",
            "allowed_read_paths": ["."],
            "allowed_write_paths": ["."],
            "resource_limits": {
                "wall_time_sec": 180,
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
            "test_name": "phase_h_swe_network_permission_denied",
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
        node_id="swe-network-tool-denied",
        phase="Phase H SWE tool runtime",
        goal="Verify network dataset tools are blocked when network permission is absent.",
        command=[],
        acceptance_criteria=[
            "Network SWE streaming tool under network:none must be denied before execution.",
            "Tool evidence must request permission review for the denied network call.",
        ],
        required_evidence=["log", "sandbox_events", "resource_report", "tool_evidence"],
        workspace=str(seed_workspace),
        executor_kind="model_agent",
        prompt="Attempt the configured network SWE dataset tool call.",
        model_provider="deterministic",
        model="deterministic-swe-tool-runtime",
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
    runtime_root = ROOT / "phaseh_swe_tool_runtime_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    seed_workspace = prepare_seed_workspace(runtime_root)

    success_node = build_swe_tool_node(seed_workspace)
    success_runtime = GraphRuntime(runtime_root / "success")
    success = success_runtime.run_graph(
        GraphRunSpec(
            graph_id="phase-h-swe-tool-runtime",
            nodes=[success_node],
            edges=[],
            max_parallelism=1,
            max_attempts=1,
            retry_budget=0,
            overlooker_mode="deterministic",
            director_mode="deterministic",
            phase="H",
        ),
        run_id="phase-h-swe-tool-runtime",
    )
    success_record = success["nodes"][success_node.node_id]
    success_workspace = Path(success_record["accepted_workspace"])
    tool_evidence = json.loads(
        (success_workspace / "tool_evidence.json").read_text(encoding="utf-8")
    )
    worker_output = json.loads(
        (success_workspace / "swe_tool_worker_output.json").read_text(encoding="utf-8")
    )
    contract_result = json.loads(
        (success_workspace / "swe_case_contract_result.json").read_text(encoding="utf-8")
    )

    denied_node = build_network_denied_node(seed_workspace)
    denied_runtime = GraphRuntime(runtime_root / "network_denied")
    denied = denied_runtime.run_graph(
        GraphRunSpec(
            graph_id="phase-h-swe-network-denied",
            nodes=[denied_node],
            edges=[],
            max_parallelism=1,
            max_attempts=1,
            retry_budget=0,
            overlooker_mode="deterministic",
            director_mode="deterministic",
            phase="H",
        ),
        run_id="phase-h-swe-network-denied",
    )
    denied_record = denied["nodes"][denied_node.node_id]
    denied_workspace = Path(denied_record["current_workspace"])
    denied_evidence = json.loads(
        (denied_workspace / "tool_evidence.json").read_text(encoding="utf-8")
    )

    report = {
        "case": "DataDog__integrations-core-14649",
        "success": {
            "accepted": success["accepted"],
            "node_status": success_record["status"],
            "worker_id": success_record["current_worker_id"],
            "overlooker_verdict": success_record["overlooker_verdict"],
            "release_overlooker": success_record["release_overlooker"],
            "tool_call_count": tool_evidence["call_count"],
            "tool_ok_count": tool_evidence["ok_count"],
            "tool_error_count": tool_evidence["error_count"],
            "tool_denied_count": tool_evidence["denied_count"],
            "command_count": tool_evidence["command_count"],
            "resource_report_ref_present": bool(success_record["resource_report_ref"]),
            "sandbox_events_ref_present": bool(success_record["sandbox_events_ref"]),
            "tool_runtime_in_worker_output": worker_output.get("tool_runtime", {}),
            "contract_result": contract_result,
        },
        "network_permission_check": {
            "graph_accepted": denied["accepted"],
            "node_status": denied_record["status"],
            "tool_status": denied_evidence["results"][0]["status"],
            "permission_review_required": denied_evidence["permission_review_required"],
            "reason": denied_evidence["results"][0]["error"],
        },
    }
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    passed = (
        success["accepted"]
        and success_record["status"] == "NODE_ACCEPTED"
        and success_record["overlooker_verdict"] == "pass"
        and tool_evidence["call_count"] == 7
        and tool_evidence["ok_count"] == 7
        and tool_evidence["error_count"] == 0
        and tool_evidence["denied_count"] == 0
        and tool_evidence["command_count"] == 3
        and contract_result["instance_id"] == "DataDog__integrations-core-14649"
        and denied["accepted"] is False
        and denied_evidence["permission_review_required"] is True
        and denied_evidence["results"][0]["status"] == "denied"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
