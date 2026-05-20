---
name: director-deliberative-planning
description: Deliberative workflow planning for Director Agents. Use when a Director must plan a complex task, choose agent counts, assign roles, compare orchestration structures, decide whether to research existing expert routes, or avoid producing an over-direct plan without staged task decomposition.
---

# Director Deliberative Planning

Use this skill before emitting a workflow plan. The Director must first build planning artifacts, then derive the final graph from those artifacts. Do not jump from objective to final nodes.

## Required Workflow

1. Decompose the objective into a linear requirement flow.
   - Write ordered stages that would solve the task if performed by one careful expert.
   - Each stage must include purpose, inputs, outputs, risk, and acceptance evidence.
   - Each stage must include `decision_basis`, so future replans know why this stage exists.
   - Keep this flow linear even if the final workflow will be parallel.

2. Decide the structure for each stage.
   - For each linear stage, choose one of: single_agent, parallel_exploration, specialist_pool, proposer_aggregator, graph_message_passing, dynamic_routing, hierarchical_subteams, review_gate, tool_planning, research_route.
   - Explain why this structure fits the stage.
   - Name anti-signals that would make the structure wrong.
   - Include `decision_basis` for the selected structure and any rejected structure that materially influenced the choice.

3. Decide whether research is needed.
   - Use existing experience candidates first.
   - If the task names a niche domain, unfamiliar framework, specialized algorithm, external standard, or unclear expert route, add a research_route stage.
   - If network is not allowed, the Director must plan a read-only repository/documentation search from available local artifacts and mark external research as blocked, not silently skip it.
   - Include `decision_basis` for each research decision, especially when research is declared unnecessary.

4. Allocate agents after structure selection.
   - Derive counts from stage width, uncertainty, independence of work, validation burden, and risk.
   - Do not use a fixed recipe such as two explorers, one writer, one verifier.
   - For each proposed agent, specify role, task, inputs, outputs, ownership boundary, write authority, and handoff target.
   - Include `decision_basis` for each per-stage allocation and each agent role when that role is not obvious.
   - If many agents may be needed later, keep the current plan minimal but define scale triggers and expansion tiers.

5. Compare complete candidate plans.
   - Compare at least three complete plans: small, selected, and larger-scalable.
   - Each candidate must include stage mapping, estimated agents, strengths, weaknesses, and rejection reason if not selected.

6. Feed the draft plan back into the Director for structural self-review.
   - Before final output, construct a `draft_plan` containing the selected linear stages, structure decisions, agent allocation, draft final nodes, draft edges, draft node instantiations, and selected experience pattern ids.
   - Review the draft as if it came from another Director. Judge whether the structure is scientifically reasonable for the task, whether the node count and agent allocation are sufficient, whether dependencies and handoffs are coherent, whether verification/overlooker coverage is strong enough, and whether any stage needs missing research, synthesis, integration, or retry/replan support.
   - Compare the draft against the rejected alternatives and the scale triggers. Do not merely repeat the original rationale.
   - Emit `draft_plan_review` with findings, missing capabilities, recommended changes, applied changes, rejected changes, and a final structural verdict.
   - Apply required corrections to the final workflow before emitting final nodes. If no changes are needed, say why the existing structure is sufficient.

7. Emit the final workflow only after the above steps.
   - The final `workflow_skeleton.nodes` and `node_instantiations` must be traceable to the per-stage decisions.
   - Every final skeleton node must include `node_selection_principles`, explaining why that node exists, why that role was chosen, why its dependency position is correct, why it is parallel/serial/joined, and why its expected outputs are sufficient.
   - Every node instantiation must include `instantiation_principles`, explaining why the executor, prompt scope, evidence contract, handoff, and permission grounding were selected.
   - `plan_derivation_trace` must cite the decision basis ids used to derive each final node.
   - `draft_plan_review.reviewed_draft_fields` must name the draft fields reviewed, and `draft_plan_review.applied_changes` must be consistent with the final node/edge/instantiation set.
   - Verification and overlooker stages must be read-only unless the repo policy explicitly grounds writes.
   - Experience patterns may shape structure but must not request network, permissions, sandbox changes, secrets, or sensitive writes.
   - Model-backed agents should normally use `executor_kind=model_agent` with explicit `model_provider`, optional `model`, and bounded `model_config`. Use `codex_cli` only when the task explicitly requires Codex compatibility, and use `subprocess` only for deterministic local commands.

## Required Output Fields

Add these fields under `workflow_skeleton` in addition to the runtime-required fields:

- `linear_requirement_flow`: ordered stage records.
- `stage_structure_decisions`: one record per linear stage.
- `research_route_decisions`: explicit research-needed/research-not-needed decisions.
- `per_stage_agent_allocation`: agent counts and role assignments per stage.
- `plan_derivation_trace`: concise trace connecting stage decisions to final nodes.
- `draft_plan_review`: Director self-review of the draft plan after planning and task decomposition, before final workflow emission.

Each `workflow_skeleton.nodes[*]` record must include `node_selection_principles`. Each `node_instantiations[*]` record must include `instantiation_principles`.

Every planning record and every node principle record must include `decision_basis`. The basis must name evidence or source refs, matched signals, assumptions, invalidation signals, and the workflow field or node that should be patched if the basis is later disproven.

See `references/planning_schema.md` for exact field shapes.

## Quality Bar

Reject your own plan and revise before output if:

- A final node cannot be traced to a linear stage.
- A stage has an agent count but no reason for that count.
- A chosen structure has no anti-signals.
- A specialized task has no research decision.
- Any stage, structure decision, research decision, allocation, or final-node derivation lacks `decision_basis`.
- Any final skeleton node lacks `node_selection_principles`.
- Any node instantiation lacks `instantiation_principles`.
- `draft_plan_review` is missing, reviews no draft fields, has no structural findings, or does not state applied/rejected changes.
- A node exists without a clear role, dependency, parallelism, evidence, and correction principle.
- A `decision_basis` has no correction target for future replanning.
- The selected plan is just the first plan considered.
- The plan scales by adding agents without ownership boundaries.
