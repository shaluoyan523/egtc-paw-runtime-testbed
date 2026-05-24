# Planning Schema

Director output must include these additional `workflow_skeleton` fields.

Director output must also include these top-level fields:

- `task_profile`
- `work_assignment_plan`
- `permission_plan`

## decision_basis

Every planning record below must include a `decision_basis` object:

```json
{
  "basis_id": "basis-stage-1",
  "source_refs": ["objective", "repo_policy.allowed_write_paths", "experience:seed-handoff-artifact-chain"],
  "matched_signals": ["complex SWE task", "requires tests", "network none"],
  "assumptions": ["The implementation surface is unknown before exploration."],
  "invalidation_signals": ["Exploration finds only one trivial file.", "Repo policy blocks all writes."],
  "confidence": "low|medium|high",
  "correction_target": "workflow_skeleton.stage_structure_decisions[stage-1]",
  "correction_action": "Replan this stage as single_agent or request human clarification."
}
```

Rules:

- `source_refs` must cite available inputs, repo policy fields, experience pattern ids, local artifacts, or prior stage outputs.
- `invalidation_signals` must describe evidence that would make the decision wrong.
- `correction_target` must name the workflow field, stage id, node id, or graph patch target to revise.
- `correction_action` must say what to change during dynamic replanning.

## task_profile

Emit `task_profile` at the top level and copy the same object into `task_diagnosis.task_profile`:

```json
{
  "primary_task_family": "code_repair",
  "task_families": ["code_repair", "terminal_execution"],
  "verification_methods": ["unit_tests", "container_test", "patch_review"],
  "knowledge_sources": ["prompt", "repo", "local_dataset", "tool_execution"],
  "predicted_failure_modes": ["ambiguity", "missing_dependency", "permission_insufficient", "environment_error", "patch_risk"],
  "risk_level": "low|medium|high",
  "estimated_difficulty": "low|medium|high",
  "estimated_budget": {
    "estimated_agents": 4,
    "estimated_tokens": 18000,
    "estimated_wall_time_sec": 480,
    "worth_multi_candidate": true
  },
  "budget_gate": {
    "max_agents_before_replan": 4,
    "max_tokens_before_replan": 36000,
    "max_wall_time_sec_before_replan": 900
  },
  "stop_condition": "Stop when required verification passes.",
  "escalation_condition": "Escalate when permission, knowledge source, or verifier reliability is insufficient.",
  "cheaper_alternative": "Use a single-agent cheap path when all knowledge is prompt-local and verification is direct."
}
```

Task family examples:

- BrowseComp: `retrieval`, verification by `external_fact_evidence` and source citation.
- Finance-Agent: `finance_calculation`, verification by formula and calculator evidence.
- SWE/Workbench: `code_repair`, verification by repo tests, patch review, and bounded ownership.
- Terminal-Bench: `terminal_execution`, verification by shell/container/checkpoint artifacts.
- PlanCraft: `planning_state_transition`, verification by ambiguity review and state transition checks.
- OpenDeepThink-style tasks: `contest_reasoning`, verification by judge, sample tests, pairwise ranking, and adaptive scaling.

Task family is a profiling signal, not a topology rule. The Director must still compare candidate plans and ground final nodes, agent counts, and permission intents in this task instance's verification methods, knowledge sources, capabilities, and constraints.

## linear_requirement_flow

List of ordered records:

```json
{
  "stage_id": "stage-1",
  "order": 1,
  "name": "Understand task and repo constraints",
  "purpose": "Find what must be changed and what must not be touched.",
  "inputs": ["objective", "repo_policy"],
  "outputs": ["touchpoint_map", "constraint_summary"],
  "risk_level": "low|medium|high",
  "acceptance_evidence": ["evidence_ref", "analysis_log"],
  "decision_basis": {
    "basis_id": "basis-stage-1",
    "source_refs": ["objective", "repo_policy", "experience_candidates"],
    "matched_signals": ["unknown implementation surface"],
    "assumptions": ["A planning stage will reduce downstream ambiguity."],
    "invalidation_signals": ["Objective already provides exact file and test."],
    "confidence": "medium",
    "correction_target": "linear_requirement_flow[stage-1]",
    "correction_action": "Remove or merge this stage if it proves redundant."
  }
}
```

