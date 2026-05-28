from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import AdapterResult, BaseAdapter, default_adapters


def _safe_child(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute():
        raise ValueError("workspace paths must be relative")
    resolved = (root / path).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("workspace path escapes bridge root")
    return resolved


@dataclass
class BridgeConfig:
    workspace_root: Path
    mock_hfss: bool = False
    hfss: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BridgeConfig":
        workspace = Path(str(raw.get("workspace_root", "em_bridge_runs")))
        hfss = raw.get("hfss") if isinstance(raw.get("hfss"), dict) else {}
        return cls(
            workspace_root=workspace,
            mock_hfss=bool(raw.get("mock_hfss", False)),
            hfss=dict(hfss),
        )


class ThreeSoftwareMcpBridge:
    def __init__(
        self,
        config: BridgeConfig,
        adapters: dict[str, BaseAdapter] | None = None,
    ) -> None:
        self.config = config
        self.workspace_root = config.workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.adapters = adapters or default_adapters(
            mock_hfss=config.mock_hfss,
            hfss_config=config.hfss,
        )

    def describe_capabilities(self) -> dict[str, Any]:
        health = {name: adapter.health_check().to_dict() for name, adapter in self.adapters.items()}
        return {
            "bridge": "three-software-mcp-bridge",
            "workspace_root": str(self.workspace_root),
            "adapters": {
                name: {
                    "version": adapter.version(),
                    "capabilities": adapter.capabilities(),
                    "health": health[name],
                }
                for name, adapter in self.adapters.items()
            },
            "tools": [tool["name"] for tool in self.tool_definitions()],
        }

    def prepare_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str(payload.get("run_id") or f"em-{uuid.uuid4().hex[:12]}")
        run_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in run_id)
        workspace = _safe_child(self.workspace_root, run_id)
        workspace.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": run_id,
            "created_at": round(time.time(), 6),
            "design": payload.get("design", {}),
            "software_roles": {
                "geometry": "geometry",
                "solver": "hfss",
                "analysis": "python_postprocess",
                "optimizer": "optimizer",
            },
        }
        manifest_path = workspace / "input_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return {
            "status": "ok",
            "run_id": run_id,
            "workspace": str(workspace),
            "artifacts": {"input_manifest": str(manifest_path)},
        }

    def call_software(self, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.prepare_run(payload) if not payload.get("workspace") else None
        workspace = Path(str(payload.get("workspace") or run["workspace"]))
        adapter_name = str(payload.get("software", payload.get("adapter", "")))
        action = str(payload.get("action", ""))
        adapter = self.adapters.get(adapter_name)
        if adapter is None:
            return {
                "status": "fail",
                "error": f"unknown adapter: {adapter_name}",
                "known_adapters": sorted(self.adapters),
            }
        result = adapter.execute(action, payload, workspace)
        self._append_step(workspace, result)
        return result.to_dict()

    def run_pipeline(self, payload: dict[str, Any]) -> dict[str, Any]:
        prepared = self.prepare_run(payload)
        workspace = Path(prepared["workspace"])
        design_payload = {"design": payload.get("design", {})}
        steps: list[dict[str, Any]] = []

        for adapter_name, action in [
            ("geometry", "prepare_geometry"),
            ("hfss", str(payload.get("solver_action") or "run_solver")),
            ("python_postprocess", "validate_results"),
            ("optimizer", "propose_next_parameters"),
        ]:
            adapter = self.adapters[adapter_name]
            result = adapter.execute(action, design_payload, workspace)
            self._append_step(workspace, result)
            steps.append(result.to_dict())
            if result.status in {"fail", "blocked"} and adapter_name == "hfss":
                break

        bridge_report = self._bridge_report(workspace, prepared["run_id"], steps)
        return {
            "status": bridge_report["status"],
            "run_id": prepared["run_id"],
            "workspace": str(workspace),
            "steps": steps,
            "artifacts": {
                **prepared["artifacts"],
                "bridge_report": str(workspace / "bridge_report.json"),
            },
            "checks": bridge_report["checks"],
        }

    def collect_artifacts(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace_from_payload(payload)
        artifacts = {
            path.name: str(path)
            for path in workspace.rglob("*")
            if path.is_file()
        }
        return {"status": "ok", "workspace": str(workspace), "artifacts": artifacts}

    def compare_results(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_ids = payload.get("run_ids", [])
        if not isinstance(run_ids, list) or len(run_ids) < 2:
            return {"status": "fail", "error": "compare_results requires at least two run_ids"}
        rows = []
        for run_id in run_ids:
            workspace = _safe_child(self.workspace_root, str(run_id))
            validation = workspace / "validation_report.json"
            data = json.loads(validation.read_text(encoding="utf-8")) if validation.exists() else {}
            rows.append({"run_id": run_id, "validation_status": data.get("status"), "metrics": data.get("metrics", {})})
        return {"status": "ok", "runs": rows}

    def validate_pipeline(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace_from_payload(payload)
        report = self._bridge_report(workspace, workspace.name, self._read_steps(workspace))
        return report

    def tool_definitions(self) -> list[dict[str, Any]]:
        object_schema = {"type": "object", "additionalProperties": True}
        return [
            {
                "name": "bridge.describe_capabilities",
                "description": "Describe bridge adapters, health, and tools.",
                "inputSchema": object_schema,
            },
            {
                "name": "bridge.prepare_run",
                "description": "Create a bridge run workspace and input manifest.",
                "inputSchema": object_schema,
            },
            {
                "name": "bridge.call_software",
                "description": "Call one software adapter action.",
                "inputSchema": object_schema,
            },
            {
                "name": "bridge.run_pipeline",
                "description": "Run geometry -> HFSS -> postprocess -> optimizer pipeline.",
                "inputSchema": object_schema,
            },
            {
                "name": "bridge.collect_artifacts",
                "description": "List artifacts for a run workspace.",
                "inputSchema": object_schema,
            },
            {
                "name": "bridge.compare_results",
                "description": "Compare validation metrics from multiple run ids.",
                "inputSchema": object_schema,
            },
            {
                "name": "bridge.validate_pipeline",
                "description": "Validate bridge cross-tool consistency for a run.",
                "inputSchema": object_schema,
            },
        ]

    def dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        mapping = {
            "bridge.describe_capabilities": self.describe_capabilities,
            "bridge.prepare_run": self.prepare_run,
            "bridge.call_software": self.call_software,
            "bridge.run_pipeline": self.run_pipeline,
            "bridge.collect_artifacts": self.collect_artifacts,
            "bridge.compare_results": self.compare_results,
            "bridge.validate_pipeline": self.validate_pipeline,
        }
        handler = mapping.get(name)
        if handler is None:
            return {"status": "fail", "error": f"unknown tool: {name}"}
        if name == "bridge.describe_capabilities":
            return self.describe_capabilities()
        return handler(arguments or {})

    def _workspace_from_payload(self, payload: dict[str, Any]) -> Path:
        if payload.get("workspace"):
            return Path(str(payload["workspace"]))
        if payload.get("run_id"):
            return _safe_child(self.workspace_root, str(payload["run_id"]))
        raise ValueError("payload requires run_id or workspace")

    def _append_step(self, workspace: Path, result: AdapterResult) -> None:
        path = workspace / "bridge_steps.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")

    def _read_steps(self, workspace: Path) -> list[dict[str, Any]]:
        path = workspace / "bridge_steps.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _bridge_report(
        self,
        workspace: Path,
        run_id: str,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        checks = self._cross_tool_checks(workspace)
        terminal_statuses = {str(step.get("status")) for step in steps}
        status = "ok"
        if "blocked" in terminal_statuses:
            status = "blocked"
        elif "fail" in terminal_statuses or any(not check["passed"] for check in checks):
            status = "fail"
        report = {
            "status": status,
            "run_id": run_id,
            "workspace": str(workspace),
            "steps": steps,
            "checks": checks,
            "created_at": round(time.time(), 6),
        }
        (workspace / "bridge_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return report

    def _cross_tool_checks(self, workspace: Path) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        manifest_path = workspace / "geometry_manifest.json"
        solver_path = workspace / "solver_report.json"
        validation_path = workspace / "validation_report.json"
        checks.append({"name": "geometry_manifest_present", "passed": manifest_path.exists()})
        checks.append({"name": "solver_report_present", "passed": solver_path.exists()})
        checks.append({"name": "validation_report_present", "passed": validation_path.exists()})
        if manifest_path.exists() and solver_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            checks.append({"name": "units_declared", "passed": bool(manifest.get("units"))})
            checks.append({"name": "geometry_revision_declared", "passed": bool(manifest.get("geometry_revision"))})
        return checks
