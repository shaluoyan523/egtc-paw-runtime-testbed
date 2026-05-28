from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .bridge import BridgeConfig, ThreeSoftwareMcpBridge


PROTOCOL_VERSION = "2025-11-25"


class JsonRpcMcpServer:
    def __init__(self, bridge: ThreeSoftwareMcpBridge) -> None:
        self.bridge = bridge

    def serve(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = self.handle(request)
            except Exception as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": str(exc)},
                }
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
                sys.stdout.flush()

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "three-software-mcp-bridge",
                        "version": "0.1.0",
                    },
                },
            )
        if method == "tools/list":
            return self._result(request_id, {"tools": self.bridge.tool_definitions()})
        if method == "tools/call":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            name = str(params.get("name", ""))
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            with _redirect_fd_stdout_to_stderr():
                result = self.bridge.dispatch_tool(name, arguments)
            is_error = result.get("status") in {"fail", "blocked"}
            return self._result(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False),
                        }
                    ],
                    "isError": bool(is_error),
                },
            )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def _result(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


@contextlib.contextmanager
def _redirect_fd_stdout_to_stderr():
    """Keep stdio MCP stdout clean even when solver libraries log to fd 1."""

    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout_fd = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout_fd, 1)
        os.close(saved_stdout_fd)


def load_config(
    path: Path | None,
    *,
    mock_hfss: bool,
    workspace_root: Path | None,
) -> BridgeConfig:
    if path and path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = {}
    if mock_hfss:
        raw["mock_hfss"] = True
    if workspace_root:
        raw["workspace_root"] = str(workspace_root)
    return BridgeConfig.from_dict(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Three-software EM MCP Bridge server")
    parser.add_argument("--config", type=Path, help="JSON config path")
    parser.add_argument("--workspace-root", type=Path, help="Directory for bridge run artifacts")
    parser.add_argument("--mock-hfss", action="store_true", help="Use mock HFSS backend for local smoke tests")
    parser.add_argument("--describe", action="store_true", help="Print bridge capabilities and exit")
    parser.add_argument("--run-pipeline-json", type=Path, help="Run one pipeline from JSON input and exit")
    args = parser.parse_args(argv)

    config = load_config(
        args.config,
        mock_hfss=args.mock_hfss,
        workspace_root=args.workspace_root,
    )
    bridge = ThreeSoftwareMcpBridge(config)
    if args.describe:
        print(json.dumps(bridge.describe_capabilities(), indent=2, sort_keys=True))
        return 0
    if args.run_pipeline_json:
        payload = json.loads(args.run_pipeline_json.read_text(encoding="utf-8"))
        print(json.dumps(bridge.run_pipeline(payload), indent=2, sort_keys=True))
        return 0
    JsonRpcMcpServer(bridge).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