## stage_structure_decisions

One record per linear stage:

```json
{
  "stage_id": "stage-1",
  "candidate_structures": [
    {
      "structure": "single_agent",
      "fit": "low|medium|high",
      "reason": "Why it could work."
    }
  ],
  "selected_structure": "parallel_exploration",
  "selection_reason": "Why this structure is selected for this stage.",
  "anti_signals": ["Signals that would make this structure wrong."],
  "experience_pattern_ids": ["seed-topology-parallel-explore-implement-verify"],
  "decision_basis": {
    "basis_id": "basis-structure-stage-1",
    "source_refs": ["experience:seed-topology-parallel-explore-implement-verify"],
    "matched_signals": ["parallelizable read-only discovery"],
    "assumptions": ["Subtasks can be explored without writes."],
    "invalidation_signals": ["Exploration requires generated files."],
    "confidence": "medium",
    "correction_target": "stage_structure_decisions[stage-1]",
    "correction_action": "Switch to single_agent or tool_planning and recompile edges."
  }
}
```

Allowed structures:

- `single_agent`
- `parallel_exploration`
- `specialist_pool`
- `proposer_aggregator`
- `graph_message_passing`
- `dynamic_routing`
- `hierarchical_subteams`
- `review_gate`
- `tool_planning`
- `research_route`

## research_route_decisions

List of records:

```json
{
  "stage_id": "stage-2",
  "research_needed": true,
  "reason": "The task names a framework or expert route not covered by local evidence.",
  "available_sources": ["experience_candidates", "repo_files", "bundled_docs"],
  "blocked_sources": ["external_web"],
  "planned_queries_or_searches": ["search repo docs for scheduler extension points"],
  "adopted_expert_route": "Use existing local experience pattern or repo-documented route.",
  "fallback_if_research_blocked": "Proceed with conservative exploration and require replan if evidence is insufficient.",
  "decision_basis": {
    "basis_id": "basis-research-stage-2",
    "source_refs": ["objective", "repo_files", "network:none"],
    "matched_signals": ["unfamiliar framework route"],
    "assumptions": ["Local docs contain enough route evidence."],
    "invalidation_signals": ["No local docs or examples mention the required route."],
    "confidence": "low",
    "correction_target": "research_route_decisions[stage-2]",
    "correction_action": "Request Director replan with a research node or ask for permission escalation."
  }
}
```

Every specialized or high-uncertainty stage must have a research route decision. If research is unnecessary, still emit a record with `research_needed=false` and a reason.

## per_stage_agent_allocation

List of records:

```json
{
  "stage_id": "stage-1",
  "agent_count": 2,
  "count_reason": "Two independent read-only surfaces need exploration.",
  "decision_basis": {
    "basis_id": "basis-allocation-stage-1",
    "source_refs": ["stage_structure_decisions[stage-1]", "repo_policy"],
    "matched_signals": ["two independent discovery surfaces"],
    "assumptions": ["The outputs can be joined before implementation."],
    "invalidation_signals": ["Explorers report overlapping scope or contradictory ownership."],
    "confidence": "medium",
    "correction_target": "per_stage_agent_allocation[stage-1]",
    "correction_action": "Merge explorers or add a synthesis node before implementation."
  },
  "agents": [
    {
      "role": "explorer",
      "task": "Map repo touchpoints.",
      "inputs": ["objective", "repo_policy"],
      "outputs": ["touchpoint_map"],
      "ownership_boundary": "Read-only repository analysis.",
      "write_authority": "none",
      "handoff_target": "implement",
      "decision_basis": {
        "basis_id": "basis-agent-stage-1-explorer",
        "source_refs": ["stage_structure_decisions[stage-1]"],
        "matched_signals": ["read-only touchpoint discovery"],
        "assumptions": ["A specialist explorer reduces implementation risk."],
        "invalidation_signals": ["No repo surface to inspect."],
        "confidence": "medium",
        "correction_target": "node:explore-context",
        "correction_action": "Remove or merge this agent during graph patching."
      }
    }
  ]
}
```

