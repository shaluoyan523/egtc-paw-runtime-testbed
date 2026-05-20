from __future__ import annotations

from .codex_wrapper import CodexExecWrapper


class AgentExecWrapper(CodexExecWrapper):
    """Provider-agnostic agent launcher.

    This is the preferred name for new runtime code. `CodexExecWrapper` remains
    as a backward-compatible implementation class for older demos.
    """
