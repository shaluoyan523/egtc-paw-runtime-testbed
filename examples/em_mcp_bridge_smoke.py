from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def send(process: subprocess.Popen[str], request: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("MCP bridge produced no response")
    return json.loads(line)


def main() -> int:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "em_mcp_bridge.server",
            "--mock-hfss",
            "--config",
            str(ROOT / "examples" / "em_mcp_bridge_config.json"),
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        init = send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0.1"},
                },
            },
        )
        tools = send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        design = json.loads((ROOT / "examples" / "em_bridge_design_input.json").read_text(encoding="utf-8"))
        call = send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "bridge.run_pipeline", "arguments": design},
            },
        )
    finally:
        process.kill()
        _stdout, stderr = process.communicate(timeout=5)

    content = call["result"]["content"][0]["text"]
    pipeline = json.loads(content)
    report = {
        "initialized": init["result"]["serverInfo"]["name"] == "three-software-mcp-bridge",
        "tool_count": len(tools["result"]["tools"]),
        "pipeline_status": pipeline["status"],
        "bridge_report": pipeline["artifacts"]["bridge_report"],
        "stderr": stderr,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["initialized"] and report["tool_count"] >= 7 and report["pipeline_status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