The sum of `agent_count` values must equal `workflow_skeleton.agent_allocation.total_agents` and the number of final skeleton nodes.

## work_assignment_plan

Emit a top-level `work_assignment_plan` with one record per final skeleton node:

```json
{
  "node_id": "explore-context",
  "role": "explorer",
  "stage_id": "stage-1",
  "agent_type": "expert|tool_or_expert|verification|generalist|candidate_generation|candidate_selection",
  "input_schema": {
    "required": ["objective", "repo_policy", "upstream_artifacts"],
    "upstream_nodes": []
  },
  "output_schema": {
    "required": ["touchpoint_map", "analysis_log"]
  },
  "ownership_boundary": "Read-only source touchpoint discovery.",
  "parallel_safe": true,
  "parallel_safety_reason": "Read-only and no dependency on peer explorer output.",
  "failure_takeover": "Overlooker may retry, fork from accepted upstream state, or request Director replan.",
  "selection_basis": "Explorer is needed because implementation surface is unknown.",
  "capability_needs": ["repo_read"]
}
```

This field is not a duplicate of `per_stage_agent_allocation`: it is the execution contract for each concrete node after the Director chooses the graph.

## permission_plan

Emit a top-level `permission_plan` with one record per node instantiation:

```json
{
  "node_id": "model-implement",
  "skeleton_node_id": "implement",
  "permission_intents": ["read_repo", "write_patch", "run_tests"],
  "minimum_boundary": {
    "read_paths": ["."],
    "write_paths": ["egtc_runtime_stagea", "examples"],
    "allowed_commands": [["python3", "-m", "compileall", "egtc_runtime_stagea"]],
    "network": "none",
    "secret_access": false
  },
  "why_needed": "Implementation needs bounded repo writes and repo-grounded tests.",
  "fallback_if_denied": "Return to Overlooker permission review and request a lower-permission Director replan."
}
```

Allowed `permission_intents`: `read_repo`, `write_patch`, `run_tests`, `run_shell`, `network_search`, `dataset_read`, `container_exec`, `finance_calculator`, `browser`.

Rules:

- `secret_access` must be false.
- Research, finance, retrieval, and verification roles should not receive repo write permission unless explicitly justified by the task.
- Network/browser/container intents must be routed through permission review when the repo policy or sandbox does not already ground them.

## scaling_policy

The Director must emit a `workflow_skeleton.scaling_policy` object. This policy is a learnable curriculum for the current graph, not a hard-coded algorithm name.

```json
{
  "policy_id": "seed-scaling-adaptive-population-curriculum",
  "current_scale_level": 2,
  "requested_scale_level": 2,
  "scale_level_name": "medium_pool_5_to_8_candidates_pairwise_K2_or_K3",
  "scale_triggers": [
    "candidate diversity is low",
    "selection uncertainty is high",
    "some candidates pass compile but fail semantic validation"
  ],
  "scale_down_triggers": [
    "no candidate is near-correct after the current budget tier",
    "pairwise judge disagrees with validator evidence"
  ],
  "max_planned_agents_for_current_task": 6,
  "expansion_strategy": [
    "increase candidate pool before adding mutation only when validator evidence shows partial competence",
    "increase comparison density when ranking uncertainty remains high",
    "route to research or a domain specialist before increasing population when all candidates share the same missing knowledge"
  ],
  "requires_replan_when": [
    "validator pass rate stays zero after the selected scale level",
    "token or latency budget is exceeded",
    "Overlooker recommends Director replan"
  ],
  "observations_to_record": [
    "candidate_count",
    "comparison_count",
    "mutation_rounds",
    "ranking_entropy",
    "validator_pass_rate",
    "retry_count",
    "replan_count",
    "token_cost",
    "latency",
    "next_scaling_hint"
  ],
  "budget_gate": {
    "max_candidate_count": 8,
    "max_comparison_count": 24,
    "max_mutation_rounds": 1,
    "max_planned_agents": 8
  },
  "decision_basis": {
    "basis_id": "basis-scaling-policy",
    "source_refs": ["objective", "experience:seed-scaling-adaptive-population-curriculum"],
    "matched_signals": ["complex verifiable task", "benchmark accuracy objective"],
    "assumptions": ["The current budget can support a medium candidate pool."],
    "invalidation_signals": ["All candidates fail for the same missing-domain reason.", "Validator is unavailable or unreliable."],
    "confidence": "medium",
    "correction_target": "workflow_skeleton.scaling_policy",
    "correction_action": "Scale up, scale down, or route to research/specialists before recompiling the graph."
  }
}
```

