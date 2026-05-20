# EGTC-PAW Runtime v4 Stage A

Public testbed for EGTC-PAW staged multi-agent runtime experiments. For a short tester-oriented entry point, see `PUBLIC_TESTING.md`.

This folder deploys Phase A from `egtc_paw_runtime_v4_engineering_blueprint_trust_artifacts_sandbox.md`.

Phase A goal:

```text
single worker execution -> WorkerSubmitted -> evidence -> validators -> Overlooker -> NodeAccepted
```

Implemented components:

- `AgentExecWrapper`: subprocess, Codex CLI, and provider-backed `model_agent` runner with JSONL event parsing.
- `ModelAgentRegistry`: provider interface for non-Codex model agents, including deterministic offline tests and OpenAI-compatible chat endpoints.
- `ArtifactStore`: local content-addressable artifact store.
- `IdentityService`: basic `ActorIdentity` and HMAC `CapabilityToken`.
- `NodeCapsule` / `EvidenceBundle`: Stage A schemas.
- `DeterministicValidator`: evidence ref, required artifact, integrity, test, and diff checks.
- `ModelOverlooker`: default provider-backed reviewer; cannot pass without `evidence_ref`.
- `CodexOverlooker`: optional Codex-backed reviewer for explicit Codex compatibility tests.
- `EventLog`: SQLite append-only runtime event log.
- `StageARuntime`: single-node orchestration with `model_agent` Overlooker by default.

Run the demo:

```bash
cd /home/dataset-local/data1/egtc_paw_runtime_stageA
python3 examples/stagea_demo.py
```

Run a ModelScope SWE-bench smoke test:

```bash
cd /home/dataset-local/data1/egtc_paw_runtime_stageA
python3 examples/swe_phasea_smoke.py --split train --scan-limit 80 --count 3
```

This downloads/streams `AI-ModelScope/SWE-bench`, selects small cases by patch and issue size, and runs them through the Stage A evidence chain. It is a static Phase A runtime smoke test; it does not clone target repositories or run the official SWE-bench unit-test harness.

Run the same style of smoke test with a real Codex CLI worker:

```bash
cd /home/dataset-local/data1/egtc_paw_runtime_stageA
python3 examples/swe_phasea_codex_smoke.py --split train --scan-limit 40 --count 1
```

This path launches `codex exec --json` and captures Codex JSONL events as worker evidence.

Run the Phase B Director Agent v1 planning demo:

```bash
cd /home/dataset-local/data1/egtc_paw_runtime_stageA
python3 examples/phaseb_director_demo.py
```

Run compiler negative checks:

```bash
python3 examples/phaseb_compiler_negative_demo.py
```

This intentionally asks for ungrounded network access and a sensitive write path; the compiler should reject it.

Run Phase B against complex SWE-bench cases:

```bash
python3 examples/phaseb_swe_complex_demo.py --split train --scan-limit 500 --count 3
```

The selector scores cases by patch size, test patch size, issue length, `FAIL_TO_PASS` count, and preferred large Python repositories.

Run Phase B with a real Codex Director Agent session:

```bash
python3 examples/phaseb_codex_director_complex_demo.py --split train --scan-limit 120
```

This creates a Director workspace, launches `codex exec --json`, requires `director_output.json`, and validates the Director's structured output.

Run Phase B with all agent roles performed by Codex:

```bash
python3 examples/phaseb_all_codex_agents_complex_demo.py --split train --scan-limit 120
```

This launches Codex for the Director Agent, one Codex worker per Director-created node, and a Codex Overlooker.

Run the Phase C sandbox/resource demo:

```bash
python3 examples/phasec_sandbox_demo.py
```

Phase C adds Codex-native sandbox mapping, `network:none` sandbox events, resource limits, `ResourceReport`, and evidence artifacts for sandbox event streams.

Run Phase C against a complex SWE-bench case with all roles performed by Codex:

```bash
python3 examples/phasec_all_codex_complex_demo.py --split train --scan-limit 120
```

This launches the Director Agent, one Codex worker per Director-created node, and the Codex overlooker through the shared agent wrapper. The report verifies that every agent session produced Phase C sandbox events and a resource report.

Run the Phase D graph runtime demo:

```bash
python3 examples/phase_d_graph_runtime_demo.py
```

Phase D adds a DAG scheduler with parallel read-only workers, a single-writer lock, checkpoint/resume, retry budget, and livelock/deadlock detection. The demo pauses after two accepted diagnosis nodes, resumes from checkpoint, serializes competing writer nodes, retries one flaky verification node from a clean accepted upstream fork, and finishes accepted.

