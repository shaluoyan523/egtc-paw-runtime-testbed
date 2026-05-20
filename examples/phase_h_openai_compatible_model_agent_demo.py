from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule


def main() -> int:
    if not os.environ.get("MODEL_AGENT_API_KEY"):
        print(json.dumps({"skipped": True, "reason": "MODEL_AGENT_API_KEY is not set"}))
        return 2

    runtime_root = ROOT / "phaseh_openai_compatible_model_agent_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    node = NodeCapsule(
        node_id="deepseek-openai-compatible-agent",
        phase="Phase H model agent provider probe",
        goal="Run an OpenAI-compatible provider-backed model agent through GraphRuntime.",
        command=[],
        acceptance_criteria=[
            "Model agent must return JSON output.",
            "Evidence must include test, sandbox_events, and resource_report.",
            "Deterministic Overlooker must release the node.",
        ],
        required_evidence=["log", "test", "sandbox_events", "resource_report"],
        executor_kind="model_agent",
        prompt=(
            "Return only JSON: {\"ok\": true, \"graph_runtime\": true, "
            "\"summary\": \"openai-compatible model agent executed\"}"
        ),
        model_provider="openai_compatible",
        model=os.environ.get("MODEL_AGENT_MODEL", "deepseek-v4-flash"),
        model_config={
            "base_url": os.environ.get("MODEL_AGENT_BASE_URL", "https://zgc.apihy.com/v1"),
            "api_key_env": "MODEL_AGENT_API_KEY",
            "output_file": "agent_output.json",
            "output_json": True,
            "temperature": 0,
            "max_tokens": 160,
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
    spec = GraphRunSpec(
        graph_id="phase-h-openai-compatible-model-agent",
        nodes=[node],
        edges=[],
        max_parallelism=1,
        max_attempts=1,
        retry_budget=0,
        overlooker_mode="deterministic",
        director_mode="deterministic",
        phase="E",
    )
    result = runtime.run_graph(spec, run_id="phase-h-openai-compatible-model-agent")
    record = result["nodes"][node.node_id]
    workspace = Path(record["accepted_workspace"]) if record.get("accepted_workspace") else None
    output_path = workspace / "agent_output.json" if workspace else None
    output = json.loads(output_path.read_text()) if output_path and output_path.exists() else None
    report = {
        "accepted": result["accepted"],
        "status": result["status"],
        "node_status": record["status"],
        "worker_id": record["current_worker_id"],
        "evidence_ref_present": bool(record["evidence_ref"]),
        "resource_report_ref_present": bool(record["resource_report_ref"]),
        "sandbox_events_ref_present": bool(record["sandbox_events_ref"]),
        "overlooker_verdict": record["overlooker_verdict"],
        "release_overlooker": record["release_overlooker"],
        "model": node.model,
        "base_url": node.model_config["base_url"],
        "output": output,
    }
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if (
        result["accepted"]
        and record["status"] == "NODE_ACCEPTED"
        and record["overlooker_verdict"] == "pass"
        and isinstance(output, dict)
        and output.get("ok") is True
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
