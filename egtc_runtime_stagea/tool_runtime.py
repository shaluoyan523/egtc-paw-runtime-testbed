from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool_id: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallResult:
    call_id: str
    tool_id: str
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    permission: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "permission": self.permission,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": round(self.finished_at - self.started_at, 6),
        }


class ModelAgentToolRuntime:
    """Runtime built-in MCP shim for model-agent tool calls."""

    def __init__(self, request: Any) -> None:
        self.request = request
        self.cwd = Path(request.cwd)
        self.tools = [
            dict(tool)
            for tool in getattr(request, "tools", [])
            if isinstance(tool, dict) and tool.get("tool_id")
        ]
        self.tool_by_id = {str(tool["tool_id"]): tool for tool in self.tools}
        self.allowed_mcp_tools = self._string_set(request.config.get("allowed_mcp_tools"))
        self.sandbox_profile = (
            getattr(request, "sandbox_profile", None)
            if isinstance(getattr(request, "sandbox_profile", None), dict)
            else {}
        )
        self.network_mode = str(self.sandbox_profile.get("network") or "none")
        self.allowed_read_paths = self._path_list(
            self.sandbox_profile.get("allowed_read_paths"),
            default=["."],
        )
        self.allowed_write_paths = self._path_list(
            self.sandbox_profile.get("allowed_write_paths"),
            default=[],
        )
        self.max_tool_calls = int(request.config.get("max_tool_calls", 16))
        self.default_timeout_sec = int(request.config.get("tool_timeout_sec", 120))
        self.results: list[ToolCallResult] = []
        self.audit_events: list[dict[str, Any]] = []
        self.network_attempt_count = 0
        self.command_count = 0

    @property
    def tool_call_count(self) -> int:
        return len(self.results)

    @property
    def has_blocking_failure(self) -> bool:
        return any(result.status in {"denied", "error"} for result in self.results)

    @property
    def has_permission_review_request(self) -> bool:
        return any(
            bool(result.permission.get("permission_review_required"))
            for result in self.results
        )

    def dispatch_many(self, tool_calls: list[ToolCall]) -> list[ToolCallResult]:
        dispatched: list[ToolCallResult] = []
        for call in tool_calls:
            result = self.dispatch(call)
            dispatched.append(result)
        return dispatched

    def dispatch(self, call: ToolCall) -> ToolCallResult:
        started_at = time.time()
        permission = self._permission_for(call)
        if not permission["allowed"]:
            return self._record_result(
                ToolCallResult(
                    call_id=call.call_id,
                    tool_id=call.tool_id,
                    status="denied",
                    error=permission["reason"],
                    permission=permission,
                    started_at=started_at,
                    finished_at=time.time(),
                )
            )
        try:
            handler = {
                "filesystem.read_text": self._filesystem_read_text,
                "filesystem.write_artifact": self._filesystem_write_artifact,
                "filesystem.search": self._filesystem_search,
                "git.inspect": self._git_inspect,
                "python.run_script": self._python_run_script,
                "python.compileall": self._python_compileall,
                "python.pytest": self._python_pytest,
                "dataset.modelscope_swe_stream": self._dataset_modelscope_swe_stream,
                "dataset.hf_swe_stream": self._dataset_hf_swe_stream,
                "dataset.select_swe_cases": self._dataset_select_swe_cases,
            }.get(call.tool_id)
            if handler is None:
                raise ValueError(f"No runtime adapter is registered for tool {call.tool_id!r}.")
            output = handler(call.arguments)
            result = ToolCallResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                status="ok",
                result=output,
                permission=permission,
                started_at=started_at,
                finished_at=time.time(),
            )
        except Exception as exc:
            result = ToolCallResult(
                call_id=call.call_id,
                tool_id=call.tool_id,
                status="error",
                error=str(exc),
                permission=permission,
                started_at=started_at,
                finished_at=time.time(),
            )
        return self._record_result(result)

    def write_artifacts(self) -> list[str]:
        if not self.results:
            return []
        audit_path = self.cwd / "tool_audit.jsonl"
        evidence_path = self.cwd / "tool_evidence.json"
        audit_path.write_text(
            "\n".join(json.dumps(event, sort_keys=True) for event in self.audit_events)
            + "\n",
            encoding="utf-8",
        )
        evidence = {
            "node_id": self.request.node_id,
            "agent_id": self.request.config.get("agent_id"),
            "tooling_profile": self.request.config.get("tooling_profile"),
            "call_count": len(self.results),
            "ok_count": sum(1 for item in self.results if item.status == "ok"),
            "denied_count": sum(1 for item in self.results if item.status == "denied"),
            "error_count": sum(1 for item in self.results if item.status == "error"),
            "network_attempt_count": self.network_attempt_count,
            "command_count": self.command_count,
            "permission_review_required": self.has_permission_review_request,
            "results": [result.to_dict() for result in self.results],
        }
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return [str(audit_path), str(evidence_path)]

    def result_payload(self) -> dict[str, Any]:
        return {
            "tool_call_count": self.tool_call_count,
            "network_attempt_count": self.network_attempt_count,
            "command_count": self.command_count,
            "permission_review_required": self.has_permission_review_request,
            "results": [result.to_dict() for result in self.results],
        }

    def stdout_events(self) -> list[dict[str, Any]]:
        return list(self.audit_events)

    def _permission_for(self, call: ToolCall) -> dict[str, Any]:
        grounded_by = [
            "model_config.tools",
            "model_config.allowed_mcp_tools",
            "sandbox_profile.network",
            "sandbox_profile.allowed_read_paths",
            "sandbox_profile.allowed_write_paths",
        ]
        if len(self.results) >= self.max_tool_calls:
            return {
                "allowed": False,
                "reason": f"max_tool_calls exceeded: {self.max_tool_calls}",
                "grounded_by": grounded_by,
                "permission_review_required": False,
            }
        descriptor = self.tool_by_id.get(call.tool_id)
        if descriptor is None:
            return {
                "allowed": False,
                "reason": f"tool {call.tool_id!r} is not declared in model_config.tools",
                "grounded_by": grounded_by,
                "permission_review_required": False,
            }
        if self.allowed_mcp_tools and call.tool_id not in self.allowed_mcp_tools:
            return {
                "allowed": False,
                "reason": f"tool {call.tool_id!r} is not listed in allowed_mcp_tools",
                "grounded_by": grounded_by,
                "permission_review_required": False,
            }
        if bool(descriptor.get("requires_network")) and self.network_mode == "none":
            return {
                "allowed": False,
                "reason": f"tool {call.tool_id!r} requires network but sandbox_profile.network is none",
                "grounded_by": grounded_by,
                "permission_review_required": True,
            }
        return {
            "allowed": True,
            "reason": "tool call is declared and permission preconditions are satisfied",
            "grounded_by": grounded_by,
            "permission_review_required": False,
            "tool_descriptor": {
                "tool_id": descriptor.get("tool_id"),
                "mcp_server_id": descriptor.get("mcp_server_id"),
                "requires_network": bool(descriptor.get("requires_network")),
            },
        }

    def _record_result(self, result: ToolCallResult) -> ToolCallResult:
        self.results.append(result)
        event = {
            "type": "model_agent_tool_call",
            "node_id": self.request.node_id,
            "provider": self.request.provider,
            "call_id": result.call_id,
            "tool_id": result.tool_id,
            "status": result.status,
            "permission": result.permission,
            "error": result.error,
            "duration_sec": round(result.finished_at - result.started_at, 6),
        }
        if result.status == "ok":
            event["result_summary"] = self._summarize_result(result.result)
        self.audit_events.append(event)
        return result

    def _filesystem_read_text(self, args: dict[str, Any]) -> dict[str, Any]:
        paths = args.get("paths")
        if paths is None:
            paths = [args.get("path")]
        if not isinstance(paths, list):
            paths = [paths]
        max_bytes = int(args.get("max_bytes", 20_000))
        files: list[dict[str, Any]] = []
        for item in paths:
            rel_path = str(item or "")
            path = self._safe_path(rel_path)
            self._ensure_path_allowed(rel_path, self.allowed_read_paths, "read")
            if not path.exists():
                files.append({"path": rel_path, "exists": False})
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            encoded = content.encode("utf-8")
            truncated = len(encoded) > max_bytes
            if truncated:
                content = encoded[:max_bytes].decode("utf-8", errors="replace")
            files.append(
                {
                    "path": rel_path,
                    "exists": True,
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "content": content,
                    "truncated": truncated,
                }
            )
        return {"files": files}

    def _filesystem_write_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        rel_path = str(args.get("path") or args.get("output_file") or "")
        if not rel_path:
            raise ValueError("filesystem.write_artifact requires path")
        self._ensure_path_allowed(rel_path, self.allowed_write_paths, "write")
        path = self._safe_path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if "json" in args:
            text = json.dumps(args["json"], indent=2, sort_keys=True)
        else:
            text = str(args.get("content", ""))
        path.write_text(text, encoding="utf-8")
        data = path.read_bytes()
        return {
            "path": rel_path,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def _filesystem_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("pattern") or "")
        if not query:
            raise ValueError("filesystem.search requires query or pattern")
        root_rel = str(args.get("root") or ".")
        self._ensure_path_allowed(root_rel, self.allowed_read_paths, "read")
        root = self._safe_path(root_rel)
        max_matches = int(args.get("max_matches", 50))
        use_regex = bool(args.get("regex", False))
        matcher = re.compile(query) if use_regex else None
        matches: list[dict[str, Any]] = []
        for current_root, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in {".git", "__pycache__"}]
            for filename in files:
                if len(matches) >= max_matches:
                    break
                path = Path(current_root) / filename
                rel_path = path.relative_to(self.cwd).as_posix()
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except Exception:
                    continue
                for line_no, line in enumerate(lines, start=1):
                    matched = bool(matcher.search(line)) if matcher else query in line
                    if matched:
                        matches.append(
                            {
                                "path": rel_path,
                                "line": line_no,
                                "snippet": line[:240],
                            }
                        )
                    if len(matches) >= max_matches:
                        break
            if len(matches) >= max_matches:
                break
        return {"query": query, "root": root_rel, "matches": matches}

    def _git_inspect(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or args.get("command") or "status")
        extra_args = args.get("args") if isinstance(args.get("args"), list) else []
        if isinstance(args.get("command"), list):
            command_parts = [str(item) for item in args["command"]]
            action = command_parts[0] if command_parts else "status"
            extra_args = command_parts[1:]
        allowed_actions = {"status", "diff", "show", "log"}
        if action not in allowed_actions:
            raise ValueError(f"git.inspect action must be one of {sorted(allowed_actions)}")
        if action == "status":
            command = ["git", "status", "--short"]
        elif action == "diff":
            command = ["git", "diff", *[str(item) for item in extra_args]]
        elif action == "show":
            command = ["git", "show", *[str(item) for item in extra_args[:2]]]
        else:
            command = ["git", "log", "--oneline", *[str(item) for item in extra_args[:4]]]
        return self._run_subprocess(command, timeout_sec=int(args.get("timeout_sec", 30)))

    def _python_run_script(self, args: dict[str, Any]) -> dict[str, Any]:
        script = str(args.get("script") or "")
        if not script:
            raise ValueError("python.run_script requires script")
        self._ensure_path_allowed(script, self.allowed_read_paths, "read")
        script_path = self._safe_path(script)
        command = [
            sys.executable,
            str(script_path),
            *[str(item) for item in args.get("args", []) if isinstance(args.get("args"), list)],
        ]
        return self._run_subprocess(command, timeout_sec=int(args.get("timeout_sec", self.default_timeout_sec)))

    def _python_compileall(self, args: dict[str, Any]) -> dict[str, Any]:
        paths = args.get("paths")
        if paths is None:
            paths = [args.get("path") or "."]
        if not isinstance(paths, list):
            paths = [paths]
        rel_paths = [str(item) for item in paths]
        for rel_path in rel_paths:
            self._ensure_path_allowed(rel_path, self.allowed_read_paths, "read")
        quiet = int(args.get("quiet", 1))
        command = [sys.executable, "-m", "compileall", *("-q" for _ in range(quiet)), *rel_paths]
        return self._run_subprocess(command, timeout_sec=int(args.get("timeout_sec", self.default_timeout_sec)))

    def _python_pytest(self, args: dict[str, Any]) -> dict[str, Any]:
        pytest_args = [str(item) for item in args.get("args", []) if isinstance(args.get("args"), list)]
        command = [sys.executable, "-m", "pytest", *pytest_args]
        return self._run_subprocess(command, timeout_sec=int(args.get("timeout_sec", self.default_timeout_sec)))

    def _dataset_modelscope_swe_stream(self, args: dict[str, Any]) -> dict[str, Any]:
        self.network_attempt_count += 1
        from modelscope.msdatasets import MsDataset

        split = str(args.get("split") or "train")
        scan_limit = int(args.get("scan_limit", args.get("count", 3)))
        dataset = MsDataset.load(
            str(args.get("dataset") or "SWE-bench"),
            namespace=str(args.get("namespace") or "AI-ModelScope"),
            split=split,
            use_streaming=True,
        )
        rows: list[dict[str, Any]] = []
        for index, row in enumerate(dataset):
            if index >= scan_limit:
                break
            rows.append(_summarize_swe_row(dict(row), max_chars=int(args.get("max_field_chars", 1200))))
        return {"dataset": "AI-ModelScope/SWE-bench", "split": split, "row_count": len(rows), "rows": rows}

    def _dataset_hf_swe_stream(self, args: dict[str, Any]) -> dict[str, Any]:
        self.network_attempt_count += 1
        from datasets import load_dataset

        dataset_id = str(args.get("dataset_id") or args.get("dataset") or "princeton-nlp/SWE-bench")
        split = str(args.get("split") or "train")
        scan_limit = int(args.get("scan_limit", args.get("count", 3)))
        dataset = load_dataset(dataset_id, split=split, streaming=True)
        rows: list[dict[str, Any]] = []
        for index, row in enumerate(dataset):
            if index >= scan_limit:
                break
            rows.append(_summarize_swe_row(dict(row), max_chars=int(args.get("max_field_chars", 1200))))
        return {"dataset": dataset_id, "split": split, "row_count": len(rows), "rows": rows}

    def _dataset_select_swe_cases(self, args: dict[str, Any]) -> dict[str, Any]:
        rows = args.get("rows")
        if not isinstance(rows, list):
            raise ValueError("dataset.select_swe_cases requires rows list")
        mode = str(args.get("mode") or "complex")
        count = int(args.get("count", 1))
        scored = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item["_selection_score"] = _swe_complexity_score(item) if mode == "complex" else _swe_simplicity_score(item)
            scored.append(item)
        scored.sort(key=lambda item: item["_selection_score"], reverse=(mode == "complex"))
        return {
            "mode": mode,
            "selected_count": min(count, len(scored)),
            "cases": [
                _summarize_swe_row(item, max_chars=int(args.get("max_field_chars", 1200)))
                for item in scored[:count]
            ],
        }

    def _run_subprocess(self, command: list[str], timeout_sec: int) -> dict[str, Any]:
        self.command_count += 1
        completed = subprocess.run(
            command,
            cwd=self.cwd,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=timeout_sec,
        )
        return {
            "command": command,
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-20_000:],
            "stderr": completed.stderr[-20_000:],
        }

    def _safe_path(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute():
            raise ValueError("tool paths must be relative to workspace")
        resolved = (self.cwd / path).resolve()
        cwd_resolved = self.cwd.resolve()
        if cwd_resolved != resolved and cwd_resolved not in resolved.parents:
            raise ValueError("tool path escapes workspace")
        return resolved

    def _ensure_path_allowed(self, relative_path: str, allowed_paths: list[str], mode: str) -> None:
        if not self._path_is_allowed(relative_path, allowed_paths):
            raise PermissionError(
                f"{mode} path {relative_path!r} is not allowed by sandbox profile"
            )

    def _path_is_allowed(self, relative_path: str, allowed_paths: list[str]) -> bool:
        normalized = PurePosixPath(str(relative_path or ".")).as_posix().strip("/")
        for allowed in allowed_paths:
            allowed_norm = PurePosixPath(str(allowed or ".")).as_posix().strip("/")
            if allowed_norm in {"", "."}:
                return True
            if normalized == allowed_norm or normalized.startswith(f"{allowed_norm}/"):
                return True
        return False

    def _path_list(self, value: object, *, default: list[str]) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        return list(default)

    def _string_set(self, value: object) -> set[str]:
        if isinstance(value, list):
            return {str(item) for item in value}
        return set()

    def _summarize_result(self, result: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key, value in result.items():
            if isinstance(value, list):
                summary[key] = {"count": len(value)}
            elif isinstance(value, dict):
                summary[key] = {"keys": sorted(value)[:10]}
            elif isinstance(value, str):
                summary[key] = value[:240]
            else:
                summary[key] = value
        return summary


def normalize_tool_calls(value: Any) -> list[ToolCall]:
    if value is None:
        return []
    if isinstance(value, dict) and "tool_calls" in value:
        value = value.get("tool_calls")
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    calls: list[ToolCall] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        tool_id = (
            raw.get("tool_id")
            or raw.get("name")
            or raw.get("tool")
            or function.get("name")
        )
        if not tool_id:
            continue
        tool_id = _canonical_tool_id(str(tool_id))
        raw_args = raw.get("arguments", raw.get("args", raw.get("input", function.get("arguments", {}))))
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except Exception:
                raw_args = {"value": raw_args}
        if not isinstance(raw_args, dict):
            raw_args = {"value": raw_args}
        calls.append(
            ToolCall(
                call_id=str(raw.get("id") or f"tool-call-{index}-{uuid.uuid4().hex[:8]}"),
                tool_id=tool_id,
                arguments=raw_args,
            )
        )
    return calls


def extract_tool_calls_from_text(text: str) -> list[ToolCall]:
    data = _extract_json_object(text)
    if data is None:
        return []
    return normalize_tool_calls(data)


def openai_tool_specs(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("tool_id"):
            continue
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": _openai_tool_name(str(tool["tool_id"])),
                    "description": f"Tool id: {tool['tool_id']}. {tool.get('description') or tool['tool_id']}",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {},
                    },
                },
            }
        )
    return specs


