from __future__ import annotations

from copy import deepcopy
from typing import Any


SWE_DATASET_TOOLING_PROFILE_ID = "swe_dataset_testing_v1"


def swe_dataset_tooling_profile() -> dict[str, Any]:
    """Tool and MCP manifest for the SWE/ModelScope dataset tests.

    The profile is intentionally declarative. A provider-backed model agent can
    read it as a capability contract, while the compiler can validate that the
    requested capabilities have explicit permission boundaries.
    """

    tools = [
        {
            "tool_id": "filesystem.read_text",
            "tool_type": "filesystem",
            "description": "Read repo files, runtime artifacts, and selected SWE case JSON files inside the granted workspace.",
            "mcp_server_id": "egtc.filesystem",
            "permissions": ["read"],
            "requires_network": False,
            "input_contract": {"paths": "relative workspace paths only"},
            "output_contract": {"content": "UTF-8 text or JSON-compatible payloads"},
        },
        {
            "tool_id": "filesystem.write_artifact",
            "tool_type": "filesystem",
            "description": "Write bounded reports such as worker_report.json, overlooker_report.json, summaries, and test artifacts.",
            "mcp_server_id": "egtc.filesystem",
            "permissions": ["write"],
            "requires_network": False,
            "input_contract": {"paths": "relative workspace paths allowed by sandbox_profile.allowed_write_paths"},
            "output_contract": {"artifacts": "files cited by evidence bundles"},
        },
        {
            "tool_id": "filesystem.search",
            "tool_type": "filesystem",
            "description": "Search local repo or copied dataset case files for symbols, filenames, and evidence strings.",
            "mcp_server_id": "egtc.filesystem",
            "permissions": ["read"],
            "requires_network": False,
            "preferred_command": ["rg"],
            "fallback_command": ["grep", "-R"],
            "input_contract": {"query": "literal or regex search term", "root": "relative path"},
            "output_contract": {"matches": "path, line, and snippet summaries"},
        },
        {
            "tool_id": "git.inspect",
            "tool_type": "git",
            "description": "Inspect repository state, diffs, commits, and patch surfaces without mutating history.",
            "mcp_server_id": "egtc.git",
            "permissions": ["read"],
            "requires_network": False,
            "allowed_commands": [
                ["git", "status", "--short"],
                ["git", "diff"],
                ["git", "show"],
                ["git", "log", "--oneline"],
            ],
            "output_contract": {"evidence": "diff and commit metadata summaries"},
        },
        {
            "tool_id": "python.run_script",
            "tool_type": "python",
            "description": "Run bounded local Python scripts used by the Stage A-H smoke and graph demos.",
            "mcp_server_id": "egtc.python",
            "permissions": ["execute"],
            "requires_network": False,
            "allowed_commands": [["python3", "examples/*.py"]],
            "input_contract": {"script": "repo-relative Python script", "args": "bounded CLI args"},
            "output_contract": {"stdout": "JSON or JSONL events when available", "exit_code": "integer"},
        },
        {
            "tool_id": "python.compileall",
            "tool_type": "python",
            "description": "Compile Python packages and examples as a low-cost syntax regression check.",
            "mcp_server_id": "egtc.python",
            "permissions": ["execute"],
            "requires_network": False,
            "allowed_commands": [["python3", "-m", "compileall", "egtc_runtime_stagea", "examples", "scripts"]],
            "output_contract": {"exit_code": "0 when syntax compilation succeeds"},
        },
        {
            "tool_id": "python.pytest",
            "tool_type": "python",
            "description": "Run repo-grounded pytest checks when the selected project or copied task workspace provides tests.",
            "mcp_server_id": "egtc.python",
            "permissions": ["execute"],
            "requires_network": False,
            "allowed_commands": [["python3", "-m", "pytest"]],
            "output_contract": {"test_report": "pytest exit code and concise failure summaries"},
        },
        {
            "tool_id": "dataset.modelscope_swe_stream",
            "tool_type": "dataset",
            "description": "Stream AI-ModelScope/SWE-bench rows with modelscope.msdatasets.MsDataset.load(..., use_streaming=True).",
            "mcp_server_id": "egtc.dataset",
            "permissions": ["network", "read"],
            "requires_network": True,
            "python_packages": ["modelscope", "datasets", "pandas", "pyarrow", "httpx[socks]"],
            "optional_env": ["MODELSCOPE_CACHE", "HF_HOME", "HF_DATASETS_CACHE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
            "input_contract": {
                "dataset": "SWE-bench",
                "namespace": "AI-ModelScope",
                "split": "train|test|validation",
                "scan_limit": "positive integer",
            },
            "output_contract": {
                "rows": "dicts containing instance_id, repo, patch, test_patch, problem_statement, and FAIL_TO_PASS when present"
            },
        },
        {
            "tool_id": "dataset.hf_swe_stream",
            "tool_type": "dataset",
            "description": "Fallback Hugging Face datasets streaming path for SWE-like parquet datasets when ModelScope mirrors are unavailable.",
            "mcp_server_id": "egtc.dataset",
            "permissions": ["network", "read"],
            "requires_network": True,
            "python_packages": ["datasets", "pyarrow", "httpx[socks]"],
            "optional_env": ["HF_HOME", "HF_DATASETS_CACHE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
            "input_contract": {"dataset_id": "dataset repository id", "split": "split name", "streaming": True},
            "output_contract": {"rows": "streamed dataset rows with SWE-compatible fields when available"},
        },
        {
            "tool_id": "dataset.select_swe_cases",
            "tool_type": "dataset",
            "description": "Score and select simple or complex SWE cases by patch size, test patch size, problem length, repo, and FAIL_TO_PASS count.",
            "mcp_server_id": "egtc.dataset",
            "permissions": ["read"],
            "requires_network": False,
            "input_contract": {"rows": "SWE rows already streamed or cached", "mode": "simple|complex", "count": "positive integer"},
            "output_contract": {"cases": "ranked selected SWE case summaries and serialized case JSON paths"},
        },
    ]
    mcp_servers = [
        {
            "server_id": "egtc.filesystem",
            "name": "EGTC workspace filesystem MCP",
            "transport": "runtime_manifest",
            "scope": "workspace",
            "tools": [
                "filesystem.read_text",
                "filesystem.write_artifact",
                "filesystem.search",
            ],
            "permission_boundary": "RepoPolicy allowed_read_paths and sandbox_profile.allowed_write_paths",
            "requires_network": False,
        },
        {
            "server_id": "egtc.git",
            "name": "EGTC git inspection MCP",
            "transport": "runtime_manifest",
            "scope": "workspace",
            "tools": ["git.inspect"],
            "permission_boundary": "read-only git inspection; no reset, checkout, push, or branch deletion",
            "requires_network": False,
        },
        {
            "server_id": "egtc.python",
            "name": "EGTC bounded Python execution MCP",
            "transport": "runtime_manifest",
            "scope": "workspace",
            "tools": ["python.run_script", "python.compileall", "python.pytest"],
            "permission_boundary": "only commands present in repo policy or node sandbox profile may execute",
            "requires_network": False,
        },
        {
            "server_id": "egtc.dataset",
            "name": "EGTC SWE dataset access MCP",
            "transport": "runtime_manifest",
            "scope": "dataset",
            "tools": [
                "dataset.modelscope_swe_stream",
                "dataset.hf_swe_stream",
                "dataset.select_swe_cases",
            ],
            "permission_boundary": "network dataset tools require explicit network permission and cache/proxy env grounding",
            "requires_network": True,
        },
    ]
    return {
        "profile_id": SWE_DATASET_TOOLING_PROFILE_ID,
        "description": "Capabilities used by the prior SWE-bench ModelScope dataset smoke, complex planning, and all-agent graph tests.",
        "tools": tools,
        "mcp_servers": mcp_servers,
        "required_python_packages": [
            "modelscope",
            "datasets",
            "pandas",
            "pyarrow",
            "httpx[socks]",
            "addict",
            "simplejson",
            "sortedcontainers",
        ],
        "optional_env": [
            "MODELSCOPE_CACHE",
            "HF_HOME",
            "HF_DATASETS_CACHE",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "MODEL_AGENT_PROVIDER",
            "MODEL_AGENT_BASE_URL",
            "MODEL_AGENT_API_KEY",
            "MODEL_AGENT_MODEL",
        ],
        "dataset_access": {
            "primary": {
                "dataset": "SWE-bench",
                "namespace": "AI-ModelScope",
                "loader": "modelscope.msdatasets.MsDataset.load",
                "streaming": True,
            },
            "fallback": {
                "loader": "datasets.load_dataset",
                "streaming": True,
            },
            "selection_modes": ["simple", "complex"],
        },
        "permission_notes": [
            "Filesystem and git tools remain workspace-scoped.",
            "Dataset streaming tools are visible to Director but executable only when network permission is grounded.",
            "Proxy-based dataset streaming requires httpx SOCKS support; install the modelscope extra from pyproject.toml.",
        ],
    }


def swe_dataset_model_config() -> dict[str, Any]:
    """Return model_config fields that attach the SWE dataset tooling profile."""

    profile = swe_dataset_tooling_profile()
    return {
        "tooling_profile": profile["profile_id"],
        "tools": deepcopy(profile["tools"]),
        "mcp_servers": deepcopy(profile["mcp_servers"]),
        "allowed_mcp_tools": [tool["tool_id"] for tool in profile["tools"]],
        "tool_env": {
            "optional": deepcopy(profile["optional_env"]),
            "required_python_packages": deepcopy(profile["required_python_packages"]),
        },
        "dataset_access": deepcopy(profile["dataset_access"]),
        "tooling_permission_notes": deepcopy(profile["permission_notes"]),
    }


def merge_model_config_tooling(
    model_config: dict[str, Any] | None,
    tooling_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a caller config over the default SWE dataset tooling config."""

    merged = deepcopy(tooling_config or swe_dataset_model_config())
    for key, value in (model_config or {}).items():
        merged[key] = value
    return merged