Scale levels:

- `0`: `single_candidate_baseline`
- `1`: `small_pool_2_to_3_candidates_all_pairs`
- `2`: `medium_pool_5_to_8_candidates_pairwise_K2_or_K3`
- `3`: `evolution_loop_with_elites_and_mutation`
- `4`: `large_population_BT_style_n12_to_n20_K4_T2_to_T3_M8_to_M10`

Rules:

- Do not jump to level 4 unless earlier evidence or task constraints justify the cost.
- Do not add more candidate agents when the observed failure is missing knowledge; add research, retrieval, or a domain-specialist route first.
- `observations_to_record` must include enough fields for workflow learning to decide whether to promote, demote, revise, scale up, or scale down the policy.
- `decision_basis` must explain why the current scale is selected and what evidence would change it.

## execution_estimate

Emit under `workflow_skeleton.execution_estimate`:

```json
{
  "estimated_agents": 4,
  "estimated_tokens": 18000,
  "estimated_wall_time_sec": 480,
  "expected_success_probability": 0.72,
  "budget_gate": {
    "max_agents_before_replan": 4,
    "max_tokens_before_replan": 36000,
    "max_wall_time_sec_before_replan": 900
  },
  "stop_condition": "Stop when required verification passes.",
  "escalation_condition": "Escalate when permissions, knowledge sources, or verifier reliability are insufficient.",
  "cheaper_alternative": "Use one agent when the task is prompt-local and direct answer matching is enough."
}
```

The estimate is a pre-run planning judgment. Runtime resource reports and workflow observations will later test whether the estimate was good.

## plan_derivation_trace

List of concise trace strings:

```json
[
  "basis-structure-stage-1: stage-1 selected parallel_exploration, producing nodes explore-context and explore-tests.",
  "basis-allocation-stage-2: stage-2 selected single_agent because writes are not yet provably independent, producing node implement."
]
```

The trace must connect the linear stages to final node ids.

## draft_plan_review

After planning and task decomposition, but before final workflow emission, the Director must feed the draft plan back into itself for structural review. Emit the review under `workflow_skeleton.draft_plan_review`:

```json
{
  "review_id": "draft-review-1",
  "reviewed_draft_fields": [
    "linear_requirement_flow",
    "stage_structure_decisions",
    "research_route_decisions",
    "per_stage_agent_allocation",
    "nodes",
    "edges",
    "node_instantiations",
    "experience_pattern_ids"
  ],
  "structural_verdict": "pass|revise_before_final|needs_human_review",
  "structure_findings": [
    {
      "finding_id": "draft-finding-1",
      "severity": "info|warning|error",
      "target": "workflow_skeleton.nodes[verify]",
      "finding": "Verification has no explicit handoff from implementation evidence.",
      "recommendation": "Add or reorder a verification node that consumes implementation outputs.",
      "decision_basis": {
        "basis_id": "basis-draft-review-1",
        "source_refs": ["draft_plan.nodes", "per_stage_agent_allocation[stage-3]"],
        "matched_signals": ["requires test evidence", "implementation writes"],
        "assumptions": ["Verifier can run read-only after implementation."],
        "invalidation_signals": ["Tests require write access or generated fixtures."],
        "confidence": "medium",
        "correction_target": "workflow_skeleton.nodes[verify]",
        "correction_action": "Add, remove, split, merge, or reorder the verification node."
      }
    }
  ],
  "missing_capabilities": ["synthesis", "integration_review"],
  "recommended_changes": [
    {
      "change_id": "draft-change-1",
      "change_type": "add_node|remove_node|split_node|merge_nodes|reorder_edge|change_role|change_evidence|change_agent_count|no_change",
      "target": "workflow_skeleton.nodes",
      "rationale": "Why the draft needs or does not need this change."
    }
  ],
  "applied_changes": [
    {
      "change_id": "draft-change-1",
      "applied": true,
      "final_targets": ["workflow_skeleton.nodes[verify]"],
      "result": "Final workflow includes a read-only verification node after implementation."
    }
  ],
  "rejected_changes": [
    {
      "change_id": "draft-change-2",
      "reason": "Rejected because it would exceed current task scope or duplicate another node."
    }
  ],
  "final_structure_summary": "Short statement explaining why the final graph is now structurally sound."
}
```