Run the Phase D retry-fork path with a real Codex worker session:

```bash
python3 examples/phase_d_codex_retry_fork_demo.py
```

This launches `codex exec --json` for the retrying worker node, the Codex Overlooker, the Codex Director GraphPatch step, and the retry fork Overlooker. Attempt 1 poisons its workspace and fails; the Overlooker rejects the node, the Director emits a compiler-validated `retry_node` `GraphPatch`, and attempt 2 is forked from the accepted baseline workspace. It succeeds only if the poison file is absent.

Run the Phase E conflict arbitration demo:

```bash
python3 examples/phase_e_branch_integration_demo.py
python3 examples/phase_e_conflict_runtime_demo.py
python3 examples/phase_e_overlooker_review_request_demo.py
```

Run the Phase E GraphPatch compiler checks:

```bash
python3 examples/phase_e_graph_patch_compiler_demo.py
```

Phase E adds branch-candidate integration: each serial agent completes its own branch workspace first, then a final integration Overlooker gates acceptance. Permission-review and human-review requests are decided by the Overlooker rather than by realtime Director arbitration.

Run the Phase F experience-library demos:

```bash
python3 examples/phase_f_experience_catalog_demo.py
python3 examples/phase_f_experience_library_demo.py
python3 examples/phase_f_experience_compiler_negative_demo.py
python3 examples/phase_f_director_planning_skill_negative_demo.py
python3 examples/phase_f_codex_director_experience_demo.py
```

Export the agent-readable experience catalog:

```bash
python3 scripts/export_experience_catalog.py --output-dir phasef_experience_catalog_data/catalog
```

The committed seed catalog is also available at `docs/experience_catalog_seed_v1/index.json`, with one pattern JSON per file under `docs/experience_catalog_seed_v1/patterns/`, for agents that should read the pattern set without importing Python.

Phase F adds an evolvable `ExperienceLibrary` of agent-readable orchestration patterns. The default catalog includes engineering runtime patterns and paper-derived patterns for proposer/aggregator layers, graph message passing, dynamic topology routing, learned routing gates, hierarchical memory planning, large-scale hierarchy, evolutionary or self-rectifying agent generation, cross-team governance, company-style role lifecycle, topology security controls, verification-aware planning, intervention debugging, context-efficient tool planning, disagreement-based recruitment, and protocol-aware communication.

Run the Phase G workflow-learning demo:

```bash
python3 examples/phase_g_workflow_learning_demo.py
```

Phase G adds Hermes-style workflow learning after each completed graph run. The runtime records workflow-level observations into the experience library, including selected pattern ids, node outcomes, retry count, Director GraphPatch/replan events, Overlooker fork events, branch candidates, and final integration decisions. Accepted workflows can promote patterns, failed workflows can demote patterns, and workflows that only succeed after dynamic updates create revision proposals so the successful correction path can be reviewed and folded back into the experience library.

Run the Phase H model-agent demos:

```bash
python3 examples/phase_h_model_agent_unit_demo.py
python3 examples/phase_h_model_director_demo.py
python3 examples/phase_h_model_retry_fork_demo.py
```

Phase H adds provider-backed agent units so Director, Worker, Overlooker, fork advisor, and integration-review sessions no longer need to be hard-bound to Codex CLI. `executor_kind="model_agent"` uses `model_provider`, `model`, and `model_config` on `NodeCapsule`. The offline provider is `deterministic`; real compatible services can use:

```bash
MODEL_AGENT_PROVIDER=openai_compatible
MODEL_AGENT_BASE_URL=https://your-compatible-endpoint/v1
MODEL_AGENT_API_KEY=...
MODEL_AGENT_MODEL=...
```

The OpenAI-compatible path calls `/chat/completions` and writes requested structured artifacts through `model_config.output_file`.

Director deliberation skill:

```text
skills/director-deliberative-planning/SKILL.md
skills/director-deliberative-planning/references/planning_schema.md
```

The Codex Director receives this skill in `director_input.json` and must produce a linear requirement flow, per-stage structure decisions, research-route decisions, per-stage agent allocation, and a plan derivation trace before emitting final workflow nodes.

Phase B adds:

