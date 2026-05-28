from __future__ import annotations

import csv
import contextlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _now() -> float:
    return round(time.time(), 6)


def _safe_name(value: str) -> str:
    allowed = []
    for char in value.strip() or "run":
        allowed.append(char if char.isalnum() or char in {"-", "_"} else "_")
    return "".join(allowed)[:80]


@dataclass
class AdapterResult:
    status: str
    software: str
    action: str
    artifacts: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "software": self.software,
            "action": self.action,
            "artifacts": dict(self.artifacts),
            "metrics": dict(self.metrics),
            "warnings": list(self.warnings),
            "error": self.error,
            "details": dict(self.details),
        }


class BaseAdapter:
    name = "base"

    def version(self) -> dict[str, Any]:
        return {"adapter": self.name, "available": True}

    def health_check(self) -> AdapterResult:
        return AdapterResult("ok", self.name, "health_check")

    def capabilities(self) -> dict[str, Any]:
        return {"actions": []}

    def prepare(self, input_data: dict[str, Any], workspace: Path) -> AdapterResult:
        return AdapterResult("ok", self.name, "prepare")

    def execute(
        self,
        action: str,
        payload: dict[str, Any],
        workspace: Path,
    ) -> AdapterResult:
        return AdapterResult("fail", self.name, action, error=f"unsupported action: {action}")

    def collect(self, workspace: Path) -> AdapterResult:
        return AdapterResult("ok", self.name, "collect")

    def shutdown(self) -> AdapterResult:
        return AdapterResult("ok", self.name, "shutdown")


class GeometryAdapter(BaseAdapter):
    name = "geometry"

    def capabilities(self) -> dict[str, Any]:
        return {
            "actions": ["prepare_geometry"],
            "outputs": ["geometry_manifest"],
        }

    def prepare(self, input_data: dict[str, Any], workspace: Path) -> AdapterResult:
        return self.execute("prepare_geometry", input_data, workspace)

    def execute(
        self,
        action: str,
        payload: dict[str, Any],
        workspace: Path,
    ) -> AdapterResult:
        if action != "prepare_geometry":
            return super().execute(action, payload, workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        project_name = _safe_name(str(design.get("project_name", "em_bridge_design")))
        units = str(design.get("units", design.get("model_units", "mm")))
        parameters = design.get("parameters") if isinstance(design.get("parameters"), dict) else {}
        sweep = design.get("sweep") if isinstance(design.get("sweep"), dict) else {}
        manifest = {
            "software": self.name,
            "created_at": _now(),
            "project_name": project_name,
            "units": units,
            "parameters": parameters,
            "sweep": sweep,
            "coordinate_system": design.get("coordinate_system", "global"),
            "reference_plane": design.get("reference_plane", "port_reference"),
            "geometry_revision": f"geom-{abs(hash(json.dumps(design, sort_keys=True, default=str))) % 10**12}",
        }
        path = workspace / "geometry_manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return AdapterResult(
            "ok",
            self.name,
            action,
            artifacts={"geometry_manifest": str(path)},
            metrics={"parameter_count": len(parameters)},
            details={"units": units, "geometry_revision": manifest["geometry_revision"]},
        )


