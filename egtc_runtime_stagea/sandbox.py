from __future__ import annotations

import resource
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import NodeCapsule, to_plain_dict


@dataclass
class ResourceLimits:
    cpu_quota: str = "2 cores"
    memory_mb: int = 4096
    disk_mb: int = 8192
    max_processes: int = 128
    wall_time_sec: int = 600
    max_command_count: int = 50


@dataclass
class ResourceReport:
    node_id: str
    wall_time_sec: float
    cpu_time_sec: float
    max_memory_mb: float
    disk_written_mb: float
    command_count: int
    network_attempt_count: int
    oom_killed: bool
    timeout_killed: bool


@dataclass
class SandboxEvent:
    timestamp: float
    run_id: str
    node_id: str
    worker_id: str
    event_type: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxExecutionSpec:
    backend: str
    sandbox_mode: str
    network_mode: str
    codex_sandbox: str
    resource_limits: ResourceLimits
    command_count: int = 1


class SandboxRuntime:
    def prepare(self, node: NodeCapsule) -> SandboxExecutionSpec:
        profile = node.sandbox_profile or {}
        limits = self._limits(profile.get("resource_limits", {}))
        sandbox_mode = str(profile.get("sandbox_mode") or "workspace_write")
        return SandboxExecutionSpec(
            backend=str(profile.get("backend") or "codex_native"),
            sandbox_mode=sandbox_mode,
            network_mode=str(profile.get("network") or "none"),
            codex_sandbox=self._codex_mode(sandbox_mode),
            resource_limits=limits,
        )

    def start_events(
        self,
        run_id: str,
        node: NodeCapsule,
        worker_id: str,
        spec: SandboxExecutionSpec,
        cwd: Path,
    ) -> list[SandboxEvent]:
        return [
            SandboxEvent(
                timestamp=time.time(),
                run_id=run_id,
                node_id=node.node_id,
                worker_id=worker_id,
                event_type="sandbox_started",
                details={
                    "backend": spec.backend,
                    "sandbox_mode": spec.sandbox_mode,
                    "network_mode": spec.network_mode,
                    "cwd": str(cwd),
                    "resource_limits": to_plain_dict(spec.resource_limits),
                },
            ),
            SandboxEvent(
                timestamp=time.time(),
                run_id=run_id,
                node_id=node.node_id,
                worker_id=worker_id,
                event_type="network_policy_applied",
                details={"mode": spec.network_mode, "enforced": spec.network_mode == "none"},
            ),
        ]

    def finish_events(
        self,
        run_id: str,
        node: NodeCapsule,
        worker_id: str,
        exit_code: int,
        timed_out: bool,
    ) -> list[SandboxEvent]:
        return [
            SandboxEvent(
                timestamp=time.time(),
                run_id=run_id,
                node_id=node.node_id,
                worker_id=worker_id,
                event_type="process_exit",
                details={"exit_code": exit_code, "timeout_killed": timed_out},
            )
        ]

    def report(
        self,
        node: NodeCapsule,
        start_time: float,
        usage_before: resource.struct_rusage,
        usage_after: resource.struct_rusage,
        timed_out: bool,
        command_count: int,
        network_attempt_count: int = 0,
    ) -> ResourceReport:
        cpu_time = (
            usage_after.ru_utime
            + usage_after.ru_stime
            - usage_before.ru_utime
            - usage_before.ru_stime
        )
        max_rss_kb = max(0, usage_after.ru_maxrss - usage_before.ru_maxrss)
        return ResourceReport(
            node_id=node.node_id,
            wall_time_sec=round(time.time() - start_time, 6),
            cpu_time_sec=round(cpu_time, 6),
            max_memory_mb=round(max_rss_kb / 1024, 6),
            disk_written_mb=0.0,
            command_count=command_count,
            network_attempt_count=network_attempt_count,
            oom_killed=False,
            timeout_killed=timed_out,
        )

    def _limits(self, raw: dict[str, Any]) -> ResourceLimits:
        return ResourceLimits(
            cpu_quota=str(raw.get("cpu_quota", "2 cores")),
            memory_mb=int(raw.get("memory_mb", 4096)),
            disk_mb=int(raw.get("disk_mb", 8192)),
            max_processes=int(raw.get("max_processes", 128)),
            wall_time_sec=int(raw.get("wall_time_sec", 600)),
            max_command_count=int(raw.get("max_command_count", 50)),
        )

    def _codex_mode(self, sandbox_mode: str) -> str:
        mapping = {
            "read_only": "read-only",
            "workspace_write": "workspace-write",
            "full_access_isolated": "danger-full-access",
        }
        return mapping.get(sandbox_mode, "workspace-write")
