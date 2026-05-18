# Public Testing Guide

This repository is a prototype runtime for testing EGTC-PAW multi-agent workflow orchestration.

## Quick Start

```bash
python3 -m compileall egtc_runtime_stagea examples scripts
python3 examples/phase_g_workflow_learning_demo.py
python3 examples/phase_e_branch_integration_demo.py
python3 examples/phase_f_experience_library_demo.py
```

These commands use deterministic local subprocess workers and do not require external API keys.

## Real Codex CLI Path

If `codex` is available on `PATH`, or `CODEX_BIN` points to a Codex CLI binary, this command launches real Codex sessions for the worker, Director GraphPatch step, and fork Overlooker:

```bash
python3 examples/phase_d_codex_retry_fork_demo.py
```

## Runtime Outputs

Demo runs create local evidence, checkpoint, and artifact folders. They are reproducible runtime outputs and are ignored by Git.

## Current Scope

The current public testbed covers:

- Stage A evidence collection and Overlooker gating.
- Phase B Director structured planning.
- Phase C sandbox/resource reporting.
- Phase D graph runtime, retry, checkpoint/resume, and fork-from-clean-upstream behavior.
- Phase E branch-candidate integration and Overlooker-owned review requests.
- Phase F experience-library retrieval and proposal generation.
- Phase G workflow-level learning after graph completion, including dynamic replan and branch integration events.