- `DirectorAgentV1`: staged central planner named Director Agent.
- `TaskDiagnosis`: task kind, risk, touchpoints, test/code-change needs, unknowns.
- `WorkflowSkeleton`: conservative topology and skeleton nodes.
- `NodeInstantiation`: concrete `NodeCapsule` generation.
- `PermissionGroundingReport`: sandbox profile grounded by `RepoPolicy`.
- `WorkflowCompiler`: structured validation for node ids, skeleton coverage, commands, paths, network, and acceptance criteria.

Phase D adds:

- `GraphRuntime`: multi-node DAG scheduler layered on the Stage A/C execution chain.
- `GraphRunSpec`: graph nodes, edges, parallelism, retry, overlooker-mode policy, and Director GraphPatch mode.
- Checkpoint/resume under `phased_graph_data/checkpoints`.
- Parallel read-only execution with single-writer scheduling.
- Retry budget plus repeated-failure livelock guard.
- v5-style `OverlookerReport` fields: `confidence`, `cited_evidence`, `failure_type`, `recommended_action`, and `release_overlooker`.
- Stage D `GraphPatch` / `CompiledGraphPatch` schemas with compiler validation for bounded `retry_node` patches.
- Director runtime GraphPatch application before retry scheduling.
- Overlooker-guided retry fork points so failed attempts do not poison the next attempt workspace.

Phase E adds:

- `DecisionConflict` and `ConflictResolution` schemas.
- Branch candidates for serial agent outputs before unified integration.
- High-risk node detection from phase and sandbox profile.
- Second Overlooker branch gate before high-risk candidates enter integration.
- Overlooker-owned human-review and permission-escalation decisions.
- Deferred integration gate after serial nodes finish, instead of realtime Director conflict arbitration.
- Phase E GraphPatch compiler validation for `insert_node`, `split_node`, `replace_worker`, `add_edge`, `remove_edge`, and `update_join_policy`.

Phase F adds:

- `ExperiencePattern`, `ExperienceObservation`, and `ExperienceUpdateProposal` schemas.
- `ExperienceLibrary`: JSONL-backed seed, retrieve, observe, update-proposal, and catalog-export operations.
- Agent-readable default orchestration patterns distilled from `多agent编排报告_20260421.zip`.
- Director experience selection with pattern ids propagated into `TaskDiagnosis`, `WorkflowSkeleton`, and `NodeCapsule`.
- `director-deliberative-planning` skill for staged Director planning before workflow emission.
- Codex Director experience planning that compares alternatives, chooses agent allocation, and emits scaling policy before compiler validation.
- Compiler checks that experience patterns can shape workflow structure but cannot request permissions, sandbox changes, network, or secrets.
- Compiler checks that Codex Director plans include linear requirement flow, stage structure decisions, research-route decisions, per-stage agent allocation, and node derivation trace.

Phase G adds:

- `WorkflowExperienceObservation` for graph-level run outcomes.
- JSONL-backed workflow observation persistence under the same `ExperienceLibrary`.
- Automatic workflow update proposals after non-paused graph runs.
- Learning from dynamic workflow updates, including Director GraphPatch application, retry scheduling, Overlooker fork selection, Phase E branch candidates, and final integration gates.

Phase H adds:

- `ModelAgentRequest`, `ModelAgentResult`, and `ModelAgentRegistry`.
- Deterministic and OpenAI-compatible model providers.
- `NodeCapsule.model_provider`, `NodeCapsule.model`, and `NodeCapsule.model_config`.
- `executor_kind="model_agent"` in the shared execution wrapper with artifact, sandbox-event, and resource-report capture.
- `DirectorAgentV1.plan_with_model_director` for non-Codex Director sessions.
- `ModelOverlooker` and model-agent paths for fork advisor, Director GraphPatch, and Phase E integration review.
- Runtime summaries expose `workflow_learning` with the recorded observation and proposed experience updates.

Deferred beyond Phase G:

- Secret handling, redaction, artifact secret scanning, and stronger per-run isolation boundaries.

Publish as a private GitHub repository:

```bash
export GITHUB_TOKEN=...
python3 scripts/publish_github_private.py --repo egtc-paw-runtime-stagea
```

Use `--org ORG_NAME` to create the private repository under an organization.

Expected result:

- The worker reaches `WorkerSubmitted` first.
- Runtime collects `diff`, `test`, `log`, `stderr`, and `worker_events` artifacts.
- Validators run before the Overlooker.
- The Overlooker report includes `evidence_ref`.
- Final state becomes `NodeAccepted`.

Runtime data is written under:

```text
/home/dataset-local/data1/egtc_paw_runtime_stageA/runtime_data
```

Runtime data and SWE smoke outputs are excluded from Git by `.gitignore`.