def _openai_tool_name(tool_id: str) -> str:
    return tool_id.replace(".", "__")


def _canonical_tool_id(tool_id: str) -> str:
    if "." not in tool_id and "__" in tool_id:
        return tool_id.replace("__", ".")
    return tool_id


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        data = json.loads(stripped)
    except Exception:
        data = None
    if isinstance(data, dict):
        return data
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stripped[start : end + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _summarize_swe_row(row: dict[str, Any], max_chars: int) -> dict[str, Any]:
    keys = [
        "instance_id",
        "repo",
        "base_commit",
        "problem_statement",
        "patch",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
    ]
    summary: dict[str, Any] = {}
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, str) and len(value) > max_chars:
            summary[key] = value[:max_chars] + "\n<truncated>"
        else:
            summary[key] = value
    summary["patch_lines"] = len(str(row.get("patch") or "").splitlines())
    summary["test_patch_lines"] = len(str(row.get("test_patch") or "").splitlines())
    summary["problem_chars"] = len(str(row.get("problem_statement") or ""))
    if "_selection_score" in row:
        summary["selection_score"] = row["_selection_score"]
    return summary


def _parse_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _swe_complexity_score(row: dict[str, Any]) -> int:
    return (
        len(str(row.get("patch") or ""))
        + len(str(row.get("test_patch") or ""))
        + len(str(row.get("problem_statement") or "")) // 2
        + 1000 * len(_parse_list(row.get("FAIL_TO_PASS")))
        + 50 * len(str(row.get("patch") or "").splitlines())
    )


def _swe_simplicity_score(row: dict[str, Any]) -> int:
    return (
        len(str(row.get("patch") or ""))
        + len(str(row.get("test_patch") or "")) // 2
        + len(str(row.get("problem_statement") or "")) // 4
        + 500 * len(_parse_list(row.get("FAIL_TO_PASS")))
    )
