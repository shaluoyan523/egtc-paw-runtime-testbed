from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea import StageARuntime
from egtc_runtime_stagea.models import NodeCapsule
from swe_phasea_smoke import select_simple_cases, write_case


def prepare_seed_workspace(case_path: Path, seed_root: Path) -> Path:
    if seed_root.exists():
        shutil.rmtree(seed_root)
    seed_root.mkdir(parents=True)
    shutil.copy2(case_path, seed_root / "swe_case.json")
    (seed_root / "README.md").write_text(
        "This is a Stage A Codex worker workspace. Read swe_case.json and produce the requested artifacts.\n",
        encoding="utf-8",
    )
    return seed_root


def codex_prompt() -> str:
    return """
You are a Codex worker inside EGTC-PAW Runtime Stage A.

Task:
1. Read ./swe_case.json.
2. Create ./swe_codex_observation.md with:
   - instance_id
   - repo
   - base_commit
   - problem_statement character count
   - patch line count
   - a short note that this is static Phase A evidence, not official SWE-bench evaluation
3. Create ./phasea_test_result.json as a JSON object:
   {"passed": true, "name": "codex_swe_static_observation", "type": "test_result"}

Do not clone repositories. Do not run external tests. Keep the output concise.
""".strip()


def run_case(runtime: StageARuntime, case: dict[str, Any], seed_workspace: Path) -> dict[str, Any]:
    node = NodeCapsule(
        node_id=f"swe-codex-{case['instance_id']}",
        phase="Phase A SWE Codex smoke",
        goal="Launch a real Codex CLI worker and accept only after evidence-backed Overlooker review.",
        command=[],
        executor_kind="codex_cli",
        prompt=codex_prompt(),
        workspace=str(seed_workspace),
        acceptance_criteria=[
            "A real codex exec --json session is launched.",
            "Worker completion only reaches WorkerSubmitted.",
            "Workspace diff includes Codex-authored observation and phasea_test_result.json.",
            "Overlooker pass cites evidence_ref.",
        ],
    )
    return runtime.run_node(node)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="train")
    parser.add_argument("--scan-limit", type=int, default=40)
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()

    selected = select_simple_cases(args.split, args.scan_limit, args.count)
    output_root = ROOT / "swe_codex_smoke_data"
    cases_dir = output_root / "cases"
    seeds_dir = output_root / "seed_workspaces"
    runtime = StageARuntime(output_root / "runtime_data", overlooker_mode="codex")

    results = []
    for case in selected:
        case_path = write_case(case, cases_dir)
        seed_workspace = prepare_seed_workspace(
            case_path,
            seeds_dir / str(case["instance_id"]).replace("/", "__"),
        )
        run_result = run_case(runtime, case, seed_workspace)
        codex_events = [
            event
            for event in run_result["worker_result"]["parsed_events"]
            if str(event.get("type", "")).startswith(("thread.", "turn.", "item."))
        ]
        results.append(
            {
                "instance_id": case["instance_id"],
                "repo": case["repo"],
                "final_state": run_result["final_state"],
                "run_id": run_result["run_id"],
                "workspace": run_result["workspace"],
                "evidence_ref": run_result["evidence"]["evidence_ref"]["uri"],
                "overlooker_verdict": run_result["overlooker_report"]["verdict"],
                "overlooker_evidence_ref": run_result["overlooker_report"]["evidence_ref"],
                "overlooker_id": run_result["overlooker_report"]["overlooker_id"],
                "overlooker_report_ref": run_result["overlooker_report"]["report_ref"]["uri"],
                "validator_passed": all(
                    report["passed"] for report in run_result["validator_reports"]
                ),
                "codex_event_count": len(codex_events),
                "worker_exit_code": run_result["worker_result"]["exit_code"],
            }
        )

    report = {
        "dataset": "AI-ModelScope/SWE-bench",
        "mode": "real Codex CLI Phase A smoke; official SWE-bench tests are not executed",
        "split": args.split,
        "scan_limit": args.scan_limit,
        "selected_count": len(selected),
        "accepted_count": sum(1 for item in results if item["final_state"] == "NodeAccepted"),
        "results": results,
    }
    report_path = output_root / "swe_phasea_codex_smoke_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted_count"] == len(results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
