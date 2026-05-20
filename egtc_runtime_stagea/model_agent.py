from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelAgentRequest:
    node_id: str
    role: str
    phase: str
    goal: str
    prompt: str
    cwd: Path
    provider: str
    model: str | None = None
    system_prompt: str | None = None
    output_file: str | None = None
    output_json: bool = False
    timeout_sec: int = 600
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelAgentResult:
    provider: str
    model: str | None
    exit_code: int
    stdout: str
    stderr: str
    raw_response: str = ""
    parsed_json: dict[str, Any] | None = None
    output_files: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    network_attempt_count: int = 0


class ModelAgentProvider(Protocol):
    provider_name: str

    def run(self, request: ModelAgentRequest) -> ModelAgentResult:
        ...


class DeterministicModelAgentProvider:
    provider_name = "deterministic"

    def run(self, request: ModelAgentRequest) -> ModelAgentResult:
        config = request.config
        if config.get("simulate_failure"):
            return ModelAgentResult(
                provider=self.provider_name,
                model=request.model,
                exit_code=int(config.get("failure_exit_code", 1)),
                stdout=self._jsonl(
                    [
                        {
                            "type": "model_agent_error",
                            "provider": self.provider_name,
                            "message": str(config.get("failure_message") or "simulated failure"),
                        }
                    ]
                ),
                stderr=str(config.get("failure_message") or "simulated failure"),
            )

        response_json = self._response_json(request)
        response_text = self._response_text(request, response_json)
        output_files = self._write_outputs(request, response_text, response_json)
        events = [
            {
                "type": "model_agent_started",
                "provider": self.provider_name,
                "model": request.model or "deterministic",
                "node_id": request.node_id,
                "role": request.role,
            },
            {
                "type": "model_agent_response",
                "provider": self.provider_name,
                "model": request.model or "deterministic",
                "node_id": request.node_id,
                "output_files": output_files,
                "json_output": response_json is not None,
            },
        ]
        if bool(config.get("emit_test_event", True)):
            events.append(
                {
                    "type": "test_result",
                    "name": str(config.get("test_name") or f"{request.node_id}_model_agent"),
                    "passed": bool(config.get("test_passed", True)),
                }
            )
        return ModelAgentResult(
            provider=self.provider_name,
            model=request.model or "deterministic",
            exit_code=0,
            stdout=self._jsonl(events),
            stderr="",
            raw_response=response_text,
            parsed_json=response_json,
            output_files=output_files,
        )

    def _response_json(self, request: ModelAgentRequest) -> dict[str, Any] | None:
        config = request.config
        configured = config.get("deterministic_response_json")
        if isinstance(configured, dict):
            return configured
        output_name = Path(request.output_file or "").name
        if output_name == "overlooker_report.json":
            return self._overlooker_report(request)
        if output_name == "fork_decision.json":
            return self._fork_decision(request)
        if output_name == "integration_overlooker_report.json":
            return self._integration_report(request)
        if output_name == "graph_patch.json":
            return self._graph_patch(request)
        if request.output_json:
            return {
                "provider": self.provider_name,
                "model": request.model or "deterministic",
                "node_id": request.node_id,
                "role": request.role,
                "status": "ok",
            }
        return None

    def _response_text(
        self,
        request: ModelAgentRequest,
        response_json: dict[str, Any] | None,
    ) -> str:
        configured = request.config.get("deterministic_response_text")
        if isinstance(configured, str):
            return configured
        if response_json is not None:
            return json.dumps(response_json, indent=2, sort_keys=True)
        return str(request.config.get("default_response_text") or "deterministic model agent completed")

    def _write_outputs(
        self,
        request: ModelAgentRequest,
        response_text: str,
        response_json: dict[str, Any] | None,
    ) -> list[str]:
        output_files: list[str] = []
        if request.output_file:
            path = _safe_workspace_path(request.cwd, request.output_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            if request.output_json and response_json is not None:
                path.write_text(
                    json.dumps(response_json, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            else:
                path.write_text(response_text, encoding="utf-8")
            output_files.append(str(path))
        if bool(request.config.get("write_test_result", True)):
            report_path = request.cwd / "phasea_test_result.json"
            report_path.write_text(
                json.dumps(
                    {
                        "type": "test_result",
                        "name": str(request.config.get("test_name") or f"{request.node_id}_model_agent"),
                        "passed": bool(request.config.get("test_passed", True)),
                        "provider": self.provider_name,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            output_files.append(str(report_path))
        return output_files

    def _overlooker_report(self, request: ModelAgentRequest) -> dict[str, Any]:
        packet = _read_json(request.cwd / "acceptance_packet.json")
        evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
        evidence_ref = (
            evidence.get("evidence_ref", {}).get("uri")
            if isinstance(evidence.get("evidence_ref"), dict)
            else None
        )
        validators = packet.get("validator_reports", [])
        worker = packet.get("worker") if isinstance(packet.get("worker"), dict) else {}
        worker_exit_code = int(worker.get("exit_code", 1))
        validators_passed = all(
            bool(item.get("passed")) for item in validators if isinstance(item, dict)
        ) and bool(validators)
        validator_refs = [
            str(item.get("validator_id"))
            for item in validators
            if isinstance(item, dict) and item.get("validator_id")
        ]
        if worker_exit_code == 0 and validators_passed and evidence_ref:
            return {
                "verdict": "pass",
                "confidence": "high",
                "rationale": "Deterministic model Overlooker accepted validator-backed evidence.",
                "evidence_ref": evidence_ref,
                "cited_evidence": [evidence_ref],
                "validator_refs": validator_refs,
                "failure_type": None,
                "recommended_action": "advance",
                "release_overlooker": True,
            }
        return {
            "verdict": "fail",
            "confidence": "high",
            "rationale": "Deterministic model Overlooker rejected missing or failing evidence.",
            "evidence_ref": evidence_ref,
            "cited_evidence": [evidence_ref] if evidence_ref else [],
            "validator_refs": validator_refs,
            "failure_type": "worker_failure" if worker_exit_code != 0 else ("validator_failure" if validators else "missing_evidence"),
            "recommended_action": "retry_same_node",
            "release_overlooker": False,
        }

    def _fork_decision(self, request: ModelAgentRequest) -> dict[str, Any]:
        packet = _read_json(request.cwd / "fork_advisor_input.json")
        candidates = [
            item
            for item in packet.get("candidate_nodes", [])
            if isinstance(item, dict)
            and item.get("status") == "NODE_ACCEPTED"
            and item.get("accepted_workspace")
        ]
        selected = str(candidates[-1].get("node_id")) if candidates else None
        return {
            "selected_node_id": selected,
            "rationale": "Select the latest accepted upstream workspace for retry.",
        }

    def _integration_report(self, request: ModelAgentRequest) -> dict[str, Any]:
        packet = _read_json(request.cwd / "integration_packet.json")
        candidates = packet.get("branch_candidates", [])
        branch_refs = [
            str(item.get("branch_candidate_ref"))
            for item in candidates
            if isinstance(item, dict) and item.get("branch_candidate_ref")
        ]
        if candidates and len(branch_refs) == len(candidates):
            return {
                "verdict": "pass",
                "recommended_action": "advance",
                "failure_type": None,
                "rationale": "All branch candidates are present.",
                "branch_candidate_refs": branch_refs,
                "permission_escalation_required": False,
                "human_review_required": False,
                "permission_review_required_for": [],
                "human_review_required_for": [],
                "second_overlooker_required_for": [],
            }
        return {
            "verdict": "blocked",
            "recommended_action": "require_human_review",
            "failure_type": "missing_branch_candidate",
            "rationale": "One or more branch candidates are missing.",
            "branch_candidate_refs": branch_refs,
            "permission_escalation_required": False,
            "human_review_required": True,
            "permission_review_required_for": [],
            "human_review_required_for": [],
            "second_overlooker_required_for": [],
        }

    def _graph_patch(self, request: ModelAgentRequest) -> dict[str, Any]:
        state = _read_json(request.cwd / "director_runtime_state.json")
        triggering = state.get("triggering_node", {})
        node_id = str(triggering.get("node_id") or request.node_id)
        graph_id = str(state.get("graph_id") or "graph")
        failure_code = triggering.get("failure_code")
        recommended = triggering.get("overlooker_recommended_action")
        return {
            "patch_id": f"model-agent-graph-patch-{node_id}",
            "director_id": "model-agent-director",
            "graph_id": graph_id,
            "triggering_node_id": node_id,
            "triggering_event": "overlooker_rejected_node",
            "overlooker_report_ref": triggering.get("overlooker_report_ref"),
            "operations": [
                {
                    "op": "retry_node",
                    "node_id": node_id,
                    "source_node_id": None,
                    "target_node_id": None,
                    "value": {
                        "failure_code": failure_code,
                        "recommended_action": recommended,
                    },
                    "rationale": "Retry the rejected node through a compiler-validated model-agent GraphPatch.",
                }
            ],
            "rationale": "Deterministic model Director selected bounded retry.",
            "replan_budget_cost": 1,
        }

    def _jsonl(self, events: list[dict[str, Any]]) -> str:
        return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


class OpenAICompatibleChatProvider:
    provider_name = "openai_compatible"

    def run(self, request: ModelAgentRequest) -> ModelAgentResult:
        config = request.config
        model = request.model or os.environ.get("MODEL_AGENT_MODEL") or os.environ.get("OPENAI_MODEL")
        if not model:
            return self._error(request, "MODEL_AGENT_MODEL or node.model is required")
        api_key_env = str(config.get("api_key_env") or "MODEL_AGENT_API_KEY")
        api_key = os.environ.get(api_key_env) or os.environ.get("OPENAI_API_KEY")
        allow_no_api_key = (
            bool(config.get("allow_no_api_key"))
            or os.environ.get("MODEL_AGENT_ALLOW_NO_API_KEY") == "1"
            or request.provider == "local_openai_compatible"
        )
        if not api_key and not allow_no_api_key:
            return self._error(request, f"{api_key_env} or OPENAI_API_KEY is required")
        endpoint = str(config.get("endpoint") or os.environ.get("MODEL_AGENT_ENDPOINT") or "")
        if not endpoint:
            base_url = str(
                config.get("base_url")
                or os.environ.get("MODEL_AGENT_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1"
            )
            endpoint = f"{base_url.rstrip('/')}/chat/completions"

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": request.system_prompt
                    or "You are a precise model-backed EGTC-PAW agent. Return only the requested artifact content.",
                },
                {"role": "user", "content": request.prompt},
            ],
            "temperature": float(config.get("temperature", 0)),
        }
        if "max_tokens" in config:
            payload["max_tokens"] = int(config["max_tokens"])
        if request.output_json and bool(config.get("enforce_json_response_format")):
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_sec) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            stderr = exc.read().decode("utf-8", errors="replace")
            return self._error(request, f"HTTP {exc.code}: {stderr}", network_attempts=1)
        except Exception as exc:
            return self._error(request, str(exc), network_attempts=1)

        data = _loads_dict(raw)
        content = self._message_content(data)
        parsed = _extract_json_object(content) if request.output_json else None
        if request.output_json and parsed is None:
            return self._error(
                request,
                "model response was not a JSON object",
                raw_response=content,
                network_attempts=1,
            )
        output_files = self._write_outputs(request, content, parsed)
        stdout = self._jsonl(
            [
                {
                    "type": "model_agent_started",
                    "provider": self.provider_name,
                    "model": model,
                    "node_id": request.node_id,
                    "role": request.role,
                },
                {
                    "type": "model_agent_response",
                    "provider": self.provider_name,
                    "model": model,
                    "node_id": request.node_id,
                    "output_files": output_files,
                    "json_output": parsed is not None,
                },
            ]
        )
        return ModelAgentResult(
            provider=self.provider_name,
            model=model,
            exit_code=0,
            stdout=stdout,
            stderr="",
            raw_response=content,
            parsed_json=parsed,
            output_files=output_files,
            usage=data.get("usage", {}) if isinstance(data.get("usage"), dict) else {},
            network_attempt_count=1,
        )

    def _message_content(self, data: dict[str, Any]) -> str:
        choices = data.get("choices", [])
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            content = message.get("content")
            return content if isinstance(content, str) else ""
        return ""

    def _write_outputs(
        self,
        request: ModelAgentRequest,
        content: str,
        parsed: dict[str, Any] | None,
    ) -> list[str]:
        if not request.output_file:
            return []
        path = _safe_workspace_path(request.cwd, request.output_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if request.output_json and parsed is not None:
            path.write_text(json.dumps(parsed, indent=2, sort_keys=True), encoding="utf-8")
        else:
            path.write_text(content, encoding="utf-8")
        return [str(path)]

    def _error(
        self,
        request: ModelAgentRequest,
        message: str,
        *,
        raw_response: str = "",
        network_attempts: int = 0,
    ) -> ModelAgentResult:
        stdout = self._jsonl(
            [
                {
                    "type": "model_agent_error",
                    "provider": self.provider_name,
                    "model": request.model,
                    "node_id": request.node_id,
                    "message": message,
                }
            ]
        )
        return ModelAgentResult(
            provider=self.provider_name,
            model=request.model,
            exit_code=1,
            stdout=stdout,
            stderr=message,
            raw_response=raw_response,
            network_attempt_count=network_attempts,
        )

    def _jsonl(self, events: list[dict[str, Any]]) -> str:
        return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


class ModelAgentRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelAgentProvider] = {
            "deterministic": DeterministicModelAgentProvider(),
            "openai_compatible": OpenAICompatibleChatProvider(),
            "openai-compatible": OpenAICompatibleChatProvider(),
            "chat_completions": OpenAICompatibleChatProvider(),
            "local_openai_compatible": OpenAICompatibleChatProvider(),
        }

    def get(self, provider_name: str | None) -> ModelAgentProvider:
        name = provider_name or os.environ.get("MODEL_AGENT_PROVIDER") or "deterministic"
        provider = self._providers.get(name)
        if provider is None:
            supported = ", ".join(sorted(self._providers))
            raise ValueError(f"unsupported model provider {name!r}; supported: {supported}")
        return provider


def _safe_workspace_path(cwd: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute():
        raise ValueError("model agent output_file must be relative to workspace")
    resolved = (cwd / path).resolve()
    cwd_resolved = cwd.resolve()
    if cwd_resolved != resolved and cwd_resolved not in resolved.parents:
        raise ValueError("model agent output_file escapes workspace")
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _loads_dict(path.read_text(encoding="utf-8"))


def _loads_dict(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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
