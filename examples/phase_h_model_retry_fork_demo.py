from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime
from egtc_runtime_stagea.models import NodeCapsule


def sandbox(read_only: bool, timeout_sec: int = 60) -> dict[str, object]:
    return {
        "backend": "model_agent",
        "sandbox_mode": "read_only" if read_only else "workspace_write",
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": [] if read_only else ["."],
        "resource_limits": {
            "wall_time_sec": timeout_sec,
            "memory_mb": 512,
            "disk_mb": 256,
            "max_processes": 8,
            "max_command_count": 1,
        },
    }


def baseline_node() -> NodeCapsule:
    return NodeCapsule(
        node_id="baseline",
        phase="baseline",
        goal="Create a clean accepted upstream workspace.",
        command=[
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import json; "
                "Path('baseline.txt').write_text('clean baseline\\n'); "
                "Path('phasea_test_result.json').write_text(json.dumps({'passed': True, 'name': 'baseline_contract'})); "
                "print(json.dumps({'type':'test_result','name':'baseline_contract','passed':True}))"
            ),
        ],
        acceptance_criteria=[
            "Worker reaches WorkerSubmitted only.",
            "Evidence contains a passing test report.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile=sandbox(read_only=False, timeout_sec=30),
    )


def flaky_model_worker() -> NodeCapsule:
    return NodeCapsule(
        node_id="model-flaky",
        phase="verification",
        goal="Fail once, then succeed only when retried from a clean accepted fork.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "phase_d_worker.py"),
            "read",
            "model-flaky",
            "0.0",
            "--fail-once",
            "--poison-on-fail",
            "--fail-if-poisoned",
        ],
        acceptance_criteria=[
            "Attempt 1 must fail after poisoning its workspace.",
            "Attempt 2 must run from the accepted baseline workspace.",
            "Model Overlooker and model Director GraphPatch must control retry.",
        ],
        required_evidence=["diff", "test", "log", "sandbox_events", "resource_report"],
        sandbox_profile=sandbox(read_only=False, timeout_sec=30),
    )


def main() -> int:
    runtime_root = ROOT / "phaseh_model_retry_fork_data"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)

    runtime = GraphRuntime(runtime_root)
    spec = GraphRunSpec(
        graph_id="phase-h-model-retry-fork",
        nodes=[baseline_node(), flaky_model_worker()],
        edges=[("baseline", "model-flaky")],
        max_parallelism=1,
        max_attempts=2,
        retry_budget=1,
        replan_budget=0,
        max_same_failure_retries=2,
        overlooker_mode="model_agent",
        director_mode="model_agent",
        phase="H",
    )
    result = runtime.run_graph(spec, run_id="phase-h-model-retry-fork")
    flaky = result["nodes"]["model-flaky"]
    fork_history = flaky["fork_history"]
    attempt1 = Path(fork_history[0]["target_workspace"])
    attempt2 = Path(fork_history[1]["target_workspace"]) if len(fork_history) > 1 else None
    workflow_observations = runtime.experience_library.load_workflow_observations()
    observation = workflow_observations[-1] if workflow_observations else None
    workflow_dynamic_events = [
        event["event_type"]
        for event in (observation.dynamic_workflow_events if observation else [])
    ]
    report = {
        "accepted": result["accepted"],
        "status": result["status"],
        "attempts": flaky["attempts"],
        "node_status": flaky["status"],
        "overlooker_verdict": flaky["overlooker_verdict"],
        "retry_fork_reason": fork_history[-1]["reason"] if fork_history else None,
        "retry_source_node": fork_history[-1]["source_node_id"] if fork_history else None,
        "attempt1_poison_exists": (attempt1 / "model-flaky.poison").exists(),
        "attempt2_poison_exists": (attempt2 / "model-flaky.poison").exists() if attempt2 else None,
        "attempt2_success_exists": (attempt2 / "model-flaky_read.txt").exists() if attempt2 else None,
        "fork_advisor_history": flaky["fork_advisor_history"],
        "graph_patch_history": flaky["graph_patch_history"],
        "retry_events": result["retry_events"],
        "workflow_learning_present": bool(result.get("workflow_learning")),
        "workflow_observation_count": len(workflow_observations),
        "workflow_outcome": observation.outcome if observation else None,
        "workflow_retry_count": observation.retry_count if observation else None,
        "workflow_replan_count": observation.replan_count if observation else None,
        "workflow_dynamic_events": workflow_dynamic_events,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report["accepted"]
        and report["attempts"] == 2
        and report["retry_fork_reason"] == "retry_from_accepted_dependency"
        and report["retry_source_node"] == "baseline"
        and len(report["fork_advisor_history"]) == 1
        and report["fork_advisor_history"][0]["selected_node_id"] == "baseline"
        and len(report["graph_patch_history"]) == 1
        and report["graph_patch_history"][0]["compiled"]["accepted"]
        and str(report["graph_patch_history"][0]["patch"]["patch_id"]).startswith("model-agent-graph-patch-")
        and report["graph_patch_history"][0]["patch"]["operations"][0]["op"] == "retry_node"
        and report["attempt1_poison_exists"]
        and not report["attempt2_poison_exists"]
        and report["attempt2_success_exists"]
        and report["workflow_learning_present"]
        and report["workflow_outcome"] == "replanned"
        and report["workflow_retry_count"] == 1
        and report["workflow_replan_count"] == 1
        and "DirectorGraphPatchSessionCompleted" in report["workflow_dynamic_events"]
        and "OverlookerForkDecision" in report["workflow_dynamic_events"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
