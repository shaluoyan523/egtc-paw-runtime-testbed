from __future__ import annotations

import json
import shutil
import sys
import argparse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egtc_runtime_stagea.graph_runtime import GraphRunSpec, GraphRuntime  # noqa: E402
from egtc_runtime_stagea.models import NodeCapsule  # noqa: E402


RUNTIME_ROOT = ROOT / "em_patch_optimization_graph_data"
RUNS_ROOT = ROOT / "_em_runs"
SEED_ROOT = RUNTIME_ROOT / "seed_workspaces"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the EGTC EM patch optimization replica graph")
    parser.add_argument("--mock-hfss", action="store_true", help="Use mock HFSS for fast EGTC runtime verification")
    parser.add_argument("--max-parallelism", type=int, default=1, help="Graph scheduler parallelism; keep real HFSS runs serial by default")
    args = parser.parse_args()

    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)
    if RUNS_ROOT.exists():
        shutil.rmtree(RUNS_ROOT)
    SEED_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)

    candidates = [
        ("candidate-w54p5", {"patch_width_mm": 54.5, "patch_length_mm": 38.5, "feed_y_offset_mm": 16.0}),
        ("candidate-w55p0", {"patch_width_mm": 55.0, "patch_length_mm": 38.5, "feed_y_offset_mm": 16.0}),
        ("candidate-w55p5", {"patch_width_mm": 55.5, "patch_length_mm": 38.5, "feed_y_offset_mm": 16.0}),
    ]
    nodes: list[NodeCapsule] = []
    edges: list[tuple[str, str]] = []
    candidate_records: list[dict[str, Any]] = []
    for node_id, overrides in candidates:
        run_id = f"egtc_patch_{node_id.replace('-', '_')}"
        design = design_payload(run_id, overrides)
        seed = write_seed(node_id, design=design)
        nodes.append(simulate_node(node_id, run_id, seed, mock_hfss=args.mock_hfss))
        edges.append((node_id, "compare-candidates"))
        candidate_records.append(
            {
                "run_id": run_id,
                "workspace": str(RUNS_ROOT / run_id),
                "parameters": design["parameters"],
            }
        )

    compare_seed = write_seed(
        "compare-candidates",
        candidates_payload={
            "workspace_root": str(RUNS_ROOT),
            "run_ids": [record["run_id"] for record in candidate_records],
            "candidates": candidate_records,
            "design": {
                "parameters": {"f0_ghz": 2.45},
                "targets": {"s11_db_at_f0_max": -10},
            },
        },
    )
    refine_records: list[dict[str, Any]] = []
    for node_id, feed_y in [("refine-feed17", 17.0), ("refine-feed18", 18.0)]:
        run_id = f"egtc_patch_{node_id.replace('-', '_')}"
        design = design_payload(
            run_id,
            {"patch_width_mm": 54.0, "patch_length_mm": 38.5, "feed_y_offset_mm": feed_y},
        )
        seed = write_seed(
            node_id,
            design=design,
            refinement_payload={
                "blend_best_and_proposed_keys": ["patch_width_mm"],
                "blend_factor": 0.5,
                "parameter_overrides": {"feed_y_offset_mm": feed_y},
            },
        )
        nodes.append(simulate_node(node_id, run_id, seed, mock_hfss=args.mock_hfss, refinement=True))
        edges.append(("compare-candidates", node_id))
        edges.append((node_id, "compare-refined"))
        refine_records.append(
            {
                "run_id": run_id,
                "workspace": str(RUNS_ROOT / run_id),
                "parameters": design["parameters"],
            }
        )
    compare_refined_seed = write_seed(
        "compare-refined",
        candidates_payload={
            "workspace_root": str(RUNS_ROOT),
            "run_ids": [record["run_id"] for record in [*candidate_records, *refine_records]],
            "candidates": [*candidate_records, *refine_records],
            "design": {
                "parameters": {"f0_ghz": 2.45},
                "targets": {"s11_db_at_f0_max": -10},
            },
        },
    )
    confirm_records: list[dict[str, Any]] = []
    for node_id, overrides in [
        ("confirm-w55p0-f17p2", {"patch_width_mm": 55.0, "patch_length_mm": 38.5, "feed_y_offset_mm": 17.2}),
        ("confirm-w55p0-f18p0", {"patch_width_mm": 55.0, "patch_length_mm": 38.5, "feed_y_offset_mm": 18.0}),
    ]:
        run_id = f"egtc_patch_{node_id.replace('-', '_')}"
        design = design_payload(run_id, overrides, maximum_passes=2)
        seed = write_seed(node_id, design=design)
        nodes.append(simulate_node(node_id, run_id, seed, mock_hfss=args.mock_hfss))
        edges.append(("compare-refined", node_id))
        edges.append((node_id, "compare-confirmed"))
        confirm_records.append(
            {
                "run_id": run_id,
                "workspace": str(RUNS_ROOT / run_id),
                "parameters": design["parameters"],
            }
        )
    compare_confirmed_seed = write_seed(
        "compare-confirmed",
        candidates_payload={
            "workspace_root": str(RUNS_ROOT),
            "run_ids": [record["run_id"] for record in confirm_records],
            "candidates": confirm_records,
            "design": {
                "parameters": {"f0_ghz": 2.45},
                "targets": {"s11_db_at_f0_max": -10},
            },
        },
    )
    final_design = design_payload(
        "egtc_patch_optimized_final",
        {"patch_width_mm": 54.0, "patch_length_mm": 38.5, "feed_y_offset_mm": 17.0},
        maximum_passes=2,
    )
    final_seed = write_seed("final-confirmation", design=final_design)
    nodes.append(compare_node(compare_seed))
    nodes.append(compare_node(compare_refined_seed, node_id="compare-refined", run_id="egtc_patch_compare_refined"))
    nodes.append(compare_node(compare_confirmed_seed, node_id="compare-confirmed", run_id="egtc_patch_compare_confirmed"))
    nodes.append(finalize_node(final_seed, mock_hfss=args.mock_hfss))
    edges.append(("compare-confirmed", "final-confirmation"))

    runtime = GraphRuntime(RUNTIME_ROOT / "runtime")
    result = runtime.run_graph(
        GraphRunSpec(
            graph_id="egtc-em-patch-optimization-replica",
            nodes=nodes,
            edges=edges,
            max_parallelism=max(1, args.max_parallelism),
            max_attempts=2,
            retry_budget=2,
            overlooker_mode="deterministic",
            director_mode="deterministic",
            phase="G",
        ),
        run_id="egtc-em-patch-optimization-replica",
    )
    final_workspace_value = result["nodes"]["final-confirmation"].get("accepted_workspace")
    final_workspace = Path(final_workspace_value) if final_workspace_value else None
    final_validation = (
        json.loads((final_workspace / "validation_report.json").read_text(encoding="utf-8"))
        if final_workspace and (final_workspace / "validation_report.json").exists()
        else {}
    )
    compare_workspace_value = result["nodes"]["compare-confirmed"].get("accepted_workspace")
    compare_workspace = Path(compare_workspace_value) if compare_workspace_value else None
    optimizer_report = (
        json.loads((compare_workspace / "optimizer_report.json").read_text(encoding="utf-8"))
        if compare_workspace and (compare_workspace / "optimizer_report.json").exists()
        else {}
    )
    summary = {
        "accepted": result["accepted"],
        "status": result["status"],
        "run_id": result["run_id"],
        "checkpoint_path": result["checkpoint_path"],
        "candidate_count": len(candidates) + len(refine_records) + len(confirm_records),
        "mock_hfss": args.mock_hfss,
        "best_candidate": optimizer_report.get("best_candidate"),
        "final_workspace": str(final_workspace) if final_workspace else None,
        "final_validation_status": final_validation.get("status"),
        "final_metrics": final_validation.get("metrics", {}),
        "node_statuses": {
            node_id: record["status"]
            for node_id, record in result["nodes"].items()
        },
    }
    (RUNTIME_ROOT / "egtc_em_patch_optimization_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if result["accepted"] else 1


def design_payload(run_id: str, overrides: dict[str, float], *, maximum_passes: int = 1) -> dict[str, Any]:
    params = {
        "f0_ghz": 2.45,
        "substrate_er": 4.4,
        "substrate_h_mm": 1.6,
        "patch_width_mm": 55.0,
        "patch_length_mm": 38.5,
        "ground_width_mm": 110.0,
        "ground_length_mm": 100.0,
        "feed_x_offset_mm": 0.0,
        "feed_y_offset_mm": 16.0,
        "coax_inner_radius_mm": 0.5,
        "coax_outer_radius_mm": 1.7,
        "coax_feed_length_mm": 12.0,
        "air_height_mm": 40.0,
        "copper_thickness_mm": 0.035,
    }
    params.update(overrides)
    return {
        "project_name": run_id,
        "design_name": run_id,
        "units": "mm",
        "solution_type": "DrivenTerminal",
        "hfss_backend": "ansysedt_script",
        "maximum_passes": maximum_passes,
        "solve": True,
        "hfss_timeout_sec": 240,
        "parameters": params,
        "sweep": {"start_ghz": 2.35, "stop_ghz": 2.55, "points": 9},
        "targets": {"s11_db_at_f0_max": -10},
    }


def write_seed(
    node_id: str,
    *,
    design: dict[str, Any] | None = None,
    candidates_payload: dict[str, Any] | None = None,
    refinement_payload: dict[str, Any] | None = None,
) -> Path:
    seed = SEED_ROOT / node_id
    seed.mkdir(parents=True, exist_ok=True)
    if design is not None:
        (seed / "design.json").write_text(json.dumps(design, indent=2, sort_keys=True), encoding="utf-8")
    if candidates_payload is not None:
        (seed / "candidates.json").write_text(
            json.dumps(candidates_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if refinement_payload is not None:
        (seed / "refinement.json").write_text(
            json.dumps(refinement_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return seed


def simulate_node(
    node_id: str,
    run_id: str,
    seed: Path,
    *,
    mock_hfss: bool,
    refinement: bool = False,
) -> NodeCapsule:
    command = [
        sys.executable,
        str(ROOT / "examples" / "em_bridge_graph_worker.py"),
        "--action",
        "simulate",
        "--run-id",
        run_id,
        "--workspace-root",
        str(RUNS_ROOT),
        "--design-json",
        "design.json",
    ]
    if refinement:
        command.extend(["--refine-json", "refinement.json"])
    if mock_hfss:
        command.append("--mock-hfss")
    return base_node(
        node_id=node_id,
        phase="em_candidate_simulation",
        goal=f"Run solver-backed EM candidate {run_id}.",
        command=command,
        workspace=seed,
        required_evidence=[
            "log",
            "test",
            "diff",
            "sandbox_events",
            "resource_report",
            "solver_report",
            "sparameters",
            "validation_report",
        ],
    )


def compare_node(
    seed: Path,
    *,
    node_id: str = "compare-candidates",
    run_id: str = "egtc_patch_compare",
) -> NodeCapsule:
    return base_node(
        node_id=node_id,
        phase="em_candidate_ranking",
        goal="Rank EM candidates using validation metrics at the target frequency.",
        command=[
            sys.executable,
            str(ROOT / "examples" / "em_bridge_graph_worker.py"),
            "--action",
            "compare",
            "--run-id",
            run_id,
            "--workspace-root",
            str(RUNS_ROOT),
            "--candidates-json",
            "candidates.json",
        ],
        workspace=seed,
        required_evidence=[
            "log",
            "test",
            "diff",
            "sandbox_events",
            "resource_report",
            "optimizer_report",
        ],
    )


def finalize_node(seed: Path, *, mock_hfss: bool) -> NodeCapsule:
    command = [
        sys.executable,
        str(ROOT / "examples" / "em_bridge_graph_worker.py"),
        "--action",
        "finalize",
        "--run-id",
        "egtc_patch_optimized_final",
        "--workspace-root",
        str(RUNS_ROOT),
        "--design-json",
        "design.json",
        "--apply-selection",
    ]
    if mock_hfss:
        command.append("--mock-hfss")
    return base_node(
        node_id="final-confirmation",
        phase="em_final_confirmation",
        goal="Re-run the selected patch antenna candidate as the final confirmation.",
        command=command,
        workspace=seed,
        required_evidence=[
            "log",
            "test",
            "diff",
            "sandbox_events",
            "resource_report",
            "solver_report",
            "sparameters",
            "validation_report",
            "target_validation_passed",
            "bridge_report",
        ],
    )


def base_node(
    *,
    node_id: str,
    phase: str,
    goal: str,
    command: list[str],
    workspace: Path,
    required_evidence: list[str],
) -> NodeCapsule:
    return NodeCapsule(
        node_id=node_id,
        phase=phase,
        goal=goal,
        command=command,
        acceptance_criteria=[
            "Worker must emit a passing phasea_test_result.json.",
            "EGTC must collect required EM artifacts as evidence.",
            "Deterministic Overlooker must cite validator-backed evidence_ref.",
        ],
        required_evidence=required_evidence,
        workspace=str(workspace),
        executor_kind="subprocess",
        sandbox_profile={
            "backend": "subprocess",
            "sandbox_mode": "workspace_write",
            "network": "none",
            "allowed_read_paths": ["."],
            "allowed_write_paths": ["."],
            "resource_limits": {
                "wall_time_sec": 360,
                "memory_mb": 4096,
                "disk_mb": 4096,
                "max_processes": 64,
                "max_command_count": 1,
            },
        },
        experience_pattern_ids=[
            "seed-handoff-artifact-chain",
            "seed-review-verification-aware-planning",
        ],
    )


if __name__ == "__main__":
    raise SystemExit(main())
