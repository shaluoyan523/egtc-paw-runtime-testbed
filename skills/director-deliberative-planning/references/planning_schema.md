# Planning Schema

Director output must include these additional `workflow_skeleton` fields.

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
  "executor_kind": "codex_cli",
  "prompt": "Worker instruction...",
  "required_evidence": ["analysis_log", "touchpoint_map"],
  "acceptance_criteria": ["Submit only read-only findings."],
  "experience_pattern_ids": ["seed-topology-parallel-explore-implement-verify"],
  "instantiation_principles": {
    "stage_id": "stage-1",
    "skeleton_node_id": "explore-context",
    "executor_principle": "codex_cli is selected because this node must be performed by an agent that can inspect repo context.",
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
- `prompt_principle` must justify scope, ownership boundary, and non-overlap with peer nodes.
- `permission_principle` must connect permissions to repo policy and the node goal.
- `evidence_principle` must justify `required_evidence` and acceptance criteria.
- `handoff_principle` must name how downstream nodes consume the output or how final acceptance uses it.
