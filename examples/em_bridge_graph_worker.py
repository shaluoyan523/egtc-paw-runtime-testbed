from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from em_mcp_bridge.adapters import OptimizerAdapter  # noqa: E402
from em_mcp_bridge.bridge import ThreeSoftwareMcpBridge  # noqa: E402
from em_mcp_bridge.server import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EGTC EM bridge graph worker")
    parser.add_argument("--action", required=True, choices=["simulate", "compare", "finalize"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "examples" / "em_mcp_bridge_config.json")
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--design-json", type=Path)
    parser.add_argument("--candidates-json", type=Path)
    parser.add_argument("--refine-json", type=Path)
    parser.add_argument("--apply-selection", action="store_true")
    parser.add_argument("--mock-hfss", action="store_true")
    parser.add_argument("--require-target-pass", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.action == "simulate":
            if args.design_json is None:
                raise ValueError("--design-json is required for simulate")
            result = simulate(args)
        elif args.action == "compare":
            if args.candidates_json is None:
                raise ValueError("--candidates-json is required for compare")
            result = compare(args)
        else:
            if args.design_json is None:
                raise ValueError("--design-json is required for finalize")
            result = finalize(args)
    except Exception as exc:
        result = {
            "status": "fail",
            "error": str(exc),
            "action": args.action,
            "run_id": args.run_id,
        }

    passed = result.get("status") == "ok"
    Path("phasea_test_result.json").write_text(
        json.dumps(
            {
                "type": "test_result",
                "name": f"em_bridge_graph_{args.action}",
                "passed": passed,
                "status": result.get("status"),
                "run_id": args.run_id,
                "metrics": result.get("metrics", {}),
                "artifacts": result.get("artifacts", {}),
                "warnings": result.get("warnings", []),
                "error": result.get("error"),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"type": "test_result", "name": f"em_bridge_graph_{args.action}", "passed": passed}))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if passed else 1


def bridge(args: argparse.Namespace) -> ThreeSoftwareMcpBridge:
    config = load_config(
        args.config,
        mock_hfss=bool(args.mock_hfss),
        workspace_root=args.workspace_root,
    )
    return ThreeSoftwareMcpBridge(config)


def simulate(args: argparse.Namespace) -> dict[str, Any]:
    _clear_workspace_outputs(Path.cwd())
    if args.apply_selection:
        apply_optimizer_selection(Path.cwd(), args.design_json)
    if args.refine_json is not None:
        apply_refinement(Path.cwd(), args.design_json, args.refine_json)
    design = read_json(args.design_json)
    payload = {
        "run_id": args.run_id,
        "solver_action": "create_patch_antenna",
        "design": design,
    }
    pipeline = bridge(args).run_pipeline(payload)
    run_workspace = Path(str(pipeline.get("workspace") or args.workspace_root / args.run_id))
    handoff_warnings = _copy_run_artifacts(run_workspace, Path.cwd())
    validation = read_json_if_exists(run_workspace / "validation_report.json")
    hfss_step = next(
        (
            step
            for step in pipeline.get("steps", [])
            if isinstance(step, dict) and step.get("software") == "hfss"
        ),
        {},
    )
    solver_ok = hfss_step.get("status") == "ok"
    validation_present = bool(validation)
    target_passed = validation.get("status") == "ok"
    status = "ok" if solver_ok and validation_present and (target_passed or not args.require_target_pass) else "fail"
    warnings = [
        *[str(item) for item in hfss_step.get("warnings", [])],
        *[str(item) for item in validation.get("warnings", [])],
        *handoff_warnings,
    ]
    return {
        "status": status,
        "action": "simulate",
        "run_id": args.run_id,
        "workspace": str(run_workspace),
        "artifacts": {
            **dict(hfss_step.get("artifacts", {})),
            "validation_report": str(run_workspace / "validation_report.json"),
            "bridge_report": str(run_workspace / "bridge_report.json"),
        },
        "metrics": {
            **dict(hfss_step.get("metrics", {})),
            **dict(validation.get("metrics", {})),
            "target_passed": target_passed,
        },
        "warnings": warnings,
        "error": None
        if status == "ok"
        else _simulate_error(solver_ok, validation_present, target_passed, args.require_target_pass),
        "solver": hfss_step,
        "validation": validation,
        "pipeline": pipeline,
    }


def compare(args: argparse.Namespace) -> dict[str, Any]:
    payload = read_json(args.candidates_json)
    payload.setdefault("workspace_root", str(args.workspace_root))
    result = OptimizerAdapter().execute("compare_results", payload, Path.cwd())
    return {
        "status": result.status,
        "action": "compare",
        "run_id": args.run_id,
        "artifacts": result.artifacts,
        "metrics": result.metrics,
        "warnings": result.warnings,
        "details": result.details,
    }


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    args.require_target_pass = True
    selection = apply_optimizer_selection(Path.cwd(), args.design_json)
    result = simulate(args)
    result["action"] = "finalize"
    if selection:
        result["selection"] = selection
    return result


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return read_json(path)


def apply_optimizer_selection(workspace: Path, design_path: Path) -> dict[str, Any]:
    optimizer_report = read_json_if_exists(workspace / "optimizer_report.json")
    best = optimizer_report.get("best_candidate") if isinstance(optimizer_report.get("best_candidate"), dict) else {}
    if not best:
        return {}

    candidate_parameters = best.get("parameters") if isinstance(best.get("parameters"), dict) else {}
    selected_parameters = dict(candidate_parameters)
    selection_source = "best_candidate_parameters"
    candidate_workspace = Path(str(best.get("workspace") or ""))
    candidate_optimizer = read_json_if_exists(candidate_workspace / "optimizer_report.json") if candidate_workspace else {}
    proposed = candidate_optimizer.get("proposed_parameters")
    if not best.get("target_pass") and isinstance(proposed, dict) and proposed:
        selected_parameters = dict(proposed)
        selection_source = "best_candidate_optimizer_proposal"

    if not selected_parameters:
        return {}

    design = read_json(design_path)
    parameters = design.get("parameters") if isinstance(design.get("parameters"), dict) else {}
    design["parameters"] = {**parameters, **selected_parameters}
    design_path.write_text(json.dumps(design, indent=2, sort_keys=True), encoding="utf-8")
    selection = {
        "source": selection_source,
        "best_run_id": best.get("run_id"),
        "best_target_pass": bool(best.get("target_pass")),
        "best_metrics": best.get("metrics", {}),
        "selected_parameters": selected_parameters,
    }
    (workspace / "selected_candidate.json").write_text(
        json.dumps(selection, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return selection


def apply_refinement(workspace: Path, design_path: Path, refine_path: Path) -> dict[str, Any]:
    refinement = read_json(refine_path)
    design = read_json(design_path)
    parameters = dict(design.get("parameters", {})) if isinstance(design.get("parameters"), dict) else {}

    optimizer_report = read_json_if_exists(workspace / "optimizer_report.json")
    best = optimizer_report.get("best_candidate") if isinstance(optimizer_report.get("best_candidate"), dict) else {}
    candidate_workspace = Path(str(best.get("workspace") or ""))
    candidate_optimizer = read_json_if_exists(candidate_workspace / "optimizer_report.json") if candidate_workspace else {}
    best_parameters = best.get("parameters") if isinstance(best.get("parameters"), dict) else {}
    proposed_parameters = (
        candidate_optimizer.get("proposed_parameters")
        if isinstance(candidate_optimizer.get("proposed_parameters"), dict)
        else {}
    )

    blend_factor = float(refinement.get("blend_factor", 0.5))
    blend_factor = max(0.0, min(1.0, blend_factor))
    for key in refinement.get("blend_best_and_proposed_keys", []):
        best_value = _numeric(best_parameters.get(key))
        proposed_value = _numeric(proposed_parameters.get(key))
        if best_value is not None and proposed_value is not None:
            parameters[str(key)] = round(best_value + (proposed_value - best_value) * blend_factor, 6)

    overrides = refinement.get("parameter_overrides")
    if isinstance(overrides, dict):
        parameters.update(overrides)

    design["parameters"] = parameters
    design_path.write_text(json.dumps(design, indent=2, sort_keys=True), encoding="utf-8")
    applied = {
        "source": "refinement",
        "best_run_id": best.get("run_id"),
        "blend_factor": blend_factor,
        "refinement": refinement,
        "selected_parameters": parameters,
    }
    (workspace / "refinement_candidate.json").write_text(
        json.dumps(applied, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return applied


def _copy_run_artifacts(source: Path, target: Path) -> list[str]:
    warnings: list[str] = []
    if not source.exists():
        return [f"Run workspace does not exist: {source}"]
    for path in _artifact_files(source):
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.resolve() != destination.resolve():
                shutil.copy2(path, destination)
        except OSError as exc:
            warnings.append(f"Could not copy non-critical artifact {relative}: {exc}")
    return warnings


def _artifact_files(source: Path) -> list[Path]:
    suffixes = {".json", ".jsonl", ".log", ".txt", ".s1p", ".s2p", ".s3p", ".aedt", ".py"}
    files = [
        path
        for path in source.iterdir()
        if path.is_file() and (path.suffix.lower() in suffixes or path.name.endswith(".ansysedt.log"))
    ]
    results_dir = source / "results"
    if results_dir.exists():
        files.extend(
            path
            for path in results_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in suffixes
        )
    return sorted(files)


def _clear_workspace_outputs(workspace: Path) -> None:
    stale_names = {
        "bridge_report.json",
        "bridge_steps.jsonl",
        "geometry_manifest.json",
        "hfss_patch_antenna_log.json",
        "hfss_worker_input.json",
        "hfss_worker_output.json",
        "hfss_worker_stderr.txt",
        "input_manifest.json",
        "optimizer_report.json",
        "phasea_test_result.json",
        "solver_report.json",
        "validation_report.json",
    }
    stale_suffixes = {".s1p", ".s2p", ".s3p", ".aedt", ".log"}
    for stale_dir in workspace.glob("*.aedtresults"):
        if stale_dir.is_dir():
            shutil.rmtree(stale_dir, ignore_errors=True)
    results_dir = workspace / "results"
    if results_dir.exists():
        shutil.rmtree(results_dir, ignore_errors=True)
    for path in workspace.iterdir():
        if not path.is_file():
            continue
        if path.name in stale_names or path.suffix.lower() in stale_suffixes or path.name.endswith(".ansysedt.log"):
            path.unlink(missing_ok=True)


def _simulate_error(
    solver_ok: bool,
    validation_present: bool,
    target_passed: bool,
    require_target_pass: bool,
) -> str:
    if not solver_ok:
        return "HFSS solver step did not report status=ok."
    if not validation_present:
        return "No validation_report.json was produced."
    if require_target_pass and not target_passed:
        return "Final confirmation did not pass the requested EM target."
    return "Simulation did not satisfy the worker acceptance policy."


def _numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