Rules:

- `reviewed_draft_fields` must name the planning, node, edge, instantiation, and experience fields that were reviewed.
- `structure_findings` must contain at least one finding. If no flaw is found, use an `info` finding that states what was checked and why no change is needed.
- Every structure finding must include `decision_basis`.
- `recommended_changes` and `applied_changes` must be non-empty. Use `change_type=no_change` only when the review found the draft already sufficient.
- If a change is applied, the final workflow fields must reflect it. If a change is rejected, `rejected_changes` must explain why.
- `structural_verdict=needs_human_review` should be used only when the Director cannot make a safe structural correction under the current policy.

## workflow_skeleton.nodes node_selection_principles

Every final skeleton node must include `node_selection_principles`:

```json
{
  "node_id": "explore-context",
  "phase": "exploration",
  "role": "explorer",
  "goal": "Map implementation touchpoints.",
  "depends_on": [],
  "expected_outputs": ["touchpoint_map"],
  "experience_pattern_ids": ["seed-topology-parallel-explore-implement-verify"],
  "node_selection_principles": {
    "stage_id": "stage-1",
    "selected_for": [
      "This node isolates read-only source discovery before any writer runs."
    ],
    "role_principle": "Explorer is selected because the node must inspect and report without editing.",
    "dependency_principle": "No predecessors are required because this is an initial discovery node.",
    "parallelism_principle": "It can run in parallel with validation exploration because both are read-only and have disjoint output ownership.",
    "evidence_principle": "touchpoint_map is sufficient for the writer to choose bounded files.",
    "experience_pattern_ids": ["seed-topology-parallel-explore-implement-verify"],
    "decision_basis": {
      "basis_id": "basis-node-explore-context",
      "source_refs": [
        "per_stage_agent_allocation[stage-1]",
        "experience:seed-topology-parallel-explore-implement-verify"
      ],
      "matched_signals": ["unknown implementation surface", "parallelizable read-only discovery"],
      "assumptions": ["Source and validation discovery can be separated cleanly."],
      "invalidation_signals": ["The repo has only one trivial touchpoint.", "The node duplicates another explorer's scope."],
      "confidence": "medium",
      "correction_target": "workflow_skeleton.nodes[explore-context]",
      "correction_action": "Merge, remove, split, reorder, or change this node role."
    }
  }
}
```

Rules:

- `selected_for` must explain why the node exists in the final graph.
- `role_principle` must justify the role instead of only naming it.
- `dependency_principle` must justify `depends_on`, including empty dependencies.
- `parallelism_principle` must explain whether the node is parallel, serial, a fan-in, or a gate.
- `evidence_principle` must explain why `expected_outputs` are enough for downstream use.

## node_instantiations instantiation_principles

Every node instantiation must include `instantiation_principles`:

```json
{
  "skeleton_node_id": "explore-context",
  "node_id": "phasef-explore-context",
  "phase": "exploration",
  "executor_kind": "model_agent",
  "model_provider": "deterministic|openai_compatible|local_openai_compatible",
  "model": "provider model id, or null when selected at runtime",
  "model_config": {
    "output_file": "agent_output.json",
    "output_json": true,
    "tooling_profile": "swe_dataset_testing_v1",
    "tools": [
      {
        "tool_id": "filesystem.read_text",
        "tool_type": "filesystem|git|python|dataset|other",
        "description": "what this node may use it for",
        "mcp_server_id": "egtc.filesystem",
        "permissions": ["read"],
        "requires_network": false,
        "input_contract": {},
        "output_contract": {}
      }
    ],
    "mcp_servers": [
      {
        "server_id": "egtc.filesystem",
        "name": "EGTC workspace filesystem MCP",
        "transport": "runtime_builtin|stdio|http",
        "scope": "workspace|dataset|external",
        "tools": ["filesystem.read_text"],
        "permission_boundary": "repo-policy and sandbox grounding for this server",
        "requires_network": false
      }
    ],
    "allowed_mcp_tools": ["filesystem.read_text"],
    "tool_env": {
      "optional": ["MODELSCOPE_CACHE", "HF_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
      "required_python_packages": ["modelscope", "datasets", "pyarrow", "httpx[socks]"]
    },
    "dataset_access": {
      "primary": {"dataset": "SWE-bench", "namespace": "AI-ModelScope", "streaming": true}
    }
  },
  "prompt": "Worker instruction...",
  "required_evidence": ["analysis_log", "touchpoint_map"],
  "acceptance_criteria": ["Submit only read-only findings."],
  "experience_pattern_ids": ["seed-topology-parallel-explore-implement-verify"],
  "instantiation_principles": {
    "stage_id": "stage-1",
    "skeleton_node_id": "explore-context",
    "executor_principle": "model_agent is selected because this node must be performed by a provider-backed agent that can be served by Codex, an OpenAI-compatible endpoint, or another model provider.",
    "prompt_principle": "The prompt confines ownership to source touchpoint discovery and forbids writes.",
    "permission_principle": "Read-only repo access is enough; no write or network permission is grounded.",
    "evidence_principle": "analysis_log and touchpoint_map are the artifacts needed by the writer and overlooker.",
    "handoff_principle": "The writer consumes touchpoint_map after all exploration nodes complete.",
    "decision_basis": {
      "basis_id": "basis-instantiation-explore-context",
      "source_refs": ["workflow_skeleton.nodes[explore-context]", "repo_policy.allowed_read_paths"],
      "matched_signals": ["read-only agent work", "artifact handoff required"],
      "assumptions": ["The repo can be inspected locally without network."],
      "invalidation_signals": ["The node requires unavailable tools.", "The prompt scope overlaps another node."],
      "confidence": "medium",
      "correction_target": "node_instantiations[phasef-explore-context]",
      "correction_action": "Change executor, prompt, evidence contract, handoff, or permission grounding."
    }
  },
  "permission_grounding": {
    "network": "none",
    "allowed_read_paths": ["."],
    "allowed_write_paths": [],
    "allowed_commands": [],
    "grounded_by": ["repo_policy.allowed_read_paths"],
    "justification": "Read-only exploration."
  }
}
```

Rules:

- `executor_principle` must justify why the node is an agent, subprocess, verifier, overlooker, or other executor.
- Prefer `executor_kind=model_agent` for model-backed agents. Use `codex_cli` only for explicit Codex compatibility tests and `subprocess` only for deterministic local commands.
- For `model_agent`, include `model_provider`; include `model` when a concrete model is selected; use `model_config.output_file` and `model_config.output_json` when the node must write a structured artifact.
- For `model_agent`, use `model_config.tools`, `model_config.mcp_servers`, `allowed_mcp_tools`, `tool_env`, and `dataset_access` to assign tool/MCP capabilities selected from `available_tooling_profiles`. The Director must compare tool fit, permission preconditions, and scale implications before deciding how many agents receive each capability.
- Dataset tools that require network must be marked with `requires_network=true`; they are not executable under `network:none` until permission grounding and overlooker review approve the escalation.
- `prompt_principle` must justify scope, ownership boundary, and non-overlap with peer nodes.
- `permission_principle` must connect permissions to repo policy and the node goal.
- `evidence_principle` must justify `required_evidence` and acceptance criteria.
- `handoff_principle` must name how downstream nodes consume the output or how final acceptance uses it.