class HfssAdapter(BaseAdapter):
    name = "hfss"

    def __init__(
        self,
        *,
        use_mock: bool = False,
        aedt_version: str | None = None,
        ansysedt_path: str | None = None,
        license_server: str | None = None,
        license_file: str | None = None,
        license_root: str | None = None,
        required_features: list[str] | None = None,
    ) -> None:
        self.use_mock = use_mock
        self.aedt_version = aedt_version or os.environ.get("AEDT_VERSION")
        self.ansysedt_path = ansysedt_path
        self.license_server = license_server or os.environ.get("ANSYSLMD_LICENSE_FILE")
        self.license_file = license_file
        self.license_root = license_root
        self.required_features = required_features or ["electronics3d_gui", "hfss_gui"]
        self._license_cache: dict[str, Any] | None = None

    def _pyaedt_module(self) -> str | None:
        for module in ("ansys.aedt.core", "pyaedt"):
            try:
                if importlib.util.find_spec(module):
                    return module
            except ModuleNotFoundError:
                continue
        return None

    def _ansysedt_path(self) -> str | None:
        configured = self.ansysedt_path or os.environ.get("ANSYSEDT_PATH")
        if configured and Path(configured).exists():
            return str(Path(configured))
        found = shutil.which("ansysedt")
        if found:
            return found
        candidates: list[Path] = []
        matches: list[Path] = []
        for drive in ("C", "D", "E", "F"):
            candidates.extend(
                [
                    Path(fr"{drive}:\Program Files\AnsysEM"),
                    Path(fr"{drive}:\Program Files\Ansys Inc"),
                    Path(fr"{drive}:\ansys\ansysEM"),
                    Path(fr"{drive}:\AnsysEM"),
                ]
            )
        for root in candidates:
            if not root.exists():
                continue
            matches.extend(root.rglob("ansysedt.exe"))
        if matches:
            return str(max(matches, key=self._aedt_exe_sort_key))
        return None

    def _aedt_exe_sort_key(self, path: Path) -> tuple[int, str]:
        version_id = 0
        for part in path.parts:
            match = re.fullmatch(r"v(\d{3})", part, flags=re.IGNORECASE)
            if match:
                version_id = int(match.group(1))
                break
        return (version_id, str(path))

    def _aedt_env_vars(self) -> dict[str, str]:
        pattern = re.compile(r"^(ANSYSEM_ROOT|ANSYSEM_PY_CLIENT_ROOT|ANSYSEMSV_ROOT|AWP_ROOT)\d{3}$")
        return {key: value for key, value in os.environ.items() if pattern.match(key)}

    def _version_from_id(self, version_id: str | None) -> str | None:
        if not version_id or not re.fullmatch(r"\d{3}", version_id):
            return None
        return f"20{version_id[:2]}.{version_id[2]}"

    def _aedt_installation(self) -> dict[str, Any]:
        ansysedt = self._ansysedt_path()
        env_vars = self._aedt_env_vars()
        install: dict[str, Any] = {
            "ansysedt_path": ansysedt,
            "configured_aedt_env_vars": sorted(env_vars),
            "current_process_environment_ready": bool(env_vars),
            "injected_env_var": None,
            "injected_env_value": None,
            "derived_aedt_version": None,
            "worker_environment_ready": bool(env_vars),
        }
        if not ansysedt:
            return install

        exe_path = Path(ansysedt)
        exe_dir = exe_path.parent
        version_id = None
        for part in exe_path.parts:
            match = re.fullmatch(r"v(\d{3})", part, flags=re.IGNORECASE)
            if match:
                version_id = match.group(1)
                break
        derived_version = self._version_from_id(version_id)
        env_var = f"ANSYSEM_ROOT{version_id}" if version_id else None
        install.update(
            {
                "ansysedt_path": str(exe_path),
                "aedt_exe_dir": str(exe_dir),
                "injected_env_var": env_var,
                "injected_env_value": str(exe_dir) if env_var else None,
                "derived_aedt_version": derived_version,
                "worker_environment_ready": bool(env_vars or env_var),
            }
        )
        return install

    def _license_root(self) -> Path | None:
        configured = self.license_root or os.environ.get("ANSYSLIC_DIR")
        if configured and Path(configured).exists():
            return Path(configured)
        install = self._aedt_installation()
        exe_dir = install.get("aedt_exe_dir")
        if isinstance(exe_dir, str) and exe_dir:
            candidate = Path(exe_dir).parents[1] / "Shared Files" / "Licensing"
            if candidate.exists():
                return candidate
        for drive in ("C", "D", "E", "F"):
            for candidate in (
                Path(fr"{drive}:\Program Files\AnsysEM\Shared Files\Licensing"),
                Path(fr"{drive}:\ansys\ansysEM\Shared Files\Licensing"),
                Path(fr"{drive}:\AnsysEM\Shared Files\Licensing"),
            ):
                if candidate.exists():
                    return candidate
        return None

    def _license_environment_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.license_file:
            values["ANSYSLMD_LICENSE_FILE"] = self.license_file
        if self.license_server:
            values.setdefault("ANSYSLMD_LICENSE_FILE", self.license_server)
        root = self._license_root()
        if root:
            values["ANSYSLIC_DIR"] = str(root)
            license_file = root / "license_files" / "ansyslmd.lic"
            if license_file.exists() and "ANSYSLMD_LICENSE_FILE" not in values:
                values["ANSYSLMD_LICENSE_FILE"] = str(license_file)
        return values

    def _preferred_server_arg(self) -> str | None:
        if not self.license_server:
            return None
        return self.license_server if re.fullmatch(r"\d+@[^\\/:]+", self.license_server) else None

    def _license_tool_path(self) -> str | None:
        install = self._aedt_installation()
        exe_dir = install.get("aedt_exe_dir")
        if isinstance(exe_dir, str) and exe_dir:
            candidate = Path(exe_dir) / "licensingclient" / "winx64" / "ansysli_util.exe"
            if candidate.exists():
                return str(candidate)
        found = shutil.which("ansysli_util")
        return found

    def _license_diagnostics(self) -> dict[str, Any]:
        if self._license_cache is not None:
            return dict(self._license_cache)
        tool = self._license_tool_path()
        env_values = self._license_environment_values()
        diagnostics: dict[str, Any] = {
            "tool": tool,
            "preferred_server": self._preferred_server_arg(),
            "configured_license_file": self.license_file,
            "environment": {
                key: os.environ.get(key) or env_values.get(key)
                for key in ("ANSYSLIC_DIR", "ANSYSLMD_LICENSE_FILE", "ANSYSLI_SERVERS")
                if os.environ.get(key) or env_values.get(key)
            },
            "checked_features": list(self.required_features),
            "status": "unknown",
            "feature_results": {},
        }
        if not tool:
            diagnostics["status"] = "unknown"
            diagnostics["error"] = "ansysli_util.exe was not found; license checkout could not be verified."
            self._license_cache = diagnostics
            return dict(diagnostics)
        env = self._worker_environment(include_license=False)
        env.update(env_values)
        overall_ok = True
        for feature in diagnostics["checked_features"]:
            command = [tool]
            preferred_server = self._preferred_server_arg()
            if preferred_server:
                command.extend(["-prefsvr", preferred_server])
            command.extend(["-checkout", feature, "-wait", "1"])
            try:
                completed = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=20,
                    env=env,
                )
                output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
                ok = completed.returncode == 0 and "CHECKOUT FAILED" not in output.upper()
                diagnostics["feature_results"][feature] = {
                    "ok": ok,
                    "returncode": completed.returncode,
                    "output_tail": output[-2000:],
                }
                overall_ok = overall_ok and ok
            except subprocess.TimeoutExpired:
                diagnostics["feature_results"][feature] = {
                    "ok": False,
                    "returncode": None,
                    "output_tail": "License checkout timed out.",
                }
                overall_ok = False
        diagnostics["status"] = "ok" if overall_ok else "blocked"
        self._license_cache = diagnostics
        return dict(diagnostics)

    def _worker_environment(self, *, include_license: bool = True) -> dict[str, str]:
        env = os.environ.copy()
        install = self._aedt_installation()
        ansysedt = install.get("ansysedt_path")
        exe_dir = install.get("aedt_exe_dir")
        env_var = install.get("injected_env_var")
        env_value = install.get("injected_env_value")
        version = self.aedt_version or install.get("derived_aedt_version")
        if isinstance(ansysedt, str) and ansysedt:
            env["ANSYSEDT_PATH"] = ansysedt
        if isinstance(env_var, str) and env_var and isinstance(env_value, str) and env_value:
            env[env_var] = env_value
            env["PYAEDT_DESKTOP_PATH"] = env_value
        if version:
            env["AEDT_VERSION"] = str(version)
        if isinstance(exe_dir, str) and exe_dir:
            env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")
        if include_license:
            for key, value in self._license_environment_values().items():
                env.setdefault(key, value)
        return env

    def version(self) -> dict[str, Any]:
        module = self._pyaedt_module()
        install = self._aedt_installation()
        startup_ready = bool((module and install["worker_environment_ready"]) or install["ansysedt_path"])
        license_diagnostics = self._license_diagnostics() if startup_ready and not self.use_mock else {}
        license_ready = license_diagnostics.get("status") == "ok" if license_diagnostics else False
        native_script_available = bool(install["ansysedt_path"])
        api_available = bool(startup_ready and (license_ready or native_script_available))
        return {
            "adapter": self.name,
            "available": bool(api_available or self.use_mock),
            "api_available": api_available,
            "license_checkout_available": license_ready,
            "native_script_available": native_script_available,
            "startup_environment_ready": startup_ready,
            "pyaedt_module": module,
            "ansysedt_path": install["ansysedt_path"],
            "aedt_version": self.aedt_version or install.get("derived_aedt_version"),
            "aedt_installation": install,
            "license_diagnostics": license_diagnostics,
            "mock_enabled": self.use_mock,
        }

    def health_check(self) -> AdapterResult:
        info = self.version()
        if self.use_mock:
            return AdapterResult(
                "degraded",
                self.name,
                "health_check",
                warnings=["mock backend enabled; results are not physical"],
                details=info,
            )
        if info["api_available"]:
            warnings = []
            installation = info.get("aedt_installation", {})
            if isinstance(installation, dict) and not installation.get("current_process_environment_ready"):
                warnings.append(
                    "No ANSYSEM_ROOTxxx/AWP_ROOTxxx was present in the parent process; "
                    "the bridge will inject the discovered AEDT root into the HFSS worker."
                )
            if not info.get("license_checkout_available"):
                warnings.append(
                    "Feature-level ansysli_util checkout did not pass; real availability will be proven by AEDT "
                    "native script execution and project artifacts."
                )
            return AdapterResult("ok", self.name, "health_check", warnings=warnings, details=info)
        if not info["pyaedt_module"]:
            return AdapterResult(
                "blocked",
                self.name,
                "health_check",
                error="PyAEDT is not importable. Install pyaedt before using the HFSS API.",
                details=info,
            )
        return AdapterResult(
            "blocked",
            self.name,
            "health_check",
            error=(
                "AEDT/HFSS startup or license checkout is not usable from this environment. "
                "Set ANSYSEDT_PATH/ANSYSEM_ROOTxxx and configure a valid HFSS/Electronics Desktop license."
            ),
            details=info,
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "actions": ["create_patch_antenna", "create_fss_unit_cell", "run_solver", "export_results"],
            "backends": ["ansysedt_script", "pyaedt", "ansysedt", "mock"],
            "real_execution_available": bool(self.version()["api_available"]),
        }

    def execute(
        self,
        action: str,
        payload: dict[str, Any],
        workspace: Path,
    ) -> AdapterResult:
        if action not in {"create_patch_antenna", "create_fss_unit_cell", "run_solver", "export_results"}:
            return super().execute(action, payload, workspace)
        if self.use_mock:
            return self._mock_run(payload, workspace, action)
        module = self._pyaedt_module()
        health = self.health_check()
        if action == "create_fss_unit_cell":
            if not module:
                return AdapterResult(
                    "blocked",
                    self.name,
                    action,
                    error="create_fss_unit_cell requires PyAEDT (ansys.aedt.core or pyaedt); native script fallback is not implemented for Floquet/lattice setup.",
                    details=self.version(),
                )
            if health.status == "blocked":
                health.action = action
                return health
            return self._pyaedt_create_fss_unit_cell(module, payload, workspace)
        if action == "create_patch_antenna" and self._aedt_installation().get("ansysedt_path"):
            return self._pyaedt_create_patch_antenna(module or "ansysedt_script", payload, workspace)
        if action == "create_patch_antenna" and health.status != "blocked":
            return self._pyaedt_create_patch_antenna(module or "ansysedt_script", payload, workspace)
        if not module or health.status == "blocked":
            health = self.health_check()
            health.action = action
            return health
        return self._pyaedt_run(module, payload, workspace)

    def _mock_run(self, payload: dict[str, Any], workspace: Path, action: str) -> AdapterResult:
        results = workspace / "results"
        results.mkdir(parents=True, exist_ok=True)
        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        sweep = design.get("sweep") if isinstance(design.get("sweep"), dict) else {}
        start = float(sweep.get("start_ghz", 1.0))
        stop = float(sweep.get("stop_ghz", 4.0))
        points = max(3, int(sweep.get("points", 31)))
        f0 = float(design.get("parameters", {}).get("f0_ghz", (start + stop) / 2))
        s1p = results / "mock_hfss.s1p"
        with s1p.open("w", encoding="utf-8") as handle:
            handle.write("! MOCK Touchstone generated by em_mcp_bridge\n")
            handle.write("# GHZ S DB R 50\n")
            for index in range(points):
                freq = start + (stop - start) * index / (points - 1)
                depth = -8.0 - 16.0 * math.exp(-((freq - f0) ** 2) / 0.08)
                handle.write(f"{freq:.9g} {depth:.6g} 0\n")
        mesh_report = results / "mesh_report.json"
        mesh_report.write_text(
            json.dumps(
                {
                    "mock": True,
                    "adaptive_passes": 3,
                    "converged": True,
                    "max_delta_s": 0.015,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        solver_report = workspace / "solver_report.json"
        solver_report.write_text(
            json.dumps(
                {
                    "software": self.name,
                    "backend": "mock",
                    "mock": True,
                    "status": "ok",
                    "created_at": _now(),
                    "sparameters": str(s1p),
                    "mesh_report": str(mesh_report),
                    "warning": "Mock HFSS output is for bridge/protocol testing only.",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return AdapterResult(
            "ok",
            self.name,
            action,
            artifacts={
                "solver_report": str(solver_report),
                "sparameters": str(s1p),
                "mesh_report": str(mesh_report),
            },
            metrics={"mock": True, "sweep_start_ghz": start, "sweep_stop_ghz": stop},
            warnings=["mock backend enabled; results are not physical"],
        )

    def _pyaedt_run(self, module: str, payload: dict[str, Any], workspace: Path) -> AdapterResult:
        # Keep real execution conservative. This bridge creates a project shell and records
        # environment evidence; domain geometry should be added by project-specific adapters.
        try:
            if module == "ansys.aedt.core":
                from ansys.aedt.core import Hfss  # type: ignore
            else:
                from pyaedt import Hfss  # type: ignore
        except Exception as exc:
            return AdapterResult("blocked", self.name, "run_solver", error=f"Unable to import PyAEDT: {exc}")

        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        project_name = _safe_name(str(design.get("project_name", "em_bridge_design")))
        project_path = workspace / f"{project_name}.aedt"
        log_path = workspace / "hfss_run_log.json"
        try:
            hfss = Hfss(
                project=str(project_path),
                design=str(design.get("design_name", "bridge_design")),
                solution_type=str(design.get("solution_type", "Modal")),
                version=self.aedt_version,
                non_graphical=True,
                new_desktop=True,
            )
        except TypeError:
            hfss = Hfss(
                projectname=str(project_path),
                designname=str(design.get("design_name", "bridge_design")),
                solution_type=str(design.get("solution_type", "Modal")),
                specified_version=self.aedt_version,
                non_graphical=True,
                new_desktop_session=True,
            )
        except Exception as exc:
            return AdapterResult("blocked", self.name, "run_solver", error=f"Unable to start HFSS through PyAEDT: {exc}")

        try:
            hfss.modeler.model_units = str(design.get("units", "mm"))
            # Project-specific geometry/ports are intentionally not guessed here.
            if hasattr(hfss, "save_project"):
                hfss.save_project()
            log = {
                "software": self.name,
                "backend": module,
                "status": "created_project_shell",
                "project": str(project_path),
                "note": "Bridge reached HFSS. Add project-specific geometry/setup before solve.",
            }
            log_path.write_text(json.dumps(log, indent=2, sort_keys=True), encoding="utf-8")
        except Exception as exc:
            return AdapterResult("fail", self.name, "run_solver", error=f"HFSS project setup failed: {exc}")
        finally:
            try:
                hfss.release_desktop()
            except Exception:
                pass

        solver_report = workspace / "solver_report.json"
        solver_report.write_text(
            json.dumps(
                {
                    "software": self.name,
                    "backend": module,
                    "status": "created_project_shell",
                    "project": str(project_path),
                    "log": str(log_path),
                    "requires_project_specific_setup": True,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return AdapterResult(
            "ok",
            self.name,
            "run_solver",
            artifacts={"hfss_project": str(project_path), "solver_report": str(solver_report), "solver_log": str(log_path)},
            warnings=["HFSS project shell created; no physical solve was run without project-specific geometry/setup."],
        )

    def _pyaedt_create_patch_antenna(
        self,
        module: str,
        payload: dict[str, Any],
        workspace: Path,
    ) -> AdapterResult:
        workspace.mkdir(parents=True, exist_ok=True)
        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        params = design.get("parameters") if isinstance(design.get("parameters"), dict) else {}
        units = str(design.get("units", "mm"))
        project_name = _safe_name(str(design.get("project_name", "patch_antenna")))
        project_path = workspace / f"{project_name}.aedt"
        report_path = workspace / "solver_report.json"
        log_path = workspace / "hfss_patch_antenna_log.json"

        geometry = {
            "f0_ghz": float(params.get("f0_ghz", 2.45)),
            "substrate_er": float(params.get("substrate_er", 4.4)),
            "substrate_h_mm": float(params.get("substrate_h_mm", 1.6)),
            "patch_width_mm": float(params.get("patch_width_mm", 37.2)),
            "patch_length_mm": float(params.get("patch_length_mm", 29.0)),
            "ground_width_mm": float(params.get("ground_width_mm", 80.0)),
            "ground_length_mm": float(params.get("ground_length_mm", 80.0)),
            "feed_width_mm": float(params.get("feed_width_mm", 3.0)),
            "feed_length_mm": float(params.get("feed_length_mm", 22.0)),
            "feed_x_offset_mm": float(params.get("feed_x_offset_mm", 0.0)),
            "feed_y_offset_mm": float(params.get("feed_y_offset_mm", 8.0)),
            "coax_inner_radius_mm": float(params.get("coax_inner_radius_mm", 0.5)),
            "coax_outer_radius_mm": float(params.get("coax_outer_radius_mm", 1.7)),
            "coax_feed_length_mm": float(params.get("coax_feed_length_mm", 12.0)),
            "port_cap_thickness_mm": float(params.get("port_cap_thickness_mm", 1.0)),
            "copper_thickness_mm": float(params.get("copper_thickness_mm", 0.035)),
            "air_height_mm": float(params.get("air_height_mm", 35.0)),
        }

        timeout_sec = int(design.get("hfss_timeout_sec", os.environ.get("HFSS_BRIDGE_TIMEOUT_SEC", 180)))
        installation = self._aedt_installation()
        aedt_version = self.aedt_version or installation.get("derived_aedt_version")
        worker_input = workspace / "hfss_worker_input.json"
        worker_output = workspace / "hfss_worker_output.json"
        worker_stderr = workspace / "hfss_worker_stderr.txt"
        worker_input.write_text(
            json.dumps(
                {
                    "module": module,
                    "execution_backend": str(design.get("hfss_backend", "ansysedt_script")),
                    "project_path": str(project_path),
                    "design_name": str(design.get("design_name", "patch_antenna")),
                    "solution_type": str(design.get("solution_type", "Modal")),
                    "version": aedt_version,
                    "non_graphical": bool(design.get("non_graphical", True)),
                    "units": units,
                    "geometry": geometry,
                    "design": design,
                    "aedt_installation": installation,
                    "log_path": str(log_path),
                    "report_path": str(report_path),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "em_mcp_bridge.hfss_worker",
                    str(worker_input),
                    str(worker_output),
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
                env=self._worker_environment(),
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            worker_stderr.write_text(
                (exc.stderr if isinstance(exc.stderr, str) else "")
                + f"\nHFSS worker timed out after {timeout_sec}s\n",
                encoding="utf-8",
            )
            return AdapterResult(
                "blocked",
                self.name,
                "create_patch_antenna",
                artifacts={"worker_input": str(worker_input), "worker_stderr": str(worker_stderr)},
                error=f"HFSS worker timed out after {timeout_sec}s while starting AEDT or creating geometry.",
                details={"project_path": str(project_path), "timeout_sec": timeout_sec, "aedt_installation": installation},
            )
        worker_stderr.write_text(completed.stderr or "", encoding="utf-8")
        if worker_output.exists():
            try:
                result = json.loads(worker_output.read_text(encoding="utf-8"))
                return AdapterResult(
                    str(result.get("status", "fail")),
                    self.name,
                    "create_patch_antenna",
                    artifacts=dict(result.get("artifacts", {})),
                    metrics=dict(result.get("metrics", {})),
                    warnings=[str(item) for item in result.get("warnings", [])],
                    error=result.get("error"),
                    details=dict(result.get("details", {})),
                )
            except Exception:
                pass
        if completed.returncode != 0:
            return AdapterResult(
                "blocked" if completed.returncode == 124 else "fail",
                self.name,
                "create_patch_antenna",
                artifacts={"worker_input": str(worker_input), "worker_stderr": str(worker_stderr)},
                error=f"HFSS worker failed with exit code {completed.returncode}.",
                details={"stdout": (completed.stdout or "")[-2000:], "stderr_path": str(worker_stderr)},
            )
        try:
            result = json.loads(worker_output.read_text(encoding="utf-8"))
        except Exception as exc:
            return AdapterResult(
                "fail",
                self.name,
                "create_patch_antenna",
                artifacts={"worker_input": str(worker_input), "worker_output": str(worker_output), "worker_stderr": str(worker_stderr)},
                error=f"HFSS worker output was not valid JSON: {exc}",
            )
        return AdapterResult(
            str(result.get("status", "fail")),
            self.name,
            "create_patch_antenna",
            artifacts=dict(result.get("artifacts", {})),
            metrics=dict(result.get("metrics", {})),
            warnings=[str(item) for item in result.get("warnings", [])],
            error=result.get("error"),
            details=dict(result.get("details", {})),
        )

    def _pyaedt_create_fss_unit_cell(
        self,
        module: str,
        payload: dict[str, Any],
        workspace: Path,
    ) -> AdapterResult:
        workspace.mkdir(parents=True, exist_ok=True)
        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        params = design.get("parameters") if isinstance(design.get("parameters"), dict) else {}
        units = str(design.get("units", "mm"))
        project_name = _safe_name(str(design.get("project_name", "fss_unit_cell")))
        project_path = workspace / f"{project_name}.aedt"
        report_path = workspace / "solver_report.json"
        log_path = workspace / "hfss_fss_unit_cell_log.json"

        b_mm = float(params.get("b_mm", params.get("period_mm", 8.0)))
        substrate_h_mm = float(params.get("substrate_h_mm", params.get("d_mm", 1.524)))
        total_length_mm = float(params.get("total_length_mm", params.get("l_mm", 12.0)))
        plate_front_gap_mm = float(params.get("plate_front_gap_mm", params.get("l3_mm", 4.65)))
        plate_back_gap_mm = float(params.get("plate_back_gap_mm", params.get("l4_mm", 3.05)))
        plate_thickness_mm = float(
            params.get(
                "plate_thickness_mm",
                params.get("lm_mm", max(0.1, total_length_mm - plate_front_gap_mm - plate_back_gap_mm)),
            )
        )
        plate_size_default = max(0.5, b_mm - 2.0 * substrate_h_mm)
        geometry = {
            "f0_ghz": float(params.get("f0_ghz", 7.5)),
            "period_mm": b_mm,
            "substrate_er": float(params.get("substrate_er", 3.0)),
            "substrate_loss_tangent": float(params.get("substrate_loss_tangent", params.get("tand", 0.0023))),
            "substrate_h_mm": substrate_h_mm,
            "substrate_width_mm": float(params.get("substrate_width_mm", params.get("w_mm", 4.0))),
            "strip_width_mm": float(params.get("strip_width_mm", params.get("w_mm", 4.0))),
            "cell_topology": str(params.get("cell_topology", "cross_centered")),
            "total_length_mm": total_length_mm,
            "plate_front_gap_mm": plate_front_gap_mm,
            "plate_back_gap_mm": plate_back_gap_mm,
            "plate_thickness_mm": plate_thickness_mm,
            "plate_size_mm": float(params.get("plate_size_mm", plate_size_default)),
            "via_diameter_mm": float(params.get("via_diameter_mm", params.get("D_mm", 1.7))),
            "copper_thickness_mm": float(params.get("copper_thickness_mm", 0.035)),
            "air_padding_mm": float(params.get("air_padding_mm", 4.0)),
            "stub_length_mm": float(params.get("stub_length_mm", 1.2)),
            "stub_width_mm": float(params.get("stub_width_mm", 0.2)),
            "include_stubs": bool(params.get("include_stubs", False)),
            "boolean_cleanup": bool(params.get("boolean_cleanup", True)),
            "subtract_air_cell": bool(params.get("subtract_air_cell", False)),
            "edge_clearance_mm": float(params.get("edge_clearance_mm", 0.05)),
        }

        timeout_sec = int(design.get("hfss_timeout_sec", os.environ.get("HFSS_BRIDGE_TIMEOUT_SEC", 900)))
        installation = self._aedt_installation()
        aedt_version = self.aedt_version or installation.get("derived_aedt_version")
        worker_input = workspace / "hfss_worker_input.json"
        worker_output = workspace / "hfss_worker_output.json"
        worker_stderr = workspace / "hfss_worker_stderr.txt"
        worker_input.write_text(
            json.dumps(
                {
                    "module": module,
                    "model_kind": "fss_unit_cell",
                    "execution_backend": "pyaedt",
                    "project_path": str(project_path),
                    "design_name": str(design.get("design_name", "fss_unit_cell")),
                    "solution_type": str(design.get("solution_type", "Modal")),
                    "version": aedt_version,
                    "non_graphical": bool(design.get("non_graphical", True)),
                    "units": units,
                    "geometry": geometry,
                    "design": design,
                    "aedt_installation": installation,
                    "log_path": str(log_path),
                    "report_path": str(report_path),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "em_mcp_bridge.hfss_worker",
                    str(worker_input),
                    str(worker_output),
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
                env=self._worker_environment(),
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            worker_stderr.write_text(
                (exc.stderr if isinstance(exc.stderr, str) else "")
                + f"\nHFSS worker timed out after {timeout_sec}s\n",
                encoding="utf-8",
            )
            return AdapterResult(
                "blocked",
                self.name,
                "create_fss_unit_cell",
                artifacts={"worker_input": str(worker_input), "worker_stderr": str(worker_stderr)},
                error=f"HFSS worker timed out after {timeout_sec}s while creating or solving the FSS unit cell.",
                details={"project_path": str(project_path), "timeout_sec": timeout_sec, "aedt_installation": installation},
            )
        worker_stderr.write_text(completed.stderr or "", encoding="utf-8")
        if worker_output.exists():
            try:
                result = json.loads(worker_output.read_text(encoding="utf-8"))
                return AdapterResult(
                    str(result.get("status", "fail")),
                    self.name,
                    "create_fss_unit_cell",
                    artifacts=dict(result.get("artifacts", {})),
                    metrics=dict(result.get("metrics", {})),
                    warnings=[str(item) for item in result.get("warnings", [])],
                    error=result.get("error"),
                    details=dict(result.get("details", {})),
                )
            except Exception:
                pass
        if completed.returncode != 0:
            return AdapterResult(
                "blocked" if completed.returncode == 124 else "fail",
                self.name,
                "create_fss_unit_cell",
                artifacts={"worker_input": str(worker_input), "worker_stderr": str(worker_stderr)},
                error=f"HFSS worker failed with exit code {completed.returncode}.",
                details={"stdout": (completed.stdout or "")[-2000:], "stderr_path": str(worker_stderr)},
            )
        try:
            result = json.loads(worker_output.read_text(encoding="utf-8"))
        except Exception as exc:
            return AdapterResult(
                "fail",
                self.name,
                "create_fss_unit_cell",
                artifacts={"worker_input": str(worker_input), "worker_output": str(worker_output), "worker_stderr": str(worker_stderr)},
                error=f"HFSS worker output was not valid JSON: {exc}",
            )
        return AdapterResult(
            str(result.get("status", "fail")),
            self.name,
            "create_fss_unit_cell",
            artifacts=dict(result.get("artifacts", {})),
            metrics=dict(result.get("metrics", {})),
            warnings=[str(item) for item in result.get("warnings", [])],
            error=result.get("error"),
            details=dict(result.get("details", {})),
        )

    def _create_patch_geometry(
        self,
        hfss: Any,
        geometry: dict[str, float],
        created: list[str],
        warnings: list[str],
    ) -> None:
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

    def _try_assign_patch_boundaries(
        self,
        hfss: Any,
        created: list[str],
        warnings: list[str],
    ) -> None:
        try:
            if hasattr(hfss, "assign_radiation_boundary_to_objects"):
                hfss.assign_radiation_boundary_to_objects(["air_region"], "Rad_air_region")
            elif hasattr(hfss, "assign_radiation_boundary_to_faces"):
                warnings.append("Radiation boundary helper requires faces; air region was created but boundary assignment was skipped.")
            else:
                warnings.append("PyAEDT radiation boundary helper not found; air region was created without boundary assignment.")
        except Exception as exc:
            warnings.append(f"Radiation boundary assignment skipped: {exc}")

        try:
            if hasattr(hfss, "lumped_port"):
                hfss.lumped_port(
                    assignment="feed_port_sheet",
                    reference="ground_copper",
                    name="P1_feed_lumped",
                )
            else:
                warnings.append("PyAEDT lumped_port helper not found; feed_port_sheet object marks the intended feed plane.")
        except Exception as exc:
            warnings.append(f"Lumped port assignment skipped: {exc}")

    def _try_create_setup(
        self,
        hfss: Any,
        design: dict[str, Any],
        geometry: dict[str, float],
        warnings: list[str],
    ) -> None:
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


class PythonPostprocessAdapter(BaseAdapter):
    name = "python_postprocess"

    def capabilities(self) -> dict[str, Any]:
        return {
            "actions": ["parse_touchstone", "validate_results", "summarize"],
            "formats": ["s1p", "s2p", "csv", "json"],
        }

    def execute(
        self,
        action: str,
        payload: dict[str, Any],
        workspace: Path,
    ) -> AdapterResult:
        if action not in {"parse_touchstone", "validate_results", "summarize"}:
            return super().execute(action, payload, workspace)
        sparam_path = self._find_sparameter(payload, workspace)
        metrics: dict[str, Any] = {}
        warnings: list[str] = []
        f0_ghz = self._target_frequency_ghz(payload)
        if sparam_path:
            parsed = self._parse_touchstone(sparam_path, f0_ghz=f0_ghz)
            metrics.update(parsed)
        else:
            warnings.append("No Touchstone S-parameter file found.")
        validation = self._validate(metrics, payload)
        report_path = workspace / "validation_report.json"
        report_path.write_text(
            json.dumps(
                {
                    "software": self.name,
                    "created_at": _now(),
                    "status": validation["status"],
                    "metrics": metrics,
                    "checks": validation["checks"],
                    "warnings": warnings,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        status = validation["status"] if validation["status"] != "fail" else "fail"
        return AdapterResult(
            status,
            self.name,
            action,
            artifacts={"validation_report": str(report_path), **({"sparameters": str(sparam_path)} if sparam_path else {})},
            metrics=metrics,
            warnings=warnings,
        )

    def _find_sparameter(self, payload: dict[str, Any], workspace: Path) -> Path | None:
        explicit = payload.get("sparameters")
        if explicit:
            path = Path(str(explicit))
            if not path.is_absolute():
                path = workspace / path
            if path.exists():
                return path
        for pattern in ("*.s1p", "*.s2p", "*.s3p", "*.s*p"):
            found = list((workspace / "results").glob(pattern)) + list(workspace.glob(pattern))
            if found:
                return found[0]
        return None

    def _target_frequency_ghz(self, payload: dict[str, Any]) -> float | None:
        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        params = design.get("parameters") if isinstance(design.get("parameters"), dict) else {}
        for source in (params, design):
            if isinstance(source, dict) and "f0_ghz" in source:
                try:
                    return float(source["f0_ghz"])
                except (TypeError, ValueError):
                    return None
        return None

    def _parse_touchstone(self, path: Path, *, f0_ghz: float | None = None) -> dict[str, Any]:
        rows: list[tuple[float, float, float | None]] = []
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
                    s11_db = self._touchstone_pair_to_db(float(parts[1]), float(parts[2]), data_format)
                    s21_db = None
                    if len(parts) >= 5 and re.search(r"\.s[2-9]\d*p$", path.name, flags=re.IGNORECASE):
                        s21_db = self._touchstone_pair_to_db(float(parts[3]), float(parts[4]), data_format)
                    rows.append((freq_ghz, s11_db, s21_db))
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
        s21_rows = [row for row in rows if row[2] is not None]
        if s21_rows:
            max_s21 = max(s21_rows, key=lambda item: item[2] if item[2] is not None else -1e300)
            metrics["max_s21_db"] = max_s21[2]
            metrics["max_s21_freq_ghz"] = max_s21[0]
        if f0_ghz is not None:
            nearest = min(rows, key=lambda item: abs(item[0] - f0_ghz))
            metrics["s11_db_at_f0"] = nearest[1]
            metrics["s11_f0_nearest_ghz"] = nearest[0]
            if nearest[2] is not None:
                metrics["s21_db_at_f0"] = nearest[2]
                metrics["s21_f0_nearest_ghz"] = nearest[0]
        return metrics

    def _touchstone_pair_to_db(self, first: float, second: float, data_format: str) -> float:
        if data_format == "DB":
            return first
        if data_format == "RI":
            return 20.0 * math.log10(max(math.hypot(first, second), 1e-300))
        return 20.0 * math.log10(max(abs(first), 1e-300))

    def _validate(self, metrics: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        targets = design.get("targets") if isinstance(design.get("targets"), dict) else {}
        sweep = design.get("sweep") if isinstance(design.get("sweep"), dict) else {}
        f0_ghz = self._target_frequency_ghz(payload)
        checks: list[dict[str, Any]] = []
        if metrics.get("point_count", 0) <= 0:
            checks.append({"name": "sparameters_present", "passed": False, "reason": "No parseable S-parameter rows."})
        else:
            checks.append({"name": "sparameters_present", "passed": True})
        if sweep and metrics.get("point_count"):
            start_ok = metrics["sweep_start_ghz"] <= float(sweep.get("start_ghz", metrics["sweep_start_ghz"])) + 1e-9
            stop_ok = metrics["sweep_stop_ghz"] >= float(sweep.get("stop_ghz", metrics["sweep_stop_ghz"])) - 1e-9
            checks.append({"name": "frequency_coverage", "passed": bool(start_ok and stop_ok)})
            if f0_ghz is not None:
                checks.append(
                    {
                        "name": "target_frequency_covered",
                        "passed": bool(metrics["sweep_start_ghz"] <= f0_ghz <= metrics["sweep_stop_ghz"]),
                        "f0_ghz": f0_ghz,
                    }
                )
        if "s11_db_at_f0_max" in targets:
            threshold = float(targets["s11_db_at_f0_max"])
            if "s11_db_at_f0" in metrics:
                checks.append(
                    {
                        "name": "s11_at_f0_target",
                        "passed": metrics["s11_db_at_f0"] <= threshold,
                        "measured_s11_db_at_f0": metrics["s11_db_at_f0"],
                        "nearest_freq_ghz": metrics.get("s11_f0_nearest_ghz"),
                        "threshold_db": threshold,
                    }
                )
            else:
                checks.append(
                    {
                        "name": "s11_at_f0_target",
                        "passed": False,
                        "reason": "Target frequency was not provided or no nearest-point S11 could be computed.",
                        "threshold_db": threshold,
                    }
                )
        if "s21_db_at_f0_min" in targets:
            threshold = float(targets["s21_db_at_f0_min"])
            if "s21_db_at_f0" in metrics:
                checks.append(
                    {
                        "name": "s21_at_f0_target",
                        "passed": metrics["s21_db_at_f0"] >= threshold,
                        "measured_s21_db_at_f0": metrics["s21_db_at_f0"],
                        "nearest_freq_ghz": metrics.get("s21_f0_nearest_ghz"),
                        "threshold_db": threshold,
                    }
                )
            else:
                checks.append(
                    {
                        "name": "s21_at_f0_target",
                        "passed": False,
                        "reason": "No two-port S21 metric could be computed from the available Touchstone data.",
                        "threshold_db": threshold,
                    }
                )
        if "max_s21_freq_ghz_target" in targets and metrics.get("point_count"):
            target_freq = float(targets["max_s21_freq_ghz_target"])
            tolerance = float(targets.get("max_s21_freq_tolerance_ghz", 0.5))
            if "max_s21_freq_ghz" in metrics:
                checks.append(
                    {
                        "name": "max_s21_frequency_target",
                        "passed": abs(metrics["max_s21_freq_ghz"] - target_freq) <= tolerance,
                        "measured_max_s21_freq_ghz": metrics["max_s21_freq_ghz"],
                        "target_freq_ghz": target_freq,
                        "tolerance_ghz": tolerance,
                    }
                )
            else:
                checks.append(
                    {
                        "name": "max_s21_frequency_target",
                        "passed": False,
                        "reason": "No S21 curve was available.",
                    }
                )
        passed = bool(checks) and all(check.get("passed") for check in checks)
        return {"status": "ok" if passed else "fail", "checks": checks}


class OptimizerAdapter(BaseAdapter):
    name = "optimizer"

    def capabilities(self) -> dict[str, Any]:
        return {
            "actions": ["propose_next_parameters", "compare_results"],
            "strategy": "solver_evidence_rank_and_step",
        }

    def execute(
        self,
        action: str,
        payload: dict[str, Any],
        workspace: Path,
    ) -> AdapterResult:
        if action not in {"propose_next_parameters", "compare_results"}:
            return super().execute(action, payload, workspace)
        if action == "compare_results":
            return self._compare_results(payload, workspace)
        validation_path = workspace / "validation_report.json"
        validation: dict[str, Any] = {}
        if validation_path.exists():
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        parameters = dict(design.get("parameters", {})) if isinstance(design.get("parameters"), dict) else {}
        proposal = dict(parameters)
        metrics = validation.get("metrics") if isinstance(validation.get("metrics"), dict) else {}
        basis = "Keep parameters because validation passes."
        if validation.get("status") != "ok":
            f0 = self._numeric(parameters.get("f0_ghz") or design.get("f0_ghz"))
            min_freq = self._numeric(metrics.get("min_s11_freq_ghz"))
            width_key = self._first_numeric_key(parameters, {"patch_width_mm", "width_mm", "w_mm"})
            feed_key = self._first_numeric_key(parameters, {"feed_y_offset_mm", "feed_offset_mm", "inset_depth_mm"})
            if f0 and min_freq and width_key:
                error = (min_freq - f0) / f0
                scale = max(0.9, min(1.1, 1.0 + error))
                proposal[width_key] = round(float(parameters[width_key]) * scale, 6)
                direction = "increase" if scale > 1.0 else "decrease"
                basis = (
                    f"Validation failed and resonance is at {min_freq:.6g} GHz versus target {f0:.6g} GHz; "
                    f"{direction} {width_key} by resonance-scaled factor {scale:.6g}."
                )
            elif feed_key:
                proposal[feed_key] = round(float(parameters[feed_key]) + 0.5, 6)
                basis = f"Validation failed near target; adjust matching parameter {feed_key} by +0.5 mm."
            elif width_key:
                proposal[width_key] = round(float(parameters[width_key]) * 1.03, 6)
                basis = f"Validation failed without resonance evidence; apply conservative +3% to {width_key}."
            else:
                basis = "Validation failed, but no recognized numeric width/feed parameter was available to adjust."
        report_path = workspace / "optimizer_report.json"
        report = {
            "software": self.name,
            "created_at": _now(),
            "status": "ok",
            "input_parameters": parameters,
            "proposed_parameters": proposal,
            "basis": basis,
            "validation_status": validation.get("status"),
            "metrics": metrics,
        }
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return AdapterResult(
            "ok",
            self.name,
            action,
            artifacts={"optimizer_report": str(report_path)},
            metrics={"changed_parameter_count": sum(1 for key in proposal if proposal.get(key) != parameters.get(key))},
            details={"proposed_parameters": proposal},
        )

    def _compare_results(self, payload: dict[str, Any], workspace: Path) -> AdapterResult:
        candidates = payload.get("candidates") or payload.get("runs") or payload.get("run_ids") or []
        if not isinstance(candidates, list) or not candidates:
            return AdapterResult(
                "fail",
                self.name,
                "compare_results",
                error="compare_results requires a non-empty candidates, runs, or run_ids list.",
            )
        design = payload.get("design") if isinstance(payload.get("design"), dict) else payload
        targets = design.get("targets") if isinstance(design.get("targets"), dict) else {}
        threshold = self._numeric(targets.get("s11_db_at_f0_max"))
        f0 = self._numeric((design.get("parameters") or {}).get("f0_ghz") if isinstance(design.get("parameters"), dict) else design.get("f0_ghz"))
        root = Path(str(payload.get("workspace_root") or workspace.parent))
        rows = [self._candidate_row(item, root) for item in candidates]

        def rank_key(row: dict[str, Any]) -> tuple[float, float, float, float, str]:
            metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
            s11_at_f0 = self._numeric(metrics.get("s11_db_at_f0"))
            min_s11 = self._numeric(metrics.get("min_s11_db"))
            min_freq = self._numeric(metrics.get("min_s11_freq_ghz"))
            target_pass = threshold is not None and s11_at_f0 is not None and s11_at_f0 <= threshold
            freq_error = abs((min_freq or f0 or 0.0) - (f0 or min_freq or 0.0)) if (f0 or min_freq) else float("inf")
            return (
                0.0 if target_pass else 1.0,
                s11_at_f0 if s11_at_f0 is not None else float("inf"),
                freq_error,
                min_s11 if min_s11 is not None else float("inf"),
                str(row.get("run_id") or row.get("workspace") or ""),
            )

        ranked = sorted(rows, key=rank_key)
        for index, row in enumerate(ranked, start=1):
            metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
            s11_at_f0 = self._numeric(metrics.get("s11_db_at_f0"))
            row["rank"] = index
            row["target_pass"] = bool(threshold is not None and s11_at_f0 is not None and s11_at_f0 <= threshold)
        best = ranked[0] if ranked else None
        report_path = workspace / "optimizer_report.json"
        report = {
            "software": self.name,
            "created_at": _now(),
            "status": "ok",
            "target_s11_db_at_f0_max": threshold,
            "target_f0_ghz": f0,
            "best_candidate": best,
            "ranked_candidates": ranked,
        }
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        best_metrics = best.get("metrics", {}) if isinstance(best, dict) else {}
        return AdapterResult(
            "ok",
            self.name,
            "compare_results",
            artifacts={"optimizer_report": str(report_path)},
            metrics={
                "candidate_count": len(ranked),
                "passed_target_count": sum(1 for row in ranked if row.get("target_pass")),
                "best_s11_db_at_f0": best_metrics.get("s11_db_at_f0") if isinstance(best_metrics, dict) else None,
                "best_min_s11_db": best_metrics.get("min_s11_db") if isinstance(best_metrics, dict) else None,
            },
            details={"best_candidate": best, "ranked_candidates": ranked},
        )

    def _candidate_row(self, item: Any, root: Path) -> dict[str, Any]:
        if isinstance(item, dict):
            run_id = item.get("run_id")
            candidate_workspace = Path(str(item.get("workspace"))) if item.get("workspace") else root / str(run_id)
            parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
        else:
            run_id = str(item)
            candidate_workspace = root / run_id
            parameters = {}
        validation_path = candidate_workspace / "validation_report.json"
        solver_report_path = candidate_workspace / "solver_report.json"
        validation = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else {}
        solver_report = json.loads(solver_report_path.read_text(encoding="utf-8")) if solver_report_path.exists() else {}
        metrics = {}
        if isinstance(solver_report.get("metrics"), dict):
            metrics.update(solver_report["metrics"])
        if isinstance(validation.get("metrics"), dict):
            metrics.update(validation["metrics"])
        geometry = solver_report.get("geometry") if isinstance(solver_report.get("geometry"), dict) else {}
        combined_parameters = {**parameters, **geometry}
        return {
            "run_id": run_id or candidate_workspace.name,
            "workspace": str(candidate_workspace),
            "validation_status": validation.get("status"),
            "metrics": metrics,
            "parameters": combined_parameters,
            "artifacts": {
                key: value
                for key, value in {
                    "validation_report": str(validation_path) if validation_path.exists() else None,
                    "solver_report": str(solver_report_path) if solver_report_path.exists() else None,
                    "sparameters": metrics.get("touchstone_path"),
                    "hfss_project": solver_report.get("project"),
                }.items()
                if value
            },
        }

    def _numeric(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _first_numeric_key(self, values: dict[str, Any], keys: set[str]) -> str | None:
        for key, value in values.items():
            if key.lower() in keys and self._numeric(value) is not None:
                return key
        return None


def default_adapters(
    *,
    mock_hfss: bool = False,
    hfss_config: dict[str, Any] | None = None,
) -> dict[str, BaseAdapter]:
    hfss_config = dict(hfss_config or {})
    return {
        "geometry": GeometryAdapter(),
        "hfss": HfssAdapter(use_mock=mock_hfss, **hfss_config),
        "python_postprocess": PythonPostprocessAdapter(),
        "optimizer": OptimizerAdapter(),
    }
