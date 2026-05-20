from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule
from egtc_runtime_stagea.phaseb_models import structured


def main() -> int:
    runtime_root = ROOT / "phaseh_model_agent_unit_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    seed_workspace = runtime_root / "seed_workspace"
    seed_workspace.mkdir(parents=True)
    (seed_workspace / "input.txt").write_text("model-agent worker input\n", encoding="utf-8")

    worker = NodeCapsule(
        node_id="model-worker",
        phase="implementation",
        goal="Run a provider-backed model worker and produce evidence.",
        command=[],
        acceptance_criteria=[
            "Worker must run through executor_kind=model_agent.",
            "Model Overlooker must cite evidence_ref.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        workspace=str(seed_workspace),
        executor_kind="model_agent",
        prompt="Read input.txt and emit a JSON model-agent response.",
        model_provider="deterministic",
        model="deterministic-unit-test",
        model_config={
            "input_files": ["input.txt"],
            "output_file": "model_worker_output.json",
            "output_json": True,
            "deterministic_response_json": {
                "status": "implemented",
                "worker": "model-agent",
                "passed": True,
            },
        },
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
    runtime = GraphRuntime(runtime_root)
    result = runtime.run_graph(
        GraphRunSpec(
            graph_id="phaseh-model-agent-unit",
            nodes=[worker],
            edges=[],
            max_parallelism=1,
            overlooker_mode="model_agent",
            director_mode="model_agent",
            phase="H",
        )
    )
    output = {
        "accepted": result["accepted"],
        "status": result["status"],
        "node": result["nodes"]["model-worker"],
        "events": result["events"],
        "workspace": result["nodes"]["model-worker"]["accepted_workspace"],
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    node = output["node"]
    event_types = {event["event_type"] for event in output["events"]}
    return 0 if (
        result["accepted"]
        and node["overlooker_verdict"] == "pass"
        and node["release_overlooker"] is True
        and node["accepted_workspace"]
        and Path(node["accepted_workspace"], "model_worker_output.json").exists()
        and "EvidenceCollected" in event_types
        and "DeterministicValidationCompleted" in event_types
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
