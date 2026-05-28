from __future__ import annotations

import contextlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) != 2:
        print("usage: python -m em_mcp_bridge.hfss_worker input.json output.json", file=sys.stderr)
        return 2
    input_path = Path(args[0])
    output_path = Path(args[1])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    try:
        result = create_patch(payload)
    except Exception as exc:
        result = {
            "status": "fail",
            "error": str(exc),
            "artifacts": {"worker_input": str(input_path)},
            "metrics": {},
            "warnings": [],
            "details": {},
        }
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result.get("status") == "ok" else 1


def create_patch(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("model_kind", "")).lower() == "fss_unit_cell":
        return create_fss_unit_cell_with_pyaedt(payload)
    backend = str(payload.get("execution_backend", "ansysedt_script"))
    if backend == "ansysedt_script":
        return create_patch_with_ansysedt_script(payload)
    return create_patch_with_pyaedt(payload)


def create_patch_with_pyaedt(payload: dict[str, Any]) -> dict[str, Any]:
    module = str(payload["module"])
    with contextlib.redirect_stdout(sys.stderr):
        if module == "ansys.aedt.core":
            from ansys.aedt.core import Hfss  # type: ignore
        else:
            from pyaedt import Hfss  # type: ignore

    project_path = Path(str(payload["project_path"]))
    log_path = Path(str(payload["log_path"]))
    report_path = Path(str(payload["report_path"]))
    design = payload["design"]
    geometry = payload["geometry"]
    created: list[str] = []
    warnings: list[str] = []

    with contextlib.redirect_stdout(sys.stderr):
        hfss = Hfss(
            project=str(project_path),
            design=str(payload["design_name"]),
            solution_type=str(payload["solution_type"]),
            version=payload.get("version"),
            non_graphical=bool(payload.get("non_graphical", False)),
            new_desktop=True,
            close_on_exit=True,
        )
    try:
        with contextlib.redirect_stdout(sys.stderr):
            hfss.modeler.model_units = str(payload.get("units", "mm"))
            _create_patch_geometry(hfss, geometry, created, warnings)
            _try_assign_patch_boundaries(hfss, warnings)
            _try_create_setup(hfss, design, geometry, warnings)
            if hasattr(hfss, "save_project"):
                hfss.save_project(str(project_path))
        log_path.write_text(
            json.dumps(
                {
                    "software": "hfss",
                    "backend": module,
                    "status": "created_patch_antenna",
                    "project": str(project_path),
                    "units": payload.get("units", "mm"),
                    "objects": created,
                    "geometry": geometry,
                    "warnings": warnings,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    finally:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                hfss.release_desktop()
        except Exception:
            pass

    report_path.write_text(
        json.dumps(
            {
                "software": "hfss",
                "backend": module,
                "status": "created_patch_antenna",
                "project": str(project_path),
                "log": str(log_path),
                "geometry": geometry,
                "objects": created,
                "warnings": warnings,
                "note": "Geometry is created and saved. Solver execution is intentionally separate.",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "artifacts": {
            "hfss_project": str(project_path),
            "solver_report": str(report_path),
            "solver_log": str(log_path),
        },
        "metrics": {
            "object_count": len(created),
            "f0_ghz": geometry["f0_ghz"],
            "substrate_er": geometry["substrate_er"],
        },
        "warnings": warnings,
        "details": {"project_path": str(project_path), "objects": created},
    }


def create_fss_unit_cell_with_pyaedt(payload: dict[str, Any]) -> dict[str, Any]:
    module = str(payload["module"])
    with contextlib.redirect_stdout(sys.stderr):
        if module == "ansys.aedt.core":
            from ansys.aedt.core import Hfss  # type: ignore
        else:
            from pyaedt import Hfss  # type: ignore

    project_path = Path(str(payload["project_path"]))
    log_path = Path(str(payload["log_path"]))
    report_path = Path(str(payload["report_path"]))
    design = payload["design"]
    geometry = payload["geometry"]
    created: list[str] = []
    warnings: list[str] = []
    solved = False
    sparameter_path = project_path.with_suffix(".s2p")
    mesh_report_path = project_path.with_suffix(".mesh_report.json")
    face_ids: dict[str, int] = {}

    with contextlib.redirect_stdout(sys.stderr):
        hfss = Hfss(
            project=str(project_path),
            design=str(payload["design_name"]),
            solution_type=str(payload["solution_type"]),
            version=payload.get("version"),
            non_graphical=bool(payload.get("non_graphical", False)),
            new_desktop=True,
            close_on_exit=True,
        )
    try:
        with contextlib.redirect_stdout(sys.stderr):
            hfss.modeler.model_units = str(payload.get("units", "mm"))
            _create_fss_geometry(hfss, geometry, created, warnings)
            face_ids = _assign_fss_boundaries(hfss, geometry, design, warnings)
            setup_created = _try_create_fss_setup(hfss, design, geometry, warnings)
            if hasattr(hfss, "save_project"):
                hfss.save_project(str(project_path))
            if bool(design.get("solve", False)):
                if not setup_created:
                    raise RuntimeError("FSS solve requested but setup creation did not complete.")
                solved = bool(hfss.analyze_setup("Setup1"))
                if solved:
                    exported = hfss.export_touchstone(
                        setup="Setup1",
                        sweep="Sweep1",
                        output_file=str(sparameter_path),
                    )
                    if isinstance(exported, str) and exported:
                        sparameter_path = Path(exported)
                    if hasattr(hfss, "save_project"):
                        hfss.save_project(str(project_path))
            else:
                solved = False
        log_path.write_text(
            json.dumps(
                {
                    "software": "hfss",
                    "backend": module,
                    "status": "solved_fss_unit_cell" if solved else "created_fss_unit_cell",
                    "project": str(project_path),
                    "units": payload.get("units", "mm"),
                    "objects": created,
                    "geometry": geometry,
                    "face_ids": face_ids,
                    "warnings": warnings,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    finally:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                hfss.release_desktop()
        except Exception:
            pass

    sweep = design.get("sweep") if isinstance(design.get("sweep"), dict) else {}
    metrics: dict[str, Any] = {
        "object_count": len(created),
        "f0_ghz": geometry["f0_ghz"],
        "period_mm": geometry["period_mm"],
        "substrate_er": geometry["substrate_er"],
        "plate_thickness_mm": geometry["plate_thickness_mm"],
        "floquet_modes": int(design.get("floquet_modes", 1)),
        "lattice_pair_count": 2,
        "floquet_port_count": 2,
        "setup_created": True,
        "solved": solved,
    }
    if sweep:
        metrics.update(
            {
                "sweep_start_ghz": float(sweep.get("start_ghz", 4.0)),
                "sweep_stop_ghz": float(sweep.get("stop_ghz", 18.0)),
                "sweep_points": int(sweep.get("points", 57)),
            }
        )
    artifacts = {
        "hfss_project": str(project_path),
        "solver_report": str(report_path),
        "solver_log": str(log_path),
    }
    if sparameter_path.exists():
        artifacts["sparameters"] = str(sparameter_path)
        metrics.update(_parse_touchstone_network_metrics(sparameter_path, float(geometry["f0_ghz"])))
    if mesh_report_path.exists():
        artifacts["mesh_report"] = str(mesh_report_path)

    error: str | None = None
    status = "ok"
    if bool(design.get("solve", False)) and not solved:
        status = "fail"
        error = "HFSS solve was requested, but PyAEDT did not report a completed solve."
    elif bool(design.get("solve", False)) and not sparameter_path.exists():
        status = "fail"
        error = "HFSS solve was requested, but no Touchstone S-parameter file was exported."
    report_path.write_text(
        json.dumps(
            {
                "software": "hfss",
                "backend": module,
                "status": "solved_fss_unit_cell" if solved else "created_fss_unit_cell",
                "project": str(project_path),
                "log": str(log_path),
                "geometry": geometry,
                "objects": created,
                "face_ids": face_ids,
                "metrics": metrics,
                "artifacts": artifacts,
                "warnings": warnings,
                "note": (
                    "Initial 3D dual-polarized FSS model based on the paper dimensions: "
                    "crossed double-sided strip-line substrates, center vias, inserted metal plate, "
                    "lattice-pair periodic boundaries, and two Floquet ports along z."
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "status": status,
        "error": error,
        "artifacts": artifacts,
        "metrics": metrics,
        "warnings": warnings,
        "details": {
            "project_path": str(project_path),
            "objects": created,
            "face_ids": face_ids,
            "backend": module,
        },
    }


def create_patch_with_ansysedt_script(payload: dict[str, Any]) -> dict[str, Any]:
    installation = payload.get("aedt_installation") if isinstance(payload.get("aedt_installation"), dict) else {}
    ansysedt_path = Path(str(installation.get("ansysedt_path") or os.environ.get("ANSYSEDT_PATH", "")))
    if not ansysedt_path.exists():
        return {
            "status": "blocked",
            "error": "ansysedt.exe was not found for AEDT script execution.",
            "artifacts": {},
            "metrics": {},
            "warnings": [],
            "details": {"ansysedt_path": str(ansysedt_path)},
        }

    project_path = Path(str(payload["project_path"]))
    log_path = Path(str(payload["log_path"]))
    report_path = Path(str(payload["report_path"]))
    script_path = project_path.with_suffix(".create_patch.py")
    aedt_log_path = project_path.with_suffix(".ansysedt.log")
    native_status_path = project_path.with_suffix(".native_status.json")
    design = payload["design"]
    geometry = payload["geometry"]
    feed_type = str(design.get("feed_type", geometry.get("feed_type", "coax_probe"))).lower()
    if feed_type != "coax_probe":
        geometry["feed_type"] = "coax_probe"
    else:
        geometry["feed_type"] = feed_type
    created = [
        "substrate_FR4",
        "ground_copper",
        "patch_copper",
        "feed_pin",
        "coax_pin",
        "coax",
        "port_cap",
        "port1",
        "air_region",
    ]
    warnings: list[str] = ["Created through AEDT native RunScriptAndExit backend."]
    if feed_type != "coax_probe":
        warnings.append(f"Unsupported feed_type={feed_type!r}; using coax_probe for HFSS port/simulation.")
    _normalize_probe_feed_geometry(geometry, warnings)
    lock_path = project_path.with_suffix(".aedt.lock")
    if lock_path.exists():
        try:
            lock_path.unlink()
            warnings.append("Removed stale AEDT lock file before RunScriptAndExit.")
        except OSError as exc:
            warnings.append(f"Unable to remove stale AEDT lock file before launch: {exc}")
    script_path.write_text(_native_aedt_script(payload, created), encoding="utf-8")

    cmd = [
        str(ansysedt_path),
        "-RunScriptAndExit",
        str(script_path),
        "-features=beta",
        "-ng",
        "-Logfile",
        str(aedt_log_path),
    ]
    completed = subprocess.run(
        cmd,
        cwd=str(project_path.parent),
        text=True,
        capture_output=True,
        check=False,
        timeout=int(design.get("ansysedt_script_timeout_sec", 240)),
    )
    if completed.returncode != 0:
        return {
            "status": "blocked",
            "error": f"ansysedt.exe RunScriptAndExit failed with exit code {completed.returncode}.",
            "artifacts": {
                "hfss_script": str(script_path),
                "aedt_log": str(aedt_log_path),
            },
            "metrics": {},
            "warnings": warnings,
            "details": {
                "stdout_tail": (completed.stdout or "")[-2000:],
                "stderr_tail": (completed.stderr or "")[-2000:],
                "command": cmd,
            },
        }
    if not project_path.exists():
        return {
            "status": "fail",
            "error": "AEDT script exited without creating the project file.",
            "artifacts": {
                "hfss_script": str(script_path),
                "aedt_log": str(aedt_log_path),
            },
            "metrics": {},
            "warnings": warnings,
            "details": {"command": cmd},
        }

    native_status: dict[str, Any] = {}
    if native_status_path.exists():
        try:
            native_status = json.loads(native_status_path.read_text(encoding="utf-8"))
            warnings.extend(str(item) for item in native_status.get("warnings", []) if str(item) not in warnings)
        except Exception as exc:
            warnings.append(f"Unable to parse AEDT native status JSON: {exc}")

    if project_path.with_suffix(".aedt.lock").exists():
        try:
            project_path.with_suffix(".aedt.lock").unlink()
            warnings.append("Removed stale AEDT lock file after RunScriptAndExit completed.")
        except OSError as exc:
            warnings.append(f"Unable to remove AEDT lock file: {exc}")

    sparameter_path = project_path.with_suffix(".s1p")
    if native_status.get("sparameters"):
        candidate = Path(str(native_status["sparameters"]))
        if candidate.exists():
            sparameter_path = candidate
    mesh_report_path = project_path.with_suffix(".mesh_report.json")
    solve_requested = bool(design.get("solve", False))
    solved = bool(native_status.get("solved", False))
    port_count = int(native_status.get("port_count", 0) or 0)
    metrics: dict[str, Any] = {
        "object_count": len(created),
        "f0_ghz": geometry["f0_ghz"],
        "substrate_er": geometry["substrate_er"],
        "feed_y_offset_mm": geometry.get("feed_y_offset_mm"),
        "port_count": port_count,
        "setup_created": bool(native_status.get("setup_created", False)),
        "solved": solved,
    }
    artifacts = {
        "hfss_project": str(project_path),
        "solver_report": str(report_path),
        "solver_log": str(log_path),
        "hfss_script": str(script_path),
        "aedt_log": str(aedt_log_path),
        "native_status": str(native_status_path),
    }
    if sparameter_path.exists():
        artifacts["sparameters"] = str(sparameter_path)
        metrics.update(_parse_touchstone_metrics(sparameter_path, float(geometry["f0_ghz"])))
    if mesh_report_path.exists():
        artifacts["mesh_report"] = str(mesh_report_path)

    status = "ok"
    error: str | None = None
    if native_status.get("status") == "fail":
        status = "fail"
        error = str(native_status.get("error") or "AEDT native script reported failure.")
    elif port_count <= 0:
        status = "fail"
        error = "AEDT project was created, but no HFSS excitation/port was detected."
    elif solve_requested and not solved:
        status = "fail"
        error = "HFSS solve was requested, but AEDT did not report a completed solve."
    elif solve_requested and not sparameter_path.exists():
        status = "fail"
        error = "HFSS solve was requested, but no Touchstone S-parameter file was exported."

    status_label = "solved_patch_antenna" if solved and sparameter_path.exists() else "created_patch_antenna_with_port"
    log_path.write_text(
        json.dumps(
            {
                "software": "hfss",
                "backend": "ansysedt_script",
                "status": status_label,
                "project": str(project_path),
                "units": payload.get("units", "mm"),
                "objects": created,
                "geometry": geometry,
                "warnings": warnings,
                "script": str(script_path),
                "aedt_log": str(aedt_log_path),
                "native_status": native_status,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(
            {
                "software": "hfss",
                "backend": "ansysedt_script",
                "status": status_label,
                "project": str(project_path),
                "log": str(log_path),
                "script": str(script_path),
                "aedt_log": str(aedt_log_path),
                "geometry": geometry,
                "objects": created,
                "metrics": metrics,
                "native_status": native_status,
                "warnings": warnings,
                "note": (
                    "Geometry, coax-probe terminal port, radiation boundary, and setup are created. "
                    "When solve=true, HFSS is analyzed and Touchstone S-parameters are exported."
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "status": status,
        "error": error,
        "artifacts": artifacts,
        "metrics": metrics,
        "warnings": warnings,
        "details": {
            "project_path": str(project_path),
            "objects": created,
            "backend": "ansysedt_script",
            "native_status": native_status,
        },
    }


def _native_aedt_script(payload: dict[str, Any], created: list[str]) -> str:
    project_path = str(payload["project_path"])
    design_name = str(payload["design_name"])
    units = str(payload.get("units", "mm"))
    geometry = payload["geometry"]
    design = payload["design"]
    sweep = design.get("sweep") if isinstance(design.get("sweep"), dict) else {}
    status_path = str(Path(project_path).with_suffix(".native_status.json"))
    sparameter_path = str(Path(project_path).with_suffix(".s1p"))
    mesh_report_path = str(Path(project_path).with_suffix(".mesh_report.json"))

    def q(value: Any) -> str:
        return json.dumps(str(value))

    def expr(value: float, suffix: str = units) -> str:
        return f"{float(value):.12g}{suffix}"

    gw = float(geometry["ground_width_mm"])
    gl = float(geometry["ground_length_mm"])
    h = float(geometry["substrate_h_mm"])
    pw = float(geometry["patch_width_mm"])
    pl = float(geometry["patch_length_mm"])
    air = float(geometry["air_height_mm"])
    feed_x = float(geometry.get("feed_x_offset_mm", 0.0))
    feed_y = float(geometry.get("feed_y_offset_mm", 8.0))
    coax_inner = float(geometry.get("coax_inner_radius_mm", 0.5))
    coax_outer = float(geometry.get("coax_outer_radius_mm", 1.7))
    feed_len = float(geometry.get("coax_feed_length_mm", 12.0))
    port_cap_thickness = float(geometry.get("port_cap_thickness_mm", 1.0))
    air_margin = max(air / 2, 10)
    air_z0 = -feed_len - port_cap_thickness - air_margin
    air_z_size = h + air + feed_len + port_cap_thickness + air_margin

    solve_requested = bool(design.get("solve", False))
    create_setup = bool(design.get("create_setup", False) or solve_requested)
    f0 = float(geometry["f0_ghz"])
    start = float(sweep.get("start_ghz", max(0.01, f0 * 0.8)))
    stop = float(sweep.get("stop_ghz", f0 * 1.2))
    points = int(sweep.get("points", 101))
    max_passes = int(design.get("maximum_passes", 6))
    sweep_type = str(design.get("sweep_type", "Discrete"))

    return f'''# AEDT native script generated by em_mcp_bridge.
import json
import os
import sys
import traceback

import ScriptEnv

project_path = {q(project_path)}
design_name = {q(design_name)}
status_path = {q(status_path)}
sparameter_path = {q(sparameter_path)}
mesh_report_path = {q(mesh_report_path)}
created_objects = {json.dumps(created)}
warnings = []
setup_created = False
solved = False
port_count = 0
excitations = []

def write_status(status, error=None):
    data = {{
        "status": status,
        "error": error,
        "project": project_path,
        "design": design_name,
        "objects": created_objects,
        "warnings": warnings,
        "setup_created": setup_created,
        "solved": solved,
        "sparameters": sparameter_path if os.path.exists(sparameter_path) else None,
        "mesh_report": mesh_report_path if os.path.exists(mesh_report_path) else None,
        "port_count": port_count,
        "excitations": excitations,
    }}
    handle = open(status_path, "w")
    try:
        handle.write(json.dumps(data, indent=2, sort_keys=True))
    finally:
        handle.close()

def material_value(name):
    return '"' + name + '"'

def attrs(name, material, color, transparency, solve_inside):
    return [
        "NAME:Attributes",
        "Name:=", name,
        "Flags:=", "",
        "Color:=", color,
        "Transparency:=", transparency,
        "PartCoordinateSystem:=", "Global",
        "UDMId:=", "",
        "MaterialValue:=", material_value(material),
        "SurfaceMaterialValue:=", '""',
        "SolveInside:=", solve_inside,
        "ShellElement:=", False,
        "ShellElementThickness:=", "0mm",
        "IsMaterialEditable:=", True,
        "UseMaterialAppearance:=", False,
        "IsLightweight:=", False,
    ]

def create_box(name, position, size, material, color, transparency, solve_inside):
    oEditor.CreateBox(
        [
            "NAME:BoxParameters",
            "XPosition:=", position[0],
            "YPosition:=", position[1],
            "ZPosition:=", position[2],
            "XSize:=", size[0],
            "YSize:=", size[1],
            "ZSize:=", size[2],
        ],
        attrs(name, material, color, transparency, solve_inside),
    )

def create_rectangle(name, start, width, height, material, color, transparency, solve_inside):
    oEditor.CreateRectangle(
        [
            "NAME:RectangleParameters",
            "XStart:=", start[0],
            "YStart:=", start[1],
            "ZStart:=", start[2],
            "Width:=", width,
            "Height:=", height,
            "WhichAxis:=", "Z",
        ],
        attrs(name, material, color, transparency, solve_inside),
    )

def create_circle(name, center, radius, material, color, transparency, solve_inside):
    oEditor.CreateCircle(
        [
            "NAME:CircleParameters",
            "XCenter:=", center[0],
            "YCenter:=", center[1],
            "ZCenter:=", center[2],
            "Radius:=", radius,
            "WhichAxis:=", "Z",
            "NumSegments:=", "0",
        ],
        attrs(name, material, color, transparency, solve_inside),
    )

def create_cylinder(name, center, radius, height, material, color, transparency, solve_inside):
    oEditor.CreateCylinder(
        [
            "NAME:CylinderParameters",
            "XCenter:=", center[0],
            "YCenter:=", center[1],
            "ZCenter:=", center[2],
            "Radius:=", radius,
            "Height:=", height,
            "WhichAxis:=", "Z",
            "NumSides:=", "0",
        ],
        attrs(name, material, color, transparency, solve_inside),
    )

try:
    ScriptEnv.Initialize("Ansoft.ElectronicsDesktop")
    oDesktop.NewProject()
    oProject = oDesktop.GetActiveProject()
    oProject.InsertDesign("HFSS", design_name, "DrivenTerminal", "")
    oDesign = oProject.SetActiveDesign(design_name)
    oEditor = oDesign.SetActiveEditor("3D Modeler")
    oEditor.SetModelUnits(["NAME:Units Parameter", "Units:=", {q(units)}, "Rescale:=", False])

    create_box(
        "substrate_FR4",
        [{q(expr(-gw / 2))}, {q(expr(-gl / 2))}, {q(expr(0))}],
        [{q(expr(gw))}, {q(expr(gl))}, {q(expr(h))}],
        "FR4_epoxy",
        "(143 175 143)",
        0.35,
        True,
    )
    create_rectangle(
        "ground_copper",
        [{q(expr(-gw / 2))}, {q(expr(-gl / 2))}, {q(expr(0))}],
        {q(expr(gw))},
        {q(expr(gl))},
        "pec",
        "(255 128 65)",
        0,
        False,
    )
    create_rectangle(
        "patch_copper",
        [{q(expr(-pw / 2))}, {q(expr(-pl / 2))}, {q(expr(h))}],
        {q(expr(pw))},
        {q(expr(pl))},
        "pec",
        "(255 128 65)",
        0,
        False,
    )
    create_circle(
        "ground_void",
        [{q(expr(feed_x))}, {q(expr(feed_y))}, {q(expr(0))}],
        {q(expr(coax_outer))},
        "vacuum",
        "(128 128 128)",
        0.5,
        True,
    )
    try:
        oEditor.Subtract(
            ["NAME:Selections", "Blank Parts:=", "ground_copper", "Tool Parts:=", "ground_void"],
            ["NAME:SubtractParameters", "KeepOriginals:=", False],
        )
    except Exception as exc:
        warnings.append("Ground coax hole subtraction skipped: " + str(exc))

    create_cylinder(
        "feed_pin",
        [{q(expr(feed_x))}, {q(expr(feed_y))}, {q(expr(0))}],
        {q(expr(coax_inner))},
        {q(expr(h))},
        "pec",
        "(255 128 65)",
        0,
        False,
    )
    create_cylinder(
        "coax_pin",
        [{q(expr(feed_x))}, {q(expr(feed_y))}, {q(expr(-feed_len))}],
        {q(expr(coax_inner))},
        {q(expr(feed_len))},
        "pec",
        "(255 128 65)",
        0,
        False,
    )
    create_cylinder(
        "coax",
        [{q(expr(feed_x))}, {q(expr(feed_y))}, {q(expr(-feed_len))}],
        {q(expr(coax_outer))},
        {q(expr(feed_len))},
        "Teflon (tm)",
        "(128 255 255)",
        0.4,
        True,
    )
    try:
        oEditor.Subtract(
            ["NAME:Selections", "Blank Parts:=", "coax", "Tool Parts:=", "coax_pin"],
            ["NAME:SubtractParameters", "KeepOriginals:=", True],
        )
    except Exception as exc:
        warnings.append("Coax dielectric pin subtraction skipped: " + str(exc))
    create_cylinder(
        "port_cap",
        [{q(expr(feed_x))}, {q(expr(feed_y))}, {q(expr(-feed_len))}],
        {q(expr(coax_outer))},
        {q(expr(-port_cap_thickness))},
        "pec",
        "(132 132 193)",
        0,
        False,
    )
    create_circle(
        "port1",
        [{q(expr(feed_x))}, {q(expr(feed_y))}, {q(expr(-feed_len))}],
        {q(expr(coax_outer))},
        "air",
        "(128 0 0)",
        0.65,
        True,
    )
    create_box(
        "air_region",
        [{q(expr(-gw / 2 - air_margin))}, {q(expr(-gl / 2 - air_margin))}, {q(expr(air_z0))}],
        [{q(expr(gw + 2 * air_margin))}, {q(expr(gl + 2 * air_margin))}, {q(expr(air_z_size))}],
        "air",
        "(128 128 255)",
        0.85,
        True,
    )
    try:
        oEditor.ChangeProperty([
            "NAME:AllTabs",
            [
                "NAME:Geometry3DAttributeTab",
                ["NAME:PropServers", "air_region"],
                ["NAME:ChangedProps", ["NAME:Display Wireframe", "Value:=", True]],
            ],
        ])
    except Exception as exc:
        warnings.append("Unable to set air_region wireframe: " + str(exc))

    try:
        oModule = oDesign.GetModule("BoundarySetup")
        try:
            oModule.AssignPerfectE(["NAME:antennaMetal", "Objects:=", ["patch_copper", "feed_pin", "coax_pin", "port_cap"], "InfGroundPlane:=", False])
        except Exception as exc:
            warnings.append("PerfectE assignment for antenna/feed metal skipped: " + str(exc))
        try:
            oModule.AssignPerfectE(["NAME:groundMetal", "Objects:=", ["ground_copper"], "InfGroundPlane:=", False])
        except Exception as exc:
            warnings.append("PerfectE assignment for ground skipped: " + str(exc))
        try:
            face_id = oEditor.GetFaceByPosition([
                "NAME:FaceParameters",
                "BodyName:=", "coax",
                "XPosition:=", {q(expr(feed_x + coax_outer))},
                "YPosition:=", {q(expr(feed_y))},
                "ZPosition:=", {q(expr(-feed_len / 2))},
            ])
            oModule.AssignPerfectE(["NAME:coax_outer", "Faces:=", [face_id], "InfGroundPlane:=", False])
        except Exception as exc:
            warnings.append("PerfectE assignment for coax outer shield skipped: " + str(exc))
        oModule.AssignRadiation([
            "NAME:Rad_air_region",
            "Objects:=", ["air_region"],
            "IsIncidentField:=", False,
            "IsEnforcedField:=", False,
            "IsFssReference:=", False,
            "IsForPML:=", False,
        ])
    except Exception as exc:
        warnings.append("Radiation boundary assignment skipped: " + str(exc))

    try:
        oModule = oDesign.GetModule("BoundarySetup")
        face_ids = oEditor.GetFaceIDs("port1")
        if len(face_ids) > 0:
            try:
                oModule.AutoIdentifyPorts(["NAME:Faces", face_ids[0]], True, ["NAME:ReferenceConductors", "port_cap"], "1", True)
            except Exception:
                oModule.AutoIdentifyPorts(["NAME:Faces", face_ids[0]], ["NAME:ReferenceConductors", "port_cap"], "1", True)
        else:
            warnings.append("No face IDs found for port1; terminal wave port was not assigned.")
        try:
            excitations = list(oModule.GetExcitations())
            port_count = 1 if len(excitations) > 0 else 0
        except Exception as exc:
            warnings.append("Unable to query excitation list: " + str(exc))
            port_count = 1
    except Exception as exc:
        warnings.append("Terminal wave port assignment failed: " + str(exc))

    if {create_setup!r}:
        try:
            oModule = oDesign.GetModule("AnalysisSetup")
            oModule.InsertSetup("HfssDriven", [
                "NAME:Setup1",
                "Frequency:=", {q(str(f0) + "GHz")},
                "AdaptMultipleFreqs:=", False,
                "PortsOnly:=", False,
                "MaxDeltaS:=", 0.02,
                "UseMatrixConv:=", False,
                "MaximumPasses:=", {max_passes},
                "MinimumPasses:=", 1,
                "MinimumConvergedPasses:=", 1,
                "PercentRefinement:=", 30,
                "IsEnabled:=", True,
                "BasisOrder:=", 1,
                "DoLambdaRefine:=", True,
                "DoMaterialLambda:=", True,
                "SetLambdaTarget:=", False,
                "Target:=", 0.3333,
                "UseMaxTetIncrease:=", False,
                "PortAccuracy:=", 2,
                "UseABCOnPort:=", False,
                "SetPortMinMaxTri:=", False,
                "UseDomains:=", False,
                "UseIterativeSolver:=", False,
                "SaveRadFieldsOnly:=", False,
                "SaveAnyFields:=", True,
                "UseMLFMM:=", False,
                "LambdaTargetForIESolver:=", 0.15,
                "UseDefaultLambdaTgtForIESolver:=", True,
            ])
            oModule.InsertFrequencySweep("Setup1", [
                "NAME:Sweep1",
                "IsEnabled:=", True,
                "RangeType:=", "LinearCount",
                "RangeStart:=", {q(str(start) + "GHz")},
                "RangeEnd:=", {q(str(stop) + "GHz")},
                "RangeCount:=", {points},
                "Type:=", {q(sweep_type)},
                "SaveFields:=", False,
                "SaveRadFields:=", False,
                "InterpTolerance:=", 0.5,
                "InterpMaxSolns:=", 250,
                "InterpMinSolns:=", 0,
                "InterpMinSubranges:=", 1,
                "ExtrapToDC:=", False,
            ])
            setup_created = True
        except Exception as exc:
            warnings.append("Setup/sweep creation skipped: " + str(exc))
    else:
        warnings.append("Setup/sweep creation skipped because create_setup=false and solve=false.")

    try:
        oProject.SaveAs(project_path, True)
    except Exception as exc:
        warnings.append("SaveAs warning: " + str(exc))

    if {solve_requested!r}:
        if not setup_created:
            raise RuntimeError("solve=true but Setup1/Sweep1 was not created")
        if port_count <= 0:
            raise RuntimeError("solve=true but no HFSS excitation/port was detected")
        oDesign.Analyze("Setup1")
        solved = True
        export_ok = False
        export_errors = []
        try:
            oModule = oDesign.GetModule("Solutions")
            oModule.ExportNetworkData(["All"], ["Setup1:Sweep1"], 3, sparameter_path, ["All"], True, 50, "S", -1, 0, 15, True, False, False)
            export_ok = os.path.exists(sparameter_path)
        except Exception as exc:
            export_errors.append(str(exc))
        if not export_ok:
            try:
                oModule = oDesign.GetModule("Solutions")
                oModule.ExportNetworkData([], ["Setup1:Sweep1"], 3, sparameter_path, ["All"], True, 50, "S", -1, 0, 15, True, False, False)
                export_ok = os.path.exists(sparameter_path)
            except Exception as exc:
                export_errors.append(str(exc))
        if not export_ok:
            warnings.append("S-parameter Touchstone export failed: " + " | ".join(export_errors))
        try:
            mesh_data = {{
                "status": "solved",
                "setup": "Setup1",
                "sweep": "Sweep1",
                "maximum_passes": {max_passes},
                "note": "Detailed adaptive mesh extraction is not implemented in the native script backend yet.",
            }}
            handle = open(mesh_report_path, "w")
            try:
                handle.write(json.dumps(mesh_data, indent=2, sort_keys=True))
            finally:
                handle.close()
        except Exception as exc:
            warnings.append("Mesh report write skipped: " + str(exc))
        try:
            oProject.Save()
        except Exception as exc:
            warnings.append("Project save after solve warning: " + str(exc))

    write_status("ok")
except Exception as exc:
    warnings.append(traceback.format_exc())
    write_status("fail", str(exc))
    raise
'''


def _normalize_probe_feed_geometry(geometry: dict[str, Any], warnings: list[str]) -> None:
    pl = float(geometry.get("patch_length_mm", 29.0))
    outer = float(geometry.get("coax_outer_radius_mm", 1.7))
    default_feed_y = min(8.0, max(0.0, pl / 2 - 1.2 * outer))
    feed_y = float(geometry.get("feed_y_offset_mm", default_feed_y))
    max_abs_feed_y = max(0.0, pl / 2 - 1.2 * outer)
    if abs(feed_y) > max_abs_feed_y:
        clamped = math.copysign(max_abs_feed_y, feed_y)
        warnings.append(
            f"Clamped feed_y_offset_mm from {feed_y:.6g} to {clamped:.6g} so the coax probe stays inside the patch."
        )
        feed_y = clamped
    geometry["feed_y_offset_mm"] = feed_y
    geometry["feed_x_offset_mm"] = float(geometry.get("feed_x_offset_mm", 0.0))
    geometry["coax_inner_radius_mm"] = float(geometry.get("coax_inner_radius_mm", 0.5))
    geometry["coax_outer_radius_mm"] = outer
    geometry["coax_feed_length_mm"] = float(geometry.get("coax_feed_length_mm", 12.0))
    geometry["port_cap_thickness_mm"] = float(geometry.get("port_cap_thickness_mm", 1.0))


def _parse_touchstone_metrics(path: Path, f0_ghz: float | None = None) -> dict[str, Any]:
    rows: list[tuple[float, float]] = []
    unit = "GHZ"
    data_format = "MA"
    unit_scale_to_ghz = {
        "HZ": 1e-9,
        "KHZ": 1e-6,
        "MHZ": 1e-3,
        "GHZ": 1.0,
    }
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("!"):
                continue
            if text.startswith("#"):
                tokens = text[1:].upper().split()
                if tokens:
                    unit = tokens[0]
                for candidate in ("DB", "MA", "RI"):
                    if candidate in tokens:
                        data_format = candidate
                        break
                continue
            parts = text.split()
            if len(parts) < 3:
                continue
            try:
                freq_ghz = float(parts[0]) * unit_scale_to_ghz.get(unit, 1.0)
                if data_format == "DB":
                    s11_db = float(parts[1])
                elif data_format == "RI":
                    s11_db = 20.0 * math.log10(max(math.hypot(float(parts[1]), float(parts[2])), 1e-300))
                else:
                    s11_db = 20.0 * math.log10(max(abs(float(parts[1])), 1e-300))
                rows.append((freq_ghz, s11_db))
            except ValueError:
                continue
    if not rows:
        return {"touchstone_path": str(path), "point_count": 0}
    min_row = min(rows, key=lambda item: item[1])
    metrics: dict[str, Any] = {
        "touchstone_path": str(path),
        "touchstone_format": data_format,
        "point_count": len(rows),
        "sweep_start_ghz": rows[0][0],
        "sweep_stop_ghz": rows[-1][0],
        "min_s11_db": min_row[1],
        "min_s11_freq_ghz": min_row[0],
    }
    if f0_ghz is not None:
        nearest = min(rows, key=lambda item: abs(item[0] - f0_ghz))
        metrics["s11_db_at_f0"] = nearest[1]
        metrics["s11_f0_nearest_ghz"] = nearest[0]
    return metrics


def _parse_touchstone_network_metrics(path: Path, f0_ghz: float | None = None) -> dict[str, Any]:
    rows: list[tuple[float, float, float]] = []
    unit = "GHZ"
    data_format = "MA"
    unit_scale_to_ghz = {
        "HZ": 1e-9,
        "KHZ": 1e-6,
        "MHZ": 1e-3,
        "GHZ": 1.0,
    }
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("!"):
                continue
            if text.startswith("#"):
                tokens = text[1:].upper().split()
                if tokens:
                    unit = tokens[0]
                for candidate in ("DB", "MA", "RI"):
                    if candidate in tokens:
                        data_format = candidate
                        break
                continue
            parts = text.split()
            if len(parts) < 5:
                continue
            try:
                freq_ghz = float(parts[0]) * unit_scale_to_ghz.get(unit, 1.0)
                s11_db = _touchstone_pair_to_db(float(parts[1]), float(parts[2]), data_format)
                s21_db = _touchstone_pair_to_db(float(parts[3]), float(parts[4]), data_format)
                rows.append((freq_ghz, s11_db, s21_db))
            except ValueError:
                continue
    if not rows:
        return {"touchstone_path": str(path), "point_count": 0}
    min_s11 = min(rows, key=lambda item: item[1])
    max_s21 = max(rows, key=lambda item: item[2])
    metrics: dict[str, Any] = {
        "touchstone_path": str(path),
        "touchstone_format": data_format,
        "point_count": len(rows),
        "sweep_start_ghz": rows[0][0],
        "sweep_stop_ghz": rows[-1][0],
        "min_s11_db": min_s11[1],
        "min_s11_freq_ghz": min_s11[0],
        "max_s21_db": max_s21[2],
        "max_s21_freq_ghz": max_s21[0],
    }
    if f0_ghz is not None:
        nearest = min(rows, key=lambda item: abs(item[0] - f0_ghz))
        metrics["s11_db_at_f0"] = nearest[1]
        metrics["s21_db_at_f0"] = nearest[2]
        metrics["f0_nearest_ghz"] = nearest[0]
    return metrics


def _touchstone_pair_to_db(first: float, second: float, data_format: str) -> float:
    if data_format == "DB":
        return first
    if data_format == "RI":
        return 20.0 * math.log10(max(math.hypot(first, second), 1e-300))
    return 20.0 * math.log10(max(abs(first), 1e-300))


def _create_fss_geometry(hfss: Any, geometry: dict[str, Any], created: list[str], warnings: list[str]) -> None:
    b = float(geometry["period_mm"])
    d = float(geometry["substrate_h_mm"])
    sw = min(float(geometry.get("substrate_width_mm", geometry["strip_width_mm"])), b - 0.05)
    w = float(geometry["strip_width_mm"])
    total_l = float(geometry["total_length_mm"])
    copper_t = float(geometry["copper_thickness_mm"])
    via_r = float(geometry["via_diameter_mm"]) / 2.0
    plate_l = float(geometry["plate_thickness_mm"])
    plate_size = min(float(geometry["plate_size_mm"]), b - 0.05)
    plate_front_gap = float(geometry["plate_front_gap_mm"])
    air_pad = float(geometry["air_padding_mm"])
    z_min = -total_l / 2.0 - air_pad
    z_span = total_l + 2.0 * air_pad

    _ensure_material(hfss, "Rogers_4230", float(geometry["substrate_er"]), float(geometry["substrate_loss_tangent"]), warnings)
    topology = str(geometry.get("cell_topology", "cross_centered")).lower()

    def box(position: list[float], size: list[float], name: str, material: str) -> Any:
        obj = hfss.modeler.create_box(position, size, name=name, material=material)
        created.append(name)
        return obj

    def cyl(orientation: str, origin: list[float], radius: float, height: float, name: str, material: str) -> Any:
        obj = hfss.modeler.create_cylinder(orientation, origin, radius, height, name=name, material=material)
        created.append(name)
        return obj

    def sheet(orientation: str, origin: list[float], size: list[float], name: str, material: str = "pec") -> Any:
        obj = hfss.modeler.create_rectangle(orientation, origin, size, name=name, material=material)
        created.append(name)
        return obj

    air = box([-b / 2.0, -b / 2.0, z_min], [b, b, z_span], "unit_air_cell", "air")
    try:
        air.display_wireframe = True
        air.transparency = 0.88
    except Exception as exc:
        warnings.append(f"Unable to set unit_air_cell display attributes: {exc}")

    plate_z = -total_l / 2.0 + plate_front_gap
    if topology == "corner_lattice":
        x_dspsl = -b / 2.0 + d / 2.0
        y_dspsl = -b / 2.0 + d / 2.0
        x_plate_min = -b / 2.0 + d
        y_plate_min = -b / 2.0 + d
        edge_clearance = float(geometry.get("edge_clearance_mm", 0.05))
        plate_xy = min(plate_size, b - d)
        box(
            [-b / 2.0 + edge_clearance, y_dspsl - d / 2.0, -total_l / 2.0],
            [b - 2.0 * edge_clearance, d, total_l],
            "substrate_xz_Rogers4230",
            "Rogers_4230",
        )
        box(
            [x_dspsl - d / 2.0, -b / 2.0 + edge_clearance, -total_l / 2.0],
            [d, b - 2.0 * edge_clearance, total_l],
            "substrate_yz_Rogers4230",
            "Rogers_4230",
        )
        inner_x = x_dspsl + d / 2.0
        inner_y = y_dspsl + d / 2.0
        plate_xy = min(b - d, max(0.5, b / 2.0 + plate_size / 2.0))
        sheet("XZ", [-w / 2.0, inner_y, -total_l / 2.0], [w, total_l], "strip_xz_ypos_copper")
        sheet("XZ", [-w / 2.0, -b / 2.0 + edge_clearance, -total_l / 2.0], [w, total_l], "strip_xz_yneg_copper")
        sheet("YZ", [inner_x, -w / 2.0, -total_l / 2.0], [w, total_l], "strip_yz_xpos_copper")
        sheet("YZ", [-b / 2.0 + edge_clearance, -w / 2.0, -total_l / 2.0], [w, total_l], "strip_yz_xneg_copper")
        cyl("Y", [0.0, -b / 2.0 + edge_clearance, 0.0], via_r, d, "via_y_copper", "copper")
        cyl("X", [-b / 2.0 + edge_clearance, 0.0, 0.0], via_r, d, "via_x_copper", "copper")
        box([inner_x, inner_y, plate_z], [plate_xy, plate_xy, plate_l], "inserted_plate_copper", "copper")
        warnings.append("corner_lattice topology uses PEC sheet strip conductors to avoid artificial thin-box intersections.")
    else:
        box([-sw / 2.0, -d / 2.0, -total_l / 2.0], [sw, d, total_l], "substrate_xz_Rogers4230", "Rogers_4230")
        box([-d / 2.0, -sw / 2.0, -total_l / 2.0], [d, sw, total_l], "substrate_yz_Rogers4230", "Rogers_4230")

        box([-w / 2.0, d / 2.0, -total_l / 2.0], [w, copper_t, total_l], "strip_xz_ypos_copper", "copper")
        box([-w / 2.0, -d / 2.0 - copper_t, -total_l / 2.0], [w, copper_t, total_l], "strip_xz_yneg_copper", "copper")
        box([d / 2.0, -w / 2.0, -total_l / 2.0], [copper_t, w, total_l], "strip_yz_xpos_copper", "copper")
        box([-d / 2.0 - copper_t, -w / 2.0, -total_l / 2.0], [copper_t, w, total_l], "strip_yz_xneg_copper", "copper")

        cyl("Y", [0.0, -d / 2.0 - copper_t, 0.0], via_r, d + 2.0 * copper_t, "via_y_copper", "copper")
        cyl("X", [-d / 2.0 - copper_t, 0.0, 0.0], via_r, d + 2.0 * copper_t, "via_x_copper", "copper")
        box([-plate_size / 2.0, -plate_size / 2.0, plate_z], [plate_size, plate_size, plate_l], "inserted_plate_copper", "copper")

    if bool(geometry.get("include_stubs", False)):
        stub_l = float(geometry["stub_length_mm"])
        stub_w = float(geometry["stub_width_mm"])
        stub_z = plate_z + plate_l / 2.0 - stub_l / 2.0
        box([-stub_w / 2.0, d / 2.0 + copper_t, stub_z], [stub_w, copper_t, stub_l], "tm_stub_ypos_copper", "copper")
        box([d / 2.0 + copper_t, -stub_w / 2.0, stub_z], [copper_t, stub_w, stub_l], "tm_stub_xpos_copper", "copper")

    if bool(geometry.get("boolean_cleanup", True)):
        _cleanup_fss_intersections(hfss, geometry, warnings)

    if bool(geometry.get("subtract_air_cell", False)):
        solids_to_subtract = [name for name in created if name != "unit_air_cell"]
        try:
            hfss.modeler.subtract("unit_air_cell", solids_to_subtract, keep_originals=True)
        except Exception as exc:
            warnings.append(f"Air cell subtraction skipped; overlapping air/solids may remain: {exc}")
    else:
        warnings.append(
            "Air cell was kept intact so periodic side faces remain available for lattice pairs; "
            "this mirrors HFSS region-style enclosure for the first FSS replication pass."
        )


def _cleanup_fss_intersections(hfss: Any, geometry: dict[str, Any], warnings: list[str]) -> None:
    topology = str(geometry.get("cell_topology", "cross_centered")).lower()
    if topology == "corner_lattice":
        substrate_name = "substrate_xz_Rogers4230"
        try:
            united_substrate = hfss.modeler.unite(
                ["substrate_xz_Rogers4230", "substrate_yz_Rogers4230"],
                keep_originals=False,
            )
            if isinstance(united_substrate, str) and united_substrate:
                substrate_name = united_substrate
        except Exception as exc:
            warnings.append(f"Corner topology substrate unite skipped: {exc}")
        try:
            hfss.modeler.subtract(
                substrate_name,
                [
                    "via_y_copper",
                    "via_x_copper",
                    "inserted_plate_copper",
                ],
                keep_originals=True,
            )
        except Exception as exc:
            warnings.append(f"Corner topology substrate clearance subtraction skipped: {exc}")
        return

    substrate_name = "substrate_xz_Rogers4230"
    copper_name = "strip_xz_ypos_copper"
    try:
        united_substrate = hfss.modeler.unite(
            ["substrate_xz_Rogers4230", "substrate_yz_Rogers4230"],
            keep_originals=False,
        )
        if isinstance(united_substrate, str) and united_substrate:
            substrate_name = united_substrate
    except Exception as exc:
        warnings.append(f"Substrate unite skipped: {exc}")
    try:
        united_copper = hfss.modeler.unite(
            [
                "strip_xz_ypos_copper",
                "strip_xz_yneg_copper",
                "strip_yz_xpos_copper",
                "strip_yz_xneg_copper",
                "via_y_copper",
                "via_x_copper",
                "inserted_plate_copper",
            ],
            keep_originals=False,
        )
        if isinstance(united_copper, str) and united_copper:
            copper_name = united_copper
    except Exception as exc:
        warnings.append(f"Conductor unite skipped: {exc}")
    try:
        hfss.modeler.subtract(
            substrate_name,
            copper_name,
            keep_originals=True,
        )
    except Exception as exc:
        warnings.append(f"Substrate conductor clearance subtraction skipped: {exc}")


def _ensure_material(hfss: Any, name: str, er: float, loss_tangent: float, warnings: list[str]) -> None:
    try:
        material = hfss.materials.add_material(name)
        material.permittivity = er
        material.dielectric_loss_tangent = loss_tangent
    except Exception as exc:
        try:
            material = hfss.materials[name]
            material.permittivity = er
            material.dielectric_loss_tangent = loss_tangent
        except Exception:
            warnings.append(f"Unable to create/update material {name}; falling back to AEDT material lookup if present: {exc}")


def _assign_fss_boundaries(
    hfss: Any,
    geometry: dict[str, Any],
    design: dict[str, Any],
    warnings: list[str],
) -> dict[str, int]:
    b = float(geometry["period_mm"])
    total_l = float(geometry["total_length_mm"])
    air_pad = float(geometry["air_padding_mm"])
    z_min = -total_l / 2.0 - air_pad
    z_max = total_l / 2.0 + air_pad
    units = "mm"

    def face(position: list[float], name: str) -> int:
        face_id = int(hfss.modeler.get_faceid_from_position(position, assignment="unit_air_cell", units=units))
        if face_id <= 0:
            raise RuntimeError(f"Unable to find {name} face at {position}.")
        return face_id

    face_ids = {
        "x_min": face([-b / 2.0, 0.0, 0.0], "x_min"),
        "x_max": face([b / 2.0, 0.0, 0.0], "x_max"),
        "y_min": face([0.0, -b / 2.0, 0.0], "y_min"),
        "y_max": face([0.0, b / 2.0, 0.0], "y_max"),
        "z_min": face([0.0, 0.0, z_min], "z_min"),
        "z_max": face([0.0, 0.0, z_max], "z_max"),
    }

    theta = float(design.get("theta_deg", 0.0))
    phi = float(design.get("phi_deg", 0.0))
    modes = int(design.get("floquet_modes", 1))
    try:
        hfss.assign_lattice_pair(
            [face_ids["x_min"], face_ids["x_max"]],
            phase_delay="UseScanAngle",
            phase_delay_param1=f"{phi}deg",
            phase_delay_param2=f"{theta}deg",
            name="Lattice_X",
        )
        hfss.assign_lattice_pair(
            [face_ids["y_min"], face_ids["y_max"]],
            phase_delay="UseScanAngle",
            phase_delay_param1=f"{phi}deg",
            phase_delay_param2=f"{theta}deg",
            name="Lattice_Y",
        )
    except Exception as exc:
        raise RuntimeError(f"Unable to assign FSS lattice pairs: {exc}") from exc

    try:
        hfss.create_floquet_port(
            face_ids["z_min"],
            lattice_origin=[-b / 2.0, -b / 2.0, z_min],
            lattice_a_end=[b / 2.0, -b / 2.0, z_min],
            lattice_b_end=[-b / 2.0, b / 2.0, z_min],
            modes=modes,
            name="Floquet_Zmin",
            renormalize=True,
        )
        hfss.create_floquet_port(
            face_ids["z_max"],
            lattice_origin=[-b / 2.0, -b / 2.0, z_max],
            lattice_a_end=[b / 2.0, -b / 2.0, z_max],
            lattice_b_end=[-b / 2.0, b / 2.0, z_max],
            modes=modes,
            name="Floquet_Zmax",
            renormalize=True,
        )
    except Exception as exc:
        raise RuntimeError(f"Unable to assign FSS Floquet ports: {exc}") from exc

    if modes != 1:
        warnings.append("Touchstone parser currently reports first-mode S11/S21 metrics; use modes=1 for Fig.3 TE baseline ranking.")
    return face_ids


def _try_create_fss_setup(
    hfss: Any,
    design: dict[str, Any],
    geometry: dict[str, Any],
    warnings: list[str],
) -> bool:
    try:
        setup = hfss.create_setup("Setup1")
        if hasattr(setup, "props"):
            setup.props["Frequency"] = f"{geometry['f0_ghz']}GHz"
            setup.props["MaximumPasses"] = int(design.get("maximum_passes", 1))
            setup.props["MinimumPasses"] = int(design.get("minimum_passes", 1))
            setup.props["MaxDeltaS"] = float(design.get("max_delta_s", 0.08))
        if hasattr(setup, "update"):
            setup.update()
        sweep = design.get("sweep") if isinstance(design.get("sweep"), dict) else {}
        if sweep and hasattr(hfss, "create_linear_count_sweep"):
            hfss.create_linear_count_sweep(
                setup="Setup1",
                unit="GHz",
                start_frequency=float(sweep.get("start_ghz", 4.0)),
                stop_frequency=float(sweep.get("stop_ghz", 18.0)),
                num_of_freq_points=int(sweep.get("points", 57)),
                name="Sweep1",
                save_fields=False,
                save_rad_fields=False,
                sweep_type=str(sweep.get("type", "Discrete")),
            )
        return True
    except Exception as exc:
        warnings.append(f"FSS setup/sweep creation failed: {exc}")
        return False


def _create_patch_geometry(hfss: Any, geometry: dict[str, float], created: list[str], warnings: list[str]) -> None:
    gw = geometry["ground_width_mm"]
    gl = geometry["ground_length_mm"]
    h = geometry["substrate_h_mm"]
    pw = geometry["patch_width_mm"]
    pl = geometry["patch_length_mm"]
    fw = geometry["feed_width_mm"]
    fl = geometry["feed_length_mm"]
    t = geometry["copper_thickness_mm"]
    air = geometry["air_height_mm"]

    def box(position: list[float], size: list[float], name: str, material: str) -> Any:
        obj = hfss.modeler.create_box(position, size, name=name, material=material)
        created.append(name)
        return obj

    box([-gw / 2, -gl / 2, 0], [gw, gl, h], "substrate_FR4", "FR4_epoxy")
    box([-gw / 2, -gl / 2, -t], [gw, gl, t], "ground_copper", "copper")
    box([-pw / 2, -pl / 2, h], [pw, pl, t], "patch_copper", "copper")
    feed_y = -pl / 2 - fl
    box([-fw / 2, feed_y, h], [fw, fl, t], "feedline_copper", "copper")
    box([-fw / 2, feed_y - 0.05, 0], [fw, 0.05, h], "feed_port_sheet", "vacuum")
    air_margin = max(air / 2, 10)
    box(
        [-gw / 2 - air_margin, -gl / 2 - air_margin, -air_margin],
        [gw + 2 * air_margin, gl + 2 * air_margin, h + air + air_margin],
        "air_region",
        "air",
    )
    try:
        hfss.modeler["air_region"].display_wireframe = True
    except Exception as exc:
        warnings.append(f"Unable to set air_region wireframe display: {exc}")


def _try_assign_patch_boundaries(hfss: Any, warnings: list[str]) -> None:
    try:
        if hasattr(hfss, "assign_radiation_boundary_to_objects"):
            hfss.assign_radiation_boundary_to_objects(["air_region"], "Rad_air_region")
        else:
            warnings.append("Radiation boundary helper not found; air region was created without boundary assignment.")
    except Exception as exc:
        warnings.append(f"Radiation boundary assignment skipped: {exc}")
    try:
        if hasattr(hfss, "lumped_port"):
            hfss.lumped_port(assignment="feed_port_sheet", reference="ground_copper", name="P1_feed_lumped")
        else:
            warnings.append("PyAEDT lumped_port helper not found; feed_port_sheet marks intended feed plane.")
    except Exception as exc:
        warnings.append(f"Lumped port assignment skipped: {exc}")


def _try_create_setup(hfss: Any, design: dict[str, Any], geometry: dict[str, float], warnings: list[str]) -> None:
    try:
        setup = hfss.create_setup("Setup1")
        if hasattr(setup, "props"):
            setup.props["Frequency"] = f"{geometry['f0_ghz']}GHz"
            setup.props["MaximumPasses"] = int(design.get("maximum_passes", 6))
        if hasattr(setup, "update"):
            setup.update()
        sweep = design.get("sweep") if isinstance(design.get("sweep"), dict) else {}
        if sweep and hasattr(hfss, "create_linear_count_sweep"):
            hfss.create_linear_count_sweep(
                setup="Setup1",
                units="GHz",
                start_frequency=float(sweep.get("start_ghz", 1.0)),
                stop_frequency=float(sweep.get("stop_ghz", 4.0)),
                num_of_freq_points=int(sweep.get("points", 101)),
                name="Sweep1",
                save_fields=False,
            )
    except Exception as exc:
        warnings.append(f"Setup/sweep creation skipped: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
