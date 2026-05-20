from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .artifact_store import ArtifactStore
from typing import Any

from .model_agent import ModelAgentRegistry, ModelAgentRequest
from .models import ActorIdentity, CapabilityToken, NodeCapsule, WorkerResult
from .sandbox import SandboxRuntime


class CodexExecWrapper:
    """Stage A worker launcher.

    `executor_kind="subprocess"` runs a local command.
    `executor_kind="codex_cli"` launches a real `codex exec --json` session.
    `executor_kind="model_agent"` launches a provider-backed model agent.
    """

    def __init__(
        self,
        artifact_store: ArtifactStore,
        actor: ActorIdentity,
        token: CapabilityToken,
    ) -> None:
        self.artifact_store = artifact_store
        self.actor = actor
        self.token = token
        self.sandbox = SandboxRuntime()

    def run(
        self,
        node: NodeCapsule,
        cwd: Path,
        role: str = "worker",
        run_id: str | None = None,
    ) -> WorkerResult:
        cwd.mkdir(parents=True, exist_ok=True)
        agent_id = f"{role}-{uuid.uuid4().hex[:12]}"
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        spec = self.sandbox.prepare(node)
        command = self._build_command(node, cwd, spec.codex_sandbox)
        start_time = time.time()
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        timed_out = False
        sandbox_events = self.sandbox.start_events(run_id, node, agent_id, spec, cwd)
        network_attempt_count = 0
        command_count = spec.command_count
        if node.executor_kind == "model_agent":
            model_result = self._run_model_agent(
                node,
                cwd,
                role,
                agent_id,
                spec.resource_limits.wall_time_sec,
            )
            exit_code = model_result.exit_code
            stdout = model_result.stdout
            stderr = model_result.stderr
            network_attempt_count = model_result.network_attempt_count
            command_count = 0
        else:
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    text=True,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    timeout=spec.resource_limits.wall_time_sec,
                )
                exit_code = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = 124
                stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                stderr = exc.stderr if isinstance(exc.stderr, str) else ""
                stderr += f"\nSandbox timeout after {spec.resource_limits.wall_time_sec}s\n"
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        sandbox_events.extend(
            self.sandbox.finish_events(run_id, node, agent_id, exit_code, timed_out)
        )
        resource_report = self.sandbox.report(
            node,
            start_time,
            usage_before,
            usage_after,
            timed_out,
            command_count=command_count,
            network_attempt_count=network_attempt_count,
        )
        parsed_events: list[dict[str, Any]] = []
        event_lines: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = {"type": "log", "stream": "stdout", "message": line}
            if isinstance(event, dict):
                event.setdefault("agent_id", agent_id)
                if role == "worker":
                    event.setdefault("worker_id", agent_id)
                elif role == "overlooker":
                    event.setdefault("overlooker_id", agent_id)
                parsed_events.append(event)
                event_lines.append(json.dumps(event, sort_keys=True))

        metadata = {
            "node_id": node.node_id,
            f"{role}_id": agent_id,
            "executor_kind": node.executor_kind,
            "model_provider": node.model_provider,
            "model": node.model,
        }
        event_ref = self.artifact_store.put_bytes(
            ("\n".join(event_lines) + "\n").encode("utf-8"),
            "application/jsonl",
            {"kind": f"{role}_events", **metadata},
            self.actor,
            self.token,
        )
        stdout_ref = self.artifact_store.put_bytes(
            stdout.encode("utf-8"),
            "text/plain",
            {"kind": f"{role}_stdout", **metadata},
            self.actor,
            self.token,
        )
        stderr_ref = self.artifact_store.put_bytes(
            stderr.encode("utf-8"),
            "text/plain",
            {"kind": f"{role}_stderr", **metadata},
            self.actor,
            self.token,
        )
        sandbox_event_ref = self.artifact_store.put_bytes(
            (
                "\n".join(json.dumps(event.__dict__, sort_keys=True) for event in sandbox_events)
                + "\n"
            ).encode("utf-8"),
            "application/jsonl",
            {"kind": f"{role}_sandbox_events", **metadata},
            self.actor,
            self.token,
        )
        resource_report_ref = self.artifact_store.put_json(
            resource_report,
            {"kind": f"{role}_resource_report", **metadata},
            self.actor,
            self.token,
        )
        return WorkerResult(
            worker_id=agent_id,
            status="submitted",
            exit_code=exit_code,
            event_refs=[event_ref],
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
            parsed_events=parsed_events,
            sandbox_event_refs=[sandbox_event_ref],
            resource_report_ref=resource_report_ref,
        )

    def _build_command(self, node: NodeCapsule, cwd: Path, codex_sandbox: str) -> list[str]:
        if node.executor_kind == "subprocess":
            if not node.command:
                raise ValueError("subprocess node requires command")
            return node.command
        if node.executor_kind == "model_agent":
            return []
        if node.executor_kind != "codex_cli":
            raise ValueError(f"unsupported executor_kind: {node.executor_kind}")

        prompt = node.prompt or node.goal
        codex_binary = node.codex_binary or self._find_codex_binary()
        return [
            codex_binary,
            "-a",
            "never",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-C",
            str(cwd.resolve()),
            "-s",
            codex_sandbox,
            prompt,
        ]

    def _run_model_agent(
        self,
        node: NodeCapsule,
        cwd: Path,
        role: str,
        agent_id: str,
        timeout_sec: int,
    ):
        config = dict(node.model_config or {})
        provider_name = node.model_provider or config.get("provider")
        provider = ModelAgentRegistry().get(str(provider_name) if provider_name else None)
        output_file = config.get("output_file")
        tools = self._dict_list(config.get("tools"))
        mcp_servers = self._dict_list(config.get("mcp_servers"))
        tool_env = config.get("tool_env") if isinstance(config.get("tool_env"), dict) else {}
        request = ModelAgentRequest(
            node_id=node.node_id,
            role=role,
            phase=node.phase,
            goal=node.goal,
            prompt=self._model_agent_prompt(node, cwd, config),
            cwd=cwd,
            provider=str(provider_name) if provider_name else provider.provider_name,
            model=node.model or (str(config.get("model")) if config.get("model") else None),
            system_prompt=(
                str(config.get("system_prompt"))
                if isinstance(config.get("system_prompt"), str)
                else None
            ),
            output_file=str(output_file) if output_file else None,
            output_json=bool(config.get("output_json", config.get("json_output", False))),
            timeout_sec=timeout_sec,
            config={
                **config,
                "agent_id": agent_id,
                "executor_kind": node.executor_kind,
            },
            tools=tools,
            mcp_servers=mcp_servers,
            tool_env=dict(tool_env),
        )
        return provider.run(request)

    def _model_agent_prompt(
        self,
        node: NodeCapsule,
        cwd: Path,
        config: dict[str, Any],
    ) -> str:
        parts = [
            f"Node id: {node.node_id}",
            f"Phase: {node.phase}",
            f"Goal: {node.goal}",
            "Acceptance criteria:",
            json.dumps(node.acceptance_criteria, indent=2, sort_keys=True),
            "Required evidence:",
            json.dumps(node.required_evidence, indent=2, sort_keys=True),
            "Instruction:",
            node.prompt or node.goal,
        ]
        tooling_profile = config.get("tooling_profile")
        tools = self._dict_list(config.get("tools"))
        mcp_servers = self._dict_list(config.get("mcp_servers"))
        tool_env = config.get("tool_env") if isinstance(config.get("tool_env"), dict) else {}
        if tooling_profile or tools or mcp_servers or tool_env:
            parts.extend(
                [
                    "Available tooling profile:",
                    json.dumps(
                        {
                            "tooling_profile": tooling_profile,
                            "tools": tools,
                            "mcp_servers": mcp_servers,
                            "allowed_mcp_tools": config.get("allowed_mcp_tools", []),
                            "tool_env": tool_env,
                            "dataset_access": config.get("dataset_access", {}),
                            "permission_notes": config.get("tooling_permission_notes", []),
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    (
                        "Tooling rule: use only tools whose permission preconditions are satisfied by the node "
                        "sandbox profile and repo policy. Treat runtime_manifest MCP servers as capability "
                        "descriptions unless the host maps them to concrete MCP transports."
                    ),
                ]
            )
        input_files = config.get("input_files", [])
        if isinstance(input_files, list) and input_files:
            max_bytes = int(config.get("max_input_file_bytes", 200_000))
            parts.append("Workspace input files:")
            for item in input_files:
                rel_path = str(item)
                path = self._safe_workspace_path(cwd, rel_path)
                if not path.exists():
                    parts.append(f"\n# {rel_path}\n<missing>")
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                if len(content.encode("utf-8")) > max_bytes:
                    content = content.encode("utf-8")[:max_bytes].decode(
                        "utf-8",
                        errors="replace",
                    )
                    content += "\n<truncated>"
                parts.append(f"\n# {rel_path}\n{content}")
        return "\n\n".join(parts)

    def _dict_list(self, value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def _safe_workspace_path(self, cwd: Path, relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute():
            raise ValueError("model agent input_files must be relative to workspace")
        resolved = (cwd / path).resolve()
        cwd_resolved = cwd.resolve()
        if cwd_resolved != resolved and cwd_resolved not in resolved.parents:
            raise ValueError("model agent input_files cannot escape workspace")
        return resolved

    def _find_codex_binary(self) -> str:
        configured = os.environ.get("CODEX_BIN")
        if configured:
            return configured
        found = shutil.which("codex")
        if found:
            return found
        candidates = [
            "/home/batchcom/.windsurf-server/extensions/openai.chatgpt-26.422.71525/bin/linux-x86_64/codex",
            "/home/batchcom/.windsurf-server/extensions/openai.chatgpt-26.409.20454-linux-x64/bin/linux-x86_64/codex",
        ]
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
        raise FileNotFoundError("codex binary not found; set CODEX_BIN")
