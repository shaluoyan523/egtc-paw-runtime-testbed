from __future__ import annotations

import re
import uuid
import json
import hashlib
import shutil
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStore
from .agent_wrapper import AgentExecWrapper
from .director_output_factory import build_deterministic_model_director_output
from .experience import ExperienceLibrary, ExperienceMatch
from .identity import IdentityService
from .models import NodeCapsule, to_plain_dict
from .phaseb_models import (
    PermissionGroundingReport,
    NodeInstantiation,
    SandboxProfile,
    TaskDiagnosis,
    WorkflowBlueprint,
    WorkflowSkeleton,
    WorkflowSkeletonNode,
)
from .repo_policy import RepoPolicy
from .compiler import PermissionGrounder
from .tool_registry import merge_model_config_tooling, swe_dataset_tooling_profile


class DirectorAgentV1:
    """Deterministic Director Agent v1 scaffold.

    The Director emits structured planning objects in three stages:
    TaskDiagnosis -> WorkflowSkeleton -> NodeInstantiation.
    """

    director_id = "director-agent-v1"

    def __init__(self, experience_library: ExperienceLibrary | None = None) -> None:
        self.experience_library = experience_library

    def diagnose(self, objective: str, repo_policy: RepoPolicy) -> TaskDiagnosis:
        lower = objective.lower()
        requires_code_change = self._objective_has_any(
            lower,
            [
                "implement",
                "design",
                "phase b",
                "phaseb",
                "fix",
                "bug",
                "patch",
                "add",
                "change",
                "modify",
                "refactor",
                "build",
                "swe",
                "workbench",
            ],
        ) or any(word in objective for word in ["实现", "设计", "新增", "修改", "落地", "中控", "代码修复"])
        requires_tests = requires_code_change or any(
            word in lower for word in ["test", "verify", "validate", "校验", "验证", "测试"]
        )
        task_kind = "director_planning" if self._objective_has_any(lower, ["director"]) or "中控" in objective else (
            "implementation" if requires_code_change else "analysis"
        )
        risk_level = "medium" if requires_code_change else "low"
        touchpoints = self._guess_touchpoints(objective, repo_policy)
        unknowns = []
        if not touchpoints:
            unknowns.append("No concrete repo path was named by the task.")
        matches = (
            self.experience_library.retrieve(objective, limit=6)
            if self.experience_library
            else []
        )
        task_profile = self._build_task_profile(
            objective,
            requires_code_change=requires_code_change,
            requires_tests=requires_tests,
            repo_policy=repo_policy,
        )
        return TaskDiagnosis(
            task_id=f"task-{uuid.uuid4().hex[:10]}",
            objective=objective,
            task_kind=str(task_profile.get("primary_task_family") or task_kind),
            risk_level=str(task_profile.get("risk_level") or risk_level),
            repo_touchpoints=touchpoints,
            requires_code_change=requires_code_change,
            requires_tests=requires_tests,
            task_profile=task_profile,
            unknowns=unknowns,
            experience_matches=self._serialize_matches(matches),
        )

    def select_skeleton(self, diagnosis: TaskDiagnosis) -> WorkflowSkeleton:
        topology_pattern_ids = self._matched_pattern_ids(diagnosis, "topology")
        review_pattern_ids = self._matched_pattern_ids(diagnosis, "review_loop")
        handoff_pattern_ids = self._matched_pattern_ids(diagnosis, "handoff")
        failure_pattern_ids = self._matched_pattern_ids(diagnosis, "failure_policy")
        matched_pattern_ids = self._matched_pattern_ids(diagnosis)
        if diagnosis.requires_code_change and topology_pattern_ids:
            return self._experience_guided_skeleton(
                diagnosis,
                topology_pattern_ids,
                review_pattern_ids,
                handoff_pattern_ids,
                failure_pattern_ids,
                matched_pattern_ids,
            )

        nodes = [
            WorkflowSkeletonNode(
                node_id="diagnose",
                phase="diagnosis",
                role="worker",
                goal="Inspect task context and identify implementation surface.",
                expected_outputs=["analysis_log"],
                experience_pattern_ids=matched_pattern_ids,
                node_selection_principles=self._default_node_selection_principles(
                    stage_id="diagnosis",
                    node_id="diagnose",
                    role="worker",
                    reason="A read-only diagnosis node is needed before writes when the implementation surface is not yet grounded.",
                    source_refs=["objective", "repo_policy"],
                ),
            )
        ]
        edges: list[tuple[str, str]] = []
        if diagnosis.requires_code_change:
            nodes.append(
                WorkflowSkeletonNode(
                    node_id="implement",
                    phase="implementation",
                    role="worker",
                    goal="Make the minimal code change required by the objective.",
                    depends_on=["diagnose"],
                    expected_outputs=["diff", "worker_log"],
                    experience_pattern_ids=matched_pattern_ids,
                    node_selection_principles=self._default_node_selection_principles(
                        stage_id="implementation",
                        node_id="implement",
                        role="worker",
                        reason="A bounded worker node is needed because the diagnosis requires a code change.",
                        source_refs=["task_diagnosis.requires_code_change"],
                    ),
                )
            )
            edges.append(("diagnose", "implement"))
        if diagnosis.requires_tests:
            nodes.append(
                WorkflowSkeletonNode(
                    node_id="verify",
                    phase="verification",
                    role="worker",
                    goal="Run repo-grounded checks and report test evidence.",
                    depends_on=["implement"] if diagnosis.requires_code_change else ["diagnose"],
                    expected_outputs=["test_report", "validator_ready_evidence"],
                    experience_pattern_ids=review_pattern_ids or matched_pattern_ids,
                    node_selection_principles=self._default_node_selection_principles(
                        stage_id="verification",
                        node_id="verify",
                        role="worker",
                        reason="A read-only verification node is needed because the task requires test evidence.",
                        source_refs=["task_diagnosis.requires_tests", "repo_policy.test_commands"],
                    ),
                )
            )
            edges.append(("implement" if diagnosis.requires_code_change else "diagnose", "verify"))
        return WorkflowSkeleton(
            skeleton_id=f"skeleton-{uuid.uuid4().hex[:10]}",
            topology="linear",
            nodes=nodes,
            edges=edges,
            rationale="Director v1 uses a conservative linear workflow for Phase B.",
            experience_pattern_ids=matched_pattern_ids,
            experience_rationale=[
                "No active topology pattern was strong enough to change the conservative linear skeleton."
            ] if matched_pattern_ids else [],
        )

    def instantiate_nodes(
        self,
        diagnosis: TaskDiagnosis,
        skeleton: WorkflowSkeleton,
        repo_policy: RepoPolicy,
    ) -> list[NodeInstantiation]:
        grounder = PermissionGrounder(repo_policy)
        instantiations: list[NodeInstantiation] = []
        for skeleton_node in skeleton.nodes:
            command = self._command_for(skeleton_node.phase, repo_policy)
            node = NodeCapsule(
                node_id=f"{diagnosis.task_id}-{skeleton_node.node_id}",
                phase=skeleton_node.phase,
                goal=skeleton_node.goal,
                command=command,
                acceptance_criteria=[
                    "Worker may only submit results.",
                    "Evidence must include log, diff, and test artifacts when required.",
                    "Overlooker acceptance must cite evidence_ref.",
                ],
                experience_pattern_ids=skeleton_node.experience_pattern_ids,
                executor_kind="subprocess",
            )
            grounding = grounder.derive(node, skeleton_node.phase)
            node.sandbox_profile = grounding.sandbox_profile.__dict__
            instantiations.append(
                NodeInstantiation(
                    node=node,
                    skeleton_node_id=skeleton_node.node_id,
                    permission_grounding=grounding,
                    instantiation_principles=self._default_instantiation_principles(
                        skeleton_node=skeleton_node,
                        node=node,
                        grounding=grounding,
                    ),
                )
            )
        return instantiations

    def plan(self, objective: str, repo_policy: RepoPolicy) -> WorkflowBlueprint:
        diagnosis = self.diagnose(objective, repo_policy)
        skeleton = self.select_skeleton(diagnosis)
        instantiations = self.instantiate_nodes(diagnosis, skeleton, repo_policy)
        return WorkflowBlueprint(
            blueprint_id=f"blueprint-{uuid.uuid4().hex[:10]}",
            director_id=self.director_id,
            task_diagnosis=diagnosis,
            repo_policy=repo_policy,
            workflow_skeleton=skeleton,
            node_instantiations=instantiations,
            task_profile=diagnosis.task_profile,
            work_assignment_plan=self._work_assignment_plan_from_skeleton(skeleton),
            permission_plan=self._permission_plan_from_instantiations(instantiations),
            experience_pattern_ids=skeleton.experience_pattern_ids,
        )

    def plan_with_codex_director(
        self,
        objective: str,
        repo_policy: RepoPolicy,
        workspace: Path,
        *,
        codex_binary: str | None = None,
        timeout_sec: int = 240,
    ) -> WorkflowBlueprint:
        """Launch a real Codex Director session for Stage F experience selection."""

        if self.experience_library is None:
            raise ValueError("plan_with_codex_director requires an ExperienceLibrary")
        workspace.mkdir(parents=True, exist_ok=True)
        seed_matches = self.experience_library.retrieve(objective, limit=16)
        skill_packet = self._materialize_director_deliberative_planning_skill(workspace)
        tooling_profile = swe_dataset_tooling_profile()
        input_packet = {
            "objective": objective,
            "repo_policy": to_plain_dict(repo_policy),
            "director_skill": skill_packet,
            "experience_candidates": [
                {
                    "pattern_id": match.pattern.pattern_id,
                    "pattern_type": match.pattern.pattern_type,
                    "description": match.pattern.description,
                    "score": match.score,
                    "matched_signals": match.matched_signals,
                    "recommended_structure": match.pattern.recommended_structure,
                    "required_evidence": match.pattern.required_evidence,
                    "risk_notes": match.pattern.risk_notes[:2],
                    "evidence_level": match.pattern.evidence_level,
                    "confidence_score": match.pattern.confidence_score,
                    "source_refs": match.pattern.source_refs[:3],
                }
                for match in seed_matches
            ],
            "available_tooling_profiles": [tooling_profile],
            "director_rules": [
                "Director must choose how many agents/nodes are needed.",
                "Director must compare multiple candidate workflow skeletons before selecting one.",
                "Director must read the skill files named by director_input.director_skill before emitting the final workflow.",
                "Director must first decompose the task into a linear requirement flow.",
                "Director must choose the structure and agent allocation for each linear stage before creating final nodes.",
                "Director must decide whether each specialized or uncertain stage needs research, and must mark blocked external research when network is unavailable.",
                "Director must feed the draft plan back into itself for structural review before final output.",
                "Director must define a scaling policy for tasks that exceed the current corpus.",
                "Director must treat population/BT/evolution patterns as generic adaptive scaling primitives, not a dedicated executor.",
                "Director must record observations needed to learn whether to scale up, scale down, or route to research.",
                "Director must compare available tooling profiles before assigning tools or MCP servers to model-agent nodes.",
                "Director must emit task_profile, work_assignment_plan, and permission_plan as first-class planning objects.",
                "Director must estimate agents, tokens, wall time, success probability, budget gate, stop condition, escalation condition, and cheaper alternatives before allocating agents.",
                "Director must cite selected experience pattern ids.",
                "Director must not request network or sandbox/permission expansion.",
                "Director must keep verification read-only.",
                "Director must include director_skill_usage with skill path and sha256 values copied from director_input.director_skill.",
                "Director structured output is compiled before execution.",
            ],
        }
        (workspace / "director_input.json").write_text(
            json.dumps(input_packet, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        identity = IdentityService()
        actor = identity.actor("director-phasef", "director")
        token = identity.issue_token(actor, ["artifact:read", "artifact:write"])
        artifacts = ArtifactStore(workspace / "artifacts", identity)
        wrapper = AgentExecWrapper(artifacts, actor, token)
        director_node = NodeCapsule(
            node_id="phasef-director-agent",
            phase="Phase F Director",
            goal="Select and apply experience patterns, including agent allocation.",
            command=[],
            acceptance_criteria=[
                "Director must write strict JSON to director_output.json.",
                "Director chooses topology and number of worker agents.",
                "Director cites experience pattern ids used for the workflow.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="codex_cli",
            prompt=self._phase_f_director_prompt_short(),
            codex_binary=codex_binary,
            sandbox_profile={
                "backend": "codex_native",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": timeout_sec,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 64,
                    "max_command_count": 1,
                },
            },
        )
        director_result = wrapper.run(
            director_node,
            workspace,
            role="director",
        )
        if director_result.exit_code != 0:
            raise RuntimeError(
                f"Codex Director session failed with exit_code={director_result.exit_code}"
            )
        output = self._read_director_output(workspace / "director_output.json")
        if not output:
            raise RuntimeError("Codex Director did not create a valid director_output.json")
        self._validate_director_skill_usage(output, skill_packet)
        blueprint = self._blueprint_from_codex_director_output(
            output,
            objective,
            repo_policy,
            seed_matches,
            director_result.worker_id,
        )
        blueprint.director_mode = "codex"
        blueprint.director_session_id = director_result.worker_id
        return blueprint

    def plan_with_model_director(
        self,
        objective: str,
        repo_policy: RepoPolicy,
        workspace: Path,
        *,
        model_provider: str = "deterministic",
        model: str | None = None,
        timeout_sec: int = 240,
        model_config: dict[str, Any] | None = None,
    ) -> WorkflowBlueprint:
        """Launch a provider-backed Director Agent session without binding to Codex CLI."""

        if self.experience_library is None:
            raise ValueError("plan_with_model_director requires an ExperienceLibrary")
        workspace.mkdir(parents=True, exist_ok=True)
        seed_matches = self.experience_library.retrieve(objective, limit=16)
        skill_packet = self._materialize_director_deliberative_planning_skill(workspace)
        tooling_profile = swe_dataset_tooling_profile()
        input_packet = {
            "objective": objective,
            "repo_policy": to_plain_dict(repo_policy),
            "director_skill": skill_packet,
            "experience_candidates": [
                {
                    "pattern_id": match.pattern.pattern_id,
                    "pattern_type": match.pattern.pattern_type,
                    "description": match.pattern.description,
                    "score": match.score,
                    "matched_signals": match.matched_signals,
                    "recommended_structure": match.pattern.recommended_structure,
                    "required_evidence": match.pattern.required_evidence,
                    "risk_notes": match.pattern.risk_notes[:2],
                    "evidence_level": match.pattern.evidence_level,
                    "confidence_score": match.pattern.confidence_score,
                    "source_refs": match.pattern.source_refs[:3],
                }
                for match in seed_matches
            ],
            "available_executor_kinds": [
                "model_agent",
                "subprocess",
                "codex_cli",
            ],
            "available_tooling_profiles": [tooling_profile],
            "preferred_agent_executor_kind": "model_agent",
            "model_agent": {
                "provider": model_provider,
                "model": model,
                "provider_contract": (
                    "Use executor_kind=model_agent for LLM-backed agents. "
                    "Set model_provider/model/model_config on each NodeCapsule when a concrete provider is selected."
                ),
                "tooling_contract": (
                    "Select tools and MCP servers from available_tooling_profiles only after comparing task needs, "
                    "permission preconditions, and dataset access requirements. Decide agent count and tool allocation "
                    "through Director deliberation rather than fixed heuristics."
                ),
            },
            "director_rules": [
                "Director must choose how many agents/nodes are needed.",
                "Director must compare multiple candidate workflow skeletons before selecting one.",
                "Director must read the skill files named by director_input.director_skill before emitting the final workflow.",
                "Director must feed the draft plan back into itself for structural review before final output.",
                "Director must cite selected experience pattern ids.",
                "Director must treat population/BT/evolution patterns as generic adaptive scaling primitives, not a dedicated executor.",
                "Director must record observations needed to learn whether to scale up, scale down, or route to research.",
                "Director must not assume Codex CLI is the only agent runtime.",
                "Director must prefer executor_kind=model_agent for model-backed agent units.",
                "Director must emit task_profile, work_assignment_plan, and permission_plan as first-class planning objects.",
                "Director must estimate agents, tokens, wall time, success probability, budget gate, stop condition, escalation condition, and cheaper alternatives before allocating agents.",
                "Director must not request network or sandbox/permission expansion.",
                "Verification nodes must be read-only.",
                "Director structured output is compiled before execution.",
            ],
        }
        (workspace / "director_input.json").write_text(
            json.dumps(input_packet, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        identity = IdentityService()
        actor = identity.actor("director-model-agent", "director")
        token = identity.issue_token(actor, ["artifact:read", "artifact:write"])
        artifacts = ArtifactStore(workspace / "artifacts", identity)
        wrapper = AgentExecWrapper(artifacts, actor, token)
        config = merge_model_config_tooling(
            {
                "input_files": [
                    "director_input.json",
                    skill_packet["skill_path"],
                    skill_packet["schema_path"],
                ],
                "output_file": "director_output.json",
                "output_json": True,
                "max_input_file_bytes": 240_000,
                **(model_config or {}),
            }
        )
        if model_provider == "deterministic" and "deterministic_response_json" not in config:
            config["deterministic_response_json"] = build_deterministic_model_director_output(
                objective=objective,
                repo_policy=repo_policy,
                seed_matches=seed_matches,
                skill_packet=skill_packet,
                executor_kind="model_agent",
                model_provider=model_provider,
                model=model,
            )
        director_node = NodeCapsule(
            node_id="phaseh-model-director-agent",
            phase="Phase H Model Director",
            goal="Select and apply experience patterns using provider-backed model-agent units.",
            command=[],
            acceptance_criteria=[
                "Director must write strict JSON to director_output.json.",
                "Director chooses topology and number of worker agents.",
                "Director uses model_agent for provider-backed agent units.",
                "Director cites experience pattern ids used for the workflow.",
            ],
            required_evidence=["log", "sandbox_events", "resource_report"],
            executor_kind="model_agent",
            prompt=self._model_director_prompt(),
            model_provider=model_provider,
            model=model,
            model_config=config,
            sandbox_profile={
                "backend": "model_agent",
                "sandbox_mode": "workspace_write",
                "network": "none",
                "allowed_read_paths": ["."],
                "allowed_write_paths": ["."],
                "resource_limits": {
                    "wall_time_sec": timeout_sec,
                    "memory_mb": 1024,
                    "disk_mb": 512,
                    "max_processes": 1,
                    "max_command_count": 0,
                },
            },
        )
        director_result = wrapper.run(
            director_node,
            workspace,
            role="director",
        )
        if director_result.exit_code != 0:
            raise RuntimeError(
                f"Model Director session failed with exit_code={director_result.exit_code}"
            )
        output = self._read_director_output(workspace / "director_output.json")
        if not output:
            raise RuntimeError("Model Director did not create a valid director_output.json")
        self._validate_director_skill_usage(output, skill_packet)
        blueprint = self._blueprint_from_codex_director_output(
            output,
            objective,
            repo_policy,
            seed_matches,
            director_result.worker_id,
        )
        blueprint.director_mode = "model_agent"
        blueprint.director_session_id = director_result.worker_id
        return blueprint

    def _phase_f_director_prompt_short(self) -> str:
        return """
You are the EGTC-PAW Phase F Director Agent.

Read ./director_input.json, then read these local skill files before planning:
- ./skills/director-deliberative-planning/SKILL.md
- ./skills/director-deliberative-planning/references/planning_schema.md

Create ./director_output.json as strict JSON only. Do not write markdown.

Planning order:
1. Diagnose the task and retrieve applicable experience patterns from director_input.experience_candidates.
2. Build a linear requirement flow.
3. Choose stage structures, research route decisions, and per-stage agent allocation.
4. Compare at least three complete skeleton candidates: small, selected, and larger-scalable.
5. Draft final nodes, edges, and node instantiations.
6. Feed that draft plan back into yourself for structural review. Review it as if another Director created it.
7. Apply necessary corrections, then emit the final workflow.

director_output.json must contain exactly these top-level objects:
- director_skill_usage
- task_profile
- task_diagnosis
- workflow_skeleton
- node_instantiations
- work_assignment_plan
- permission_plan

Use the schema in planning_schema.md for exact field shapes. Required workflow_skeleton fields:
- topology
- agent_allocation
- alternative_skeletons
- scaling_policy
- execution_estimate
- deliberation_trace
- linear_requirement_flow
- stage_structure_decisions
- research_route_decisions
- per_stage_agent_allocation
- plan_derivation_trace
- draft_plan_review
- experience_pattern_ids
- experience_rationale
- nodes
- edges

draft_plan_review requirements:
- reviewed_draft_fields must include linear_requirement_flow, stage_structure_decisions, research_route_decisions, per_stage_agent_allocation, nodes, edges, node_instantiations, and experience_pattern_ids.
- structural_verdict must be pass, revise_before_final, or needs_human_review.
- structure_findings must be non-empty; use an info finding if the draft is already structurally sound.
- every structure finding must include decision_basis.
- recommended_changes and applied_changes must be non-empty.
- rejected_changes must be a list, even when empty.
- final_structure_summary must explain why the final graph is structurally sound after review.

director_skill_usage must copy hashes exactly from director_input.director_skill and include applied_required_fields containing:
task_profile, work_assignment_plan, permission_plan, linear_requirement_flow, stage_structure_decisions, research_route_decisions, per_stage_agent_allocation, scaling_policy, plan_derivation_trace, node_selection_principles, instantiation_principles, draft_plan_review, decision_basis.

Rules:
- Use only pattern ids present in director_input.experience_candidates.
- Do not assume a fixed number of agents; derive counts from complexity, uncertainty, dependency breadth, validation burden, risk, and evidence.
- The current task may need 1 agent, 4 agents, dozens of agents, or a staged plan that can grow toward hundreds; include scale triggers.
- For complex verifiable tasks, choose an adaptive scale level rather than a special-case executor; population, pairwise ranking, mutation, and large BT-style runs are generic primitives.
- scaling_policy must include policy_id/current_scale_level/scale_level_name/scale_down_triggers/observations_to_record/budget_gate/decision_basis when applicable.
- task_diagnosis.task_profile and top-level task_profile must classify task family, verification, knowledge sources, failure modes, and budget.
- workflow_skeleton.execution_estimate must include estimated_agents, estimated_tokens, estimated_wall_time_sec, expected_success_probability, budget_gate, stop_condition, escalation_condition, and cheaper_alternative.
- work_assignment_plan must cover every final node with schemas, ownership, parallel safety, failure takeover, selection basis, and capability needs.
- permission_plan must cover every node instantiation with permission_intents, minimum_boundary, why_needed, fallback_if_denied, and secret_access=false.
- observations_to_record must include candidate_count, comparison_count, validator_pass_rate, retry_count, replan_count, and next_scaling_hint.
- Every planning record, node_selection_principles object, instantiation_principles object, and draft_plan_review structure finding must include decision_basis.
- The sum of per_stage_agent_allocation.agent_count values must equal agent_allocation.total_agents and the final skeleton node count.
- Every final node id must appear in plan_derivation_trace.
- Node instantiations should normally use executor_kind="model_agent" for model-backed agents; use codex_cli only for explicit Codex compatibility.
- Do not request network access.
- Do not write sensitive paths.
- Verification nodes must be read-only.
- Do not clone repositories. Do not run tests.
""".strip()

    def _phase_f_director_prompt(self) -> str:
        return self._phase_f_director_prompt_short()

    def _model_director_prompt(self) -> str:
        return """
You are the EGTC-PAW Director Agent running as a provider-backed model agent.

Read ./director_input.json, then read these local skill files before planning:
- ./skills/director-deliberative-planning/SKILL.md
- ./skills/director-deliberative-planning/references/planning_schema.md

Create ./director_output.json as strict JSON only. Do not write markdown.

Planning order:
1. Diagnose the task and retrieve applicable experience patterns from director_input.experience_candidates.
2. Build a linear requirement flow.
3. Choose stage structures, research route decisions, and per-stage agent allocation.
4. Compare at least three complete skeleton candidates: small, selected, and larger-scalable.
5. Draft final nodes, edges, and node instantiations.
6. Feed that draft plan back into yourself for structural review. Review it as if another Director created it.
7. Apply necessary corrections, then emit the final workflow.

Rules:
- Use only pattern ids present in director_input.experience_candidates.
- Do not assume a fixed number of agents; derive counts from complexity, uncertainty, dependency breadth, validation burden, risk, and evidence.
- Do not assume Codex CLI is the only agent runtime.
- Prefer executor_kind="model_agent" for model-backed agents and include model_provider/model/model_config when concrete provider information is known.
- For complex verifiable tasks, choose an adaptive scale level rather than a special-case executor; population, pairwise ranking, mutation, and large BT-style runs are generic primitives.
- scaling_policy must include policy_id/current_scale_level/scale_level_name/scale_down_triggers/observations_to_record/budget_gate/decision_basis when applicable.
- observations_to_record must include candidate_count, comparison_count, validator_pass_rate, retry_count, replan_count, and next_scaling_hint.
- Use subprocess only for deterministic local commands, and codex_cli only when the plan explicitly needs Codex compatibility.
- Every planning record, node_selection_principles object, instantiation_principles object, and draft_plan_review structure finding must include decision_basis.
- The sum of per_stage_agent_allocation.agent_count values must equal agent_allocation.total_agents and the final skeleton node count.
- Every final node id must appear in plan_derivation_trace.
- Verification nodes must be read-only.
- Do not request network access, clone repositories, write sensitive paths, or run tests.

director_output.json must contain exactly these top-level objects:
- director_skill_usage
- task_profile
- task_diagnosis
- workflow_skeleton
- node_instantiations
- work_assignment_plan
- permission_plan

Use the schema in planning_schema.md for exact field shapes. Required workflow_skeleton fields:
- topology
- agent_allocation
- alternative_skeletons
- scaling_policy
- execution_estimate
- deliberation_trace
- linear_requirement_flow
- stage_structure_decisions
- research_route_decisions
- per_stage_agent_allocation
- plan_derivation_trace
- draft_plan_review
- experience_pattern_ids
- experience_rationale
- nodes
- edges
""".strip()

    def _phase_f_director_prompt_legacy(self) -> str:
        return """
You are the EGTC-PAW Phase F Director Agent.

Read ./director_input.json and create ./director_output.json.

You must read the Director skill files before planning:
- ./skills/director-deliberative-planning/SKILL.md
- ./skills/director-deliberative-planning/references/planning_schema.md

Use the files named by director_input.director_skill. Then produce deliberative
planning artifacts and derive the final workflow. Do not jump directly from
objective to final nodes.

You must choose and apply experience-library patterns yourself. This includes:
- decomposing the objective into a linear requirement flow,
- selecting a structure for each linear stage,
- deciding whether local or external research is needed for each specialized stage,
- selecting which experience pattern ids to use,
- choosing topology,
- choosing how many worker agents/nodes to instantiate,
- assigning roles to each node,
- assigning experience_pattern_ids to the skeleton and each node.
- comparing multiple candidate workflow skeletons before committing.
- defining how the workflow should scale if the task needs tens or hundreds of agents.
- feeding the draft workflow back into yourself for structural review before finalizing, then applying necessary corrections.

Output strict JSON:
{
  "director_skill_usage": {
    "skill_name": "director-deliberative-planning",
    "skill_path": "skills/director-deliberative-planning/SKILL.md",
    "schema_path": "skills/director-deliberative-planning/references/planning_schema.md",
    "skill_sha256": "copy from director_input.director_skill.skill_sha256",
    "schema_sha256": "copy from director_input.director_skill.schema_sha256",
    "loaded": true,
    "applied_required_fields": [
      "task_profile",
      "work_assignment_plan",
      "permission_plan",
      "linear_requirement_flow",
      "stage_structure_decisions",
      "research_route_decisions",
      "per_stage_agent_allocation",
      "scaling_policy",
      "plan_derivation_trace",
      "node_selection_principles",
      "instantiation_principles",
      "draft_plan_review",
      "decision_basis"
    ]
  },
  "task_diagnosis": {
    "task_kind": "implementation" | "analysis" | "director_planning",
    "risk_level": "low" | "medium" | "high",
    "requires_code_change": true | false,
    "requires_tests": true | false,
    "task_profile": {
      "task_family": "retrieval | finance_calculation | planning | code_repair | terminal_execution | contest_reasoning | implementation | analysis",
      "verification_method": "answer_match | unit_tests | container_tests | external_fact_evidence | human_judged | judge",
      "knowledge_sources": ["prompt", "repo", "local_dataset", "network", "tool_execution"],
      "predicted_failure_modes": ["ambiguity", "missing_dependency", "permission_gap", "long_chain_error", "arithmetic_error", "environment_error"],
      "estimated_difficulty": "low | medium | high | extreme",
      "estimated_agents": 1,
      "estimated_tokens": 4000,
      "estimated_wall_time_sec": 300,
      "multi_candidate_worthwhile": false,
      "budget_gate": {"max_agents": 1, "max_tokens": 4000, "max_wall_time_sec": 300},
      "stop_condition": "what evidence stops execution",
      "escalation_condition": "what evidence requires Director replan or human/permission review",
      "cheaper_alternative": "single cheap path when sufficient"
    },
    "repo_touchpoints": ["."],
    "unknowns": [],
    "experience_matches": [
      {
        "pattern_id": "...",
        "pattern_type": "...",
        "score": 0,
        "matched_signals": ["..."],
        "description": "...",
        "evidence_level": "...",
        "confidence_score": 0,
        "source_refs": ["..."]
      }
    ]
  },
  "workflow_skeleton": {
    "topology": "director_selected_topology_name",
    "agent_allocation": {
      "total_agents": 0,
      "roles": {"role_name": 0},
      "allocation_rationale": ["why this number of agents is enough for the current task"],
      "agent_count_confidence": "low" | "medium" | "high"
    },
    "alternative_skeletons": [
      {
        "name": "candidate topology name",
        "estimated_agents": 0,
        "strengths": ["..."],
        "weaknesses": ["..."],
        "selected": false,
        "rejection_reason": "why this was not chosen, or empty when selected"
      }
    ],
    "scaling_policy": {
      "policy_id": "experience pattern id or local policy id",
      "current_scale_level": 0,
      "requested_scale_level": 0,
      "scale_level_name": "single_candidate_baseline | small_pool_2_to_3_candidates_all_pairs | medium_pool_5_to_8_candidates_pairwise_K2_or_K3 | evolution_loop_with_elites_and_mutation | large_population_BT_style_n12_to_n20_K4_T2_to_T3_M8_to_M10",
      "scale_triggers": ["signals that require more agents/nodes"],
      "scale_down_triggers": ["signals that require fewer agents or research before more population"],
      "max_planned_agents_for_current_task": 0,
      "expansion_strategy": ["how to add more explorers/workers/verifiers/overlookers if complexity grows"],
      "requires_replan_when": ["conditions that force Director replan"],
      "observations_to_record": ["candidate_count", "comparison_count", "validator_pass_rate", "retry_count", "replan_count", "next_scaling_hint"],
      "budget_gate": {"max_candidate_count": 0, "max_comparison_count": 0, "max_mutation_rounds": 0, "max_planned_agents": 0},
      "decision_basis": {
        "basis_id": "basis-scaling-policy",
        "source_refs": ["objective", "experience:pattern-id"],
        "matched_signals": ["complexity, verifiability, uncertainty, budget"],
        "assumptions": ["why this scale level is enough now"],
        "invalidation_signals": ["what evidence requires scale up, scale down, or research routing"],
        "confidence": "low | medium | high",
        "correction_target": "workflow_skeleton.scaling_policy",
        "correction_action": "scale up, scale down, add comparison/mutation, or route to research/specialist before recompiling"
      }
    },
    "deliberation_trace": [
      "compare evidence and task signals, then explain a planning judgment",
      "explain why selected topology is better than alternatives"
    ],
    "linear_requirement_flow": [
      {
        "stage_id": "stage-1",
        "order": 1,
        "name": "linear stage name",
        "purpose": "why this stage exists",
        "inputs": ["objective"],
        "outputs": ["stage artifact"],
        "risk_level": "low | medium | high",
        "acceptance_evidence": ["evidence_ref"],
        "decision_basis": {
          "basis_id": "basis-stage-1",
          "source_refs": ["objective", "repo_policy", "experience:pattern-id"],
          "matched_signals": ["task signal"],
          "assumptions": ["assumption that may later be disproven"],
          "invalidation_signals": ["evidence that would make this decision wrong"],
          "confidence": "low | medium | high",
          "correction_target": "linear_requirement_flow[stage-1]",
          "correction_action": "how to revise this decision during dynamic replanning"
        }
      }
    ],
    "stage_structure_decisions": [
      {
        "stage_id": "stage-1",
        "candidate_structures": [
          {"structure": "single_agent", "fit": "low | medium | high", "reason": "..."}
        ],
        "selected_structure": "single_agent | parallel_exploration | specialist_pool | proposer_aggregator | graph_message_passing | dynamic_routing | hierarchical_subteams | review_gate | tool_planning | research_route",
        "selection_reason": "why this structure fits this stage",
        "anti_signals": ["what would make this structure wrong"],
        "experience_pattern_ids": ["..."],
        "decision_basis": {
          "basis_id": "basis-structure-stage-1",
          "source_refs": ["linear_requirement_flow[stage-1]", "experience:pattern-id"],
          "matched_signals": ["parallel work signal"],
          "assumptions": ["why this structure should work"],
          "invalidation_signals": ["when this structure should be replaced"],
          "confidence": "low | medium | high",
          "correction_target": "stage_structure_decisions[stage-1]",
          "correction_action": "switch structure, split stage, or request replan"
        }
      }
    ],
    "research_route_decisions": [
      {
        "stage_id": "stage-1",
        "research_needed": false,
        "reason": "why research is or is not needed",
        "available_sources": ["experience_candidates", "repo_files"],
        "blocked_sources": ["external_web"],
        "planned_queries_or_searches": ["local search or query plan"],
        "adopted_expert_route": "route selected from experience or local evidence",
        "fallback_if_research_blocked": "fallback plan",
        "decision_basis": {
          "basis_id": "basis-research-stage-1",
          "source_refs": ["objective", "experience_candidates", "network:none"],
          "matched_signals": ["specialist route signal or no-research signal"],
          "assumptions": ["what local evidence is expected to cover"],
          "invalidation_signals": ["what proves research was insufficient"],
          "confidence": "low | medium | high",
          "correction_target": "research_route_decisions[stage-1]",
          "correction_action": "add research node, mark blocked source, or request permission escalation"
        }
      }
    ],
    "per_stage_agent_allocation": [
      {
        "stage_id": "stage-1",
        "agent_count": 1,
        "count_reason": "why this many agents are needed for this stage",
        "decision_basis": {
          "basis_id": "basis-allocation-stage-1",
          "source_refs": ["stage_structure_decisions[stage-1]"],
          "matched_signals": ["width, uncertainty, independence, validation burden, risk"],
          "assumptions": ["why this count is enough"],
          "invalidation_signals": ["what proves more/fewer agents are needed"],
          "confidence": "low | medium | high",
          "correction_target": "per_stage_agent_allocation[stage-1]",
          "correction_action": "add, remove, split, or merge agents and recompile graph"
        },
        "agents": [
          {
            "role": "explorer",
            "task": "agent task",
            "inputs": ["stage input"],
            "outputs": ["stage output"],
            "ownership_boundary": "what this agent owns",
            "write_authority": "none | bounded write path",
            "handoff_target": "next stage or node",
            "decision_basis": {
              "basis_id": "basis-agent-stage-1-explorer",
              "source_refs": ["per_stage_agent_allocation[stage-1]"],
              "matched_signals": ["why this role is needed"],
              "assumptions": ["what this role can resolve"],
              "invalidation_signals": ["what makes this role redundant or insufficient"],
              "confidence": "low | medium | high",
              "correction_target": "node:final-node-id",
              "correction_action": "replace, remove, split, or add handoff constraints"
            }
          }
        ]
      }
    ],
    "plan_derivation_trace": [
      "basis-structure-stage-1: stage-1 selected parallel_exploration, producing final nodes explore-a and explore-b"
    ],
    "draft_plan_review": {
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
      "structural_verdict": "pass | revise_before_final | needs_human_review",
      "structure_findings": [
        {
          "finding_id": "draft-finding-1",
          "severity": "info | warning | error",
          "target": "workflow_skeleton.nodes[final-node-id]",
          "finding": "what the Director noticed after feeding the draft plan back to itself",
          "recommendation": "what should change or why no change is needed",
          "decision_basis": {
            "basis_id": "basis-draft-review-1",
            "source_refs": ["draft_plan.nodes", "draft_plan.edges", "experience:pattern-id"],
            "matched_signals": ["structural issue or sufficiency signal"],
            "assumptions": ["what must hold for the final structure"],
            "invalidation_signals": ["what would prove this review wrong"],
            "confidence": "low | medium | high",
            "correction_target": "workflow_skeleton.nodes[final-node-id]",
            "correction_action": "add, remove, split, merge, reorder, or leave the node unchanged"
          }
        }
      ],
      "missing_capabilities": ["capabilities missing from the draft, or none"],
      "recommended_changes": [
        {
          "change_id": "draft-change-1",
          "change_type": "add_node | remove_node | split_node | merge_nodes | reorder_edge | change_role | change_evidence | change_agent_count | no_change",
          "target": "workflow_skeleton.nodes",
          "rationale": "why the draft needs this change, or why no change is needed"
        }
      ],
      "applied_changes": [
        {
          "change_id": "draft-change-1",
          "applied": true,
          "final_targets": ["workflow_skeleton.nodes[final-node-id]"],
          "result": "how the final workflow reflects this review"
        }
      ],
      "rejected_changes": [
        {
          "change_id": "draft-change-2",
          "reason": "why a considered change was not applied"
        }
      ],
      "final_structure_summary": "why the final graph is structurally sound after review"
    },
    "experience_pattern_ids": ["..."],
    "experience_rationale": ["..."],
    "nodes": [
      {
        "node_id": "explore-context",
        "phase": "exploration",
        "role": "explorer",
        "goal": "...",
        "depends_on": [],
        "expected_outputs": ["analysis_log"],
        "experience_pattern_ids": ["..."],
        "node_selection_principles": {
          "stage_id": "stage-1",
          "selected_for": ["why this node exists in the final graph"],
          "role_principle": "why this role is assigned instead of another role",
          "dependency_principle": "why depends_on is empty or names its predecessors",
          "parallelism_principle": "why this node is parallel, serial, or a join point",
          "evidence_principle": "why the expected_outputs are sufficient",
          "experience_pattern_ids": ["..."],
          "decision_basis": {
            "basis_id": "basis-node-explore-context",
            "source_refs": ["per_stage_agent_allocation[stage-1]", "experience:pattern-id"],
            "matched_signals": ["why this final node is needed"],
            "assumptions": ["what must be true for this node to remain useful"],
            "invalidation_signals": ["what would make this node redundant, too broad, or wrongly ordered"],
            "confidence": "low | medium | high",
            "correction_target": "workflow_skeleton.nodes[explore-context]",
            "correction_action": "remove, merge, split, reorder, or change the node role"
          }
        }
      }
    ],
    "edges": [["explore-context", "implement"]]
  },
  "node_instantiations": [
    {
      "skeleton_node_id": "explore-context",
      "node_id": "phasef-explore-context",
      "phase": "exploration",
      "goal": "...",
      "executor_kind": "model_agent",
      "command": [],
      "model_provider": "deterministic | openai_compatible | local_openai_compatible",
      "model": "provider model id or null",
      "model_config": {"output_file": "agent_output.json", "output_json": true},
      "prompt": "Worker-specific instruction for this node.",
      "required_evidence": ["diff", "test", "log"],
      "acceptance_criteria": ["Worker may only submit results.", "Overlooker acceptance must cite evidence_ref."],
      "experience_pattern_ids": ["..."],
      "instantiation_principles": {
        "stage_id": "stage-1",
        "skeleton_node_id": "explore-context",
        "executor_principle": "why this must be a model_agent, codex_cli compatibility agent, subprocess, or other executor",
        "prompt_principle": "why the prompt scope and ownership boundary are sufficient",
        "permission_principle": "why read/write/network permissions are minimal and grounded",
        "evidence_principle": "why required_evidence and acceptance_criteria fit this node",
        "handoff_principle": "how this node's output is consumed by downstream nodes",
        "decision_basis": {
          "basis_id": "basis-instantiation-explore-context",
          "source_refs": ["workflow_skeleton.nodes[explore-context]", "repo_policy"],
          "matched_signals": ["why this execution form is needed"],
          "assumptions": ["what must be true for this executor and permission profile"],
          "invalidation_signals": ["what would require changing executor, permissions, evidence, or prompt"],
          "confidence": "low | medium | high",
          "correction_target": "node_instantiations[phasef-explore-context]",
          "correction_action": "change executor, prompt, evidence contract, or permission grounding"
        }
      },
      "permission_grounding": {
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": [],
        "allowed_commands": [["python3", "-c", "print('Director-selected node submitted')"]],
        "grounded_by": ["repo_policy.allowed_read_paths", "repo_policy.allowed_write_paths", "repo_policy.test_commands", "repo_policy.network_allowed_by_default"],
        "justification": "Read-only exploration."
      }
    }
  ]
}

Rules:
- Use only pattern ids present in director_input.experience_candidates.
- Read ./skills/director-deliberative-planning/SKILL.md and ./skills/director-deliberative-planning/references/planning_schema.md before creating the final plan.
- director_skill_usage.skill_sha256 and director_skill_usage.schema_sha256 must exactly match director_input.director_skill hashes.
- If the skill files cannot be read, do not invent a plan; write director_skill_usage.loaded=false and explain the missing file in task_diagnosis.unknowns.
- Do not assume a fixed number of agents. Derive total_agents from task complexity, uncertainty, dependency breadth, validation surface, risk, and available evidence.
- The current task may need 1 agent, 4 agents, dozens of agents, or a staged plan that can grow toward hundreds. If the full scale is not needed now, explain the scale triggers.
- For complex verifiable tasks, choose an adaptive scale level rather than a special-case executor. Treat population sampling, pairwise ranking, mutation, and large BT-style runs as generic primitives that can be expanded by evidence.
- scaling_policy must include enough observations for self-learning: candidate_count, comparison_count, validator_pass_rate, retry_count, replan_count, token/latency when available, and next_scaling_hint.
- Compare at least three candidate skeletons, including a small conservative plan, a medium plan, and a larger scalable plan.
- Pick the smallest plan that has enough coverage, but explicitly describe when it should be expanded.
- Every final node must be traceable to a linear_requirement_flow stage through per_stage_agent_allocation and plan_derivation_trace.
- Before finalizing nodes, create a draft plan object internally, review it as if another Director created it, and emit workflow_skeleton.draft_plan_review.
- draft_plan_review must cover planning fields, final nodes, edges, node instantiations, and selected experience patterns.
- draft_plan_review must include structure_findings, recommended_changes, applied_changes, rejected_changes, and final_structure_summary.
- Every planning record must include decision_basis with source_refs, matched_signals, assumptions, invalidation_signals, confidence, correction_target, and correction_action.
- Every workflow_skeleton.nodes item must include node_selection_principles explaining why this node, role, dependency position, expected outputs, and parallel/serial placement were selected.
- Every node_instantiations item must include instantiation_principles explaining why this executor, prompt scope, evidence contract, handoff, and permission grounding were selected.
- The sum of per_stage_agent_allocation.agent_count values must equal agent_allocation.total_agents and the final skeleton node count.
- Each stage_structure_decisions item must include anti_signals.
- Each stage must have a research_route_decisions item. If external research would help but network is none, mark external_web as blocked and plan local research only.
- Node instantiations should normally use executor_kind="model_agent" for model-backed agents; use codex_cli only for explicit Codex compatibility.
- Do not request network access.
- Do not write sensitive paths.
- Verification nodes must be read-only.
- No markdown. Do not clone repositories. Do not run tests.
""".strip()

    def _read_director_output(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _blueprint_from_codex_director_output(
        self,
        output: dict[str, Any],
        objective: str,
        repo_policy: RepoPolicy,
        seed_matches: list[ExperienceMatch],
        director_session_id: str,
    ) -> WorkflowBlueprint:
        if not output:
            raise ValueError("Codex Director output is empty")
        raw_diagnosis = output.get("task_diagnosis") if isinstance(output.get("task_diagnosis"), dict) else {}
        raw_skeleton = output.get("workflow_skeleton") if isinstance(output.get("workflow_skeleton"), dict) else {}
        raw_nodes = raw_skeleton.get("nodes") if isinstance(raw_skeleton.get("nodes"), list) else []
        raw_instantiations = output.get("node_instantiations") if isinstance(output.get("node_instantiations"), list) else []
        selected_pattern_ids = self._active_director_pattern_ids(output, seed_matches)
        if not raw_diagnosis:
            raise ValueError("Codex Director output is missing task_diagnosis")
        if not raw_skeleton:
            raise ValueError("Codex Director output is missing workflow_skeleton")
        if not raw_nodes:
            raise ValueError("Codex Director output has no skeleton nodes")
        if not raw_instantiations:
            raise ValueError("Codex Director output has no node instantiations")
        diagnosis = TaskDiagnosis(
            task_id=f"task-{uuid.uuid4().hex[:10]}",
            objective=objective,
            task_kind=str(raw_diagnosis.get("task_kind") or "implementation"),
            risk_level=str(raw_diagnosis.get("risk_level") or "medium"),
            repo_touchpoints=[
                str(path) for path in raw_diagnosis.get("repo_touchpoints", ["."])
            ],
            requires_code_change=bool(raw_diagnosis.get("requires_code_change", True)),
            requires_tests=bool(raw_diagnosis.get("requires_tests", True)),
            task_profile=(
                raw_diagnosis.get("task_profile")
                if isinstance(raw_diagnosis.get("task_profile"), dict)
                else (
                    output.get("task_profile")
                    if isinstance(output.get("task_profile"), dict)
                    else self._build_task_profile(
                        objective,
                        requires_code_change=bool(raw_diagnosis.get("requires_code_change", True)),
                        requires_tests=bool(raw_diagnosis.get("requires_tests", True)),
                        repo_policy=repo_policy,
                    )
                )
            ),
            unknowns=[str(item) for item in raw_diagnosis.get("unknowns", [])],
            experience_matches=(
                raw_diagnosis.get("experience_matches")
                if isinstance(raw_diagnosis.get("experience_matches"), list)
                else self._serialize_matches(seed_matches)
            ),
        )
        skeleton_nodes: list[WorkflowSkeletonNode] = []
        for raw in raw_nodes:
            if not isinstance(raw, dict):
                continue
            skeleton_nodes.append(
                WorkflowSkeletonNode(
                    node_id=str(raw.get("node_id") or f"node-{len(skeleton_nodes)+1}"),
                    phase=str(raw.get("phase") or "analysis"),
                    role=str(raw.get("role") or "worker"),
                    goal=str(raw.get("goal") or "Director-selected node."),
                    depends_on=[str(item) for item in raw.get("depends_on", [])],
                    expected_outputs=[str(item) for item in raw.get("expected_outputs", [])],
                    experience_pattern_ids=self._filter_known_patterns(
                        raw.get("experience_pattern_ids", selected_pattern_ids),
                        selected_pattern_ids,
                    ),
                    node_selection_principles=(
                        raw.get("node_selection_principles")
                        if isinstance(raw.get("node_selection_principles"), dict)
                        else {}
                    ),
                )
            )
        if not skeleton_nodes:
            raise ValueError("Codex Director output yielded no valid skeleton nodes")
        edges = []
        for edge in raw_skeleton.get("edges", []):
            if isinstance(edge, list | tuple) and len(edge) == 2:
                edges.append((str(edge[0]), str(edge[1])))
        skeleton = WorkflowSkeleton(
            skeleton_id=f"skeleton-{uuid.uuid4().hex[:10]}",
            topology=str(raw_skeleton.get("topology") or "director_selected"),
            nodes=skeleton_nodes,
            edges=edges,
            rationale="Codex Director selected topology and agent allocation from experience candidates.",
            agent_allocation=(
                raw_skeleton.get("agent_allocation")
                if isinstance(raw_skeleton.get("agent_allocation"), dict)
                else {"total_agents": len(skeleton_nodes)}
            ),
            alternative_skeletons=(
                raw_skeleton.get("alternative_skeletons")
                if isinstance(raw_skeleton.get("alternative_skeletons"), list)
                else []
            ),
            scaling_policy=(
                raw_skeleton.get("scaling_policy")
                if isinstance(raw_skeleton.get("scaling_policy"), dict)
                else {}
            ),
            execution_estimate=(
                raw_skeleton.get("execution_estimate")
                if isinstance(raw_skeleton.get("execution_estimate"), dict)
                else {}
            ),
            deliberation_trace=[
                str(item) for item in raw_skeleton.get("deliberation_trace", [])
            ],
            linear_requirement_flow=(
                raw_skeleton.get("linear_requirement_flow")
                if isinstance(raw_skeleton.get("linear_requirement_flow"), list)
                else []
            ),
            stage_structure_decisions=(
                raw_skeleton.get("stage_structure_decisions")
                if isinstance(raw_skeleton.get("stage_structure_decisions"), list)
                else []
            ),
            research_route_decisions=(
                raw_skeleton.get("research_route_decisions")
                if isinstance(raw_skeleton.get("research_route_decisions"), list)
                else []
            ),
            per_stage_agent_allocation=(
                raw_skeleton.get("per_stage_agent_allocation")
                if isinstance(raw_skeleton.get("per_stage_agent_allocation"), list)
                else []
            ),
            plan_derivation_trace=[
                str(item) for item in raw_skeleton.get("plan_derivation_trace", [])
            ],
            draft_plan_review=(
                raw_skeleton.get("draft_plan_review")
                if isinstance(raw_skeleton.get("draft_plan_review"), dict)
                else {}
            ),
            experience_pattern_ids=self._filter_known_patterns(
                raw_skeleton.get("experience_pattern_ids", selected_pattern_ids),
                selected_pattern_ids,
            ),
            experience_rationale=[
                str(item) for item in raw_skeleton.get("experience_rationale", [])
            ],
        )
        instantiations: list[NodeInstantiation] = []
        for raw in raw_instantiations:
            if not isinstance(raw, dict):
                continue
            skeleton_node_id = str(raw.get("skeleton_node_id") or "")
            if not skeleton_node_id:
                continue
            command = raw.get("command")
            command_list = [str(item) for item in command] if isinstance(command, list) else self._command_for(str(raw.get("phase") or ""), repo_policy)
            pattern_ids = self._filter_known_patterns(
                raw.get("experience_pattern_ids", skeleton.experience_pattern_ids),
                selected_pattern_ids,
            )
            node = NodeCapsule(
                node_id=str(raw.get("node_id") or f"{diagnosis.task_id}-{skeleton_node_id}"),
                phase=str(raw.get("phase") or "analysis"),
                goal=str(raw.get("goal") or "Director-selected node."),
                command=command_list,
                acceptance_criteria=[
                    str(item) for item in raw.get("acceptance_criteria", [])
                ] or [
                    "Worker may only submit results.",
                    "Overlooker acceptance must cite evidence_ref.",
                ],
                required_evidence=[
                    str(item) for item in raw.get("required_evidence", ["diff", "test", "log"])
                ],
                experience_pattern_ids=pattern_ids,
                executor_kind=str(raw.get("executor_kind") or "subprocess"),
                prompt=str(raw.get("prompt") or raw.get("goal") or "Submit evidence for this Director-selected node."),
                model_provider=(
                    str(raw.get("model_provider"))
                    if raw.get("model_provider") is not None
                    else None
                ),
                model=(
                    str(raw.get("model"))
                    if raw.get("model") is not None
                    else None
                ),
                model_config=(
                    raw.get("model_config")
                    if isinstance(raw.get("model_config"), dict)
                    else {}
                ),
            )
            grounding = self._grounding_from_director(raw, node, repo_policy)
            node.sandbox_profile = grounding.sandbox_profile.__dict__
            instantiations.append(
                NodeInstantiation(
                    node=node,
                    skeleton_node_id=skeleton_node_id,
                    permission_grounding=grounding,
                    instantiation_principles=(
                        raw.get("instantiation_principles")
                        if isinstance(raw.get("instantiation_principles"), dict)
                        else {}
                    ),
                )
            )
        if not instantiations:
            raise ValueError("Codex Director output yielded no valid node instantiations")
        return WorkflowBlueprint(
            blueprint_id=f"blueprint-{uuid.uuid4().hex[:10]}",
            director_id=self.director_id,
            task_diagnosis=diagnosis,
            repo_policy=repo_policy,
            workflow_skeleton=skeleton,
            node_instantiations=instantiations,
            task_profile=(
                output.get("task_profile")
                if isinstance(output.get("task_profile"), dict)
                else diagnosis.task_profile
            ),
            work_assignment_plan=(
                output.get("work_assignment_plan")
                if isinstance(output.get("work_assignment_plan"), list)
                else self._work_assignment_plan_from_skeleton(skeleton)
            ),
            permission_plan=(
                output.get("permission_plan")
                if isinstance(output.get("permission_plan"), list)
                else self._permission_plan_from_instantiations(instantiations)
            ),
            experience_pattern_ids=skeleton.experience_pattern_ids,
            director_mode="codex",
            director_session_id=director_session_id,
            director_skill_usage=(
                output.get("director_skill_usage")
                if isinstance(output.get("director_skill_usage"), dict)
                else {}
            ),
        )

    def _grounding_from_director(
        self,
        raw: dict[str, Any],
        node: NodeCapsule,
        repo_policy: RepoPolicy,
    ) -> PermissionGroundingReport:
        raw_grounding = raw.get("permission_grounding")
        if not isinstance(raw_grounding, dict):
            return PermissionGrounder(repo_policy).derive(node, node.phase)
        allowed_commands = raw_grounding.get("allowed_commands")
        return PermissionGroundingReport(
            node_id=node.node_id,
            sandbox_profile=SandboxProfile(
                network=str(raw_grounding.get("network") or "none"),
                allowed_read_paths=[
                    str(item) for item in raw_grounding.get("allowed_read_paths", ["."])
                ],
                allowed_write_paths=[
                    str(item) for item in raw_grounding.get("allowed_write_paths", [])
                ],
                allowed_commands=(
                    [
                        [str(part) for part in command]
                        for command in allowed_commands
                        if isinstance(command, list)
                    ]
                    if isinstance(allowed_commands, list)
                    else []
                ),
                justification=str(raw_grounding.get("justification") or "Director-provided grounding."),
            ),
            grounded_by=[
                str(item) for item in raw_grounding.get("grounded_by", [])
            ],
        )

    def _director_deliberative_planning_skill(self) -> dict[str, str]:
        root = Path(__file__).resolve().parents[1]
        skill_root = root / "skills" / "director-deliberative-planning"
        skill_path = skill_root / "SKILL.md"
        schema_path = skill_root / "references" / "planning_schema.md"
        return {
            "name": "director-deliberative-planning",
            "skill_path": str(skill_path),
            "schema_path": str(schema_path),
            "instructions": skill_path.read_text(encoding="utf-8") if skill_path.exists() else "",
            "planning_schema": schema_path.read_text(encoding="utf-8") if schema_path.exists() else "",
            "skill_sha256": self._sha256(skill_path),
            "schema_sha256": self._sha256(schema_path),
        }

    def _materialize_director_deliberative_planning_skill(self, workspace: Path) -> dict[str, str]:
        source_packet = self._director_deliberative_planning_skill()
        source_root = Path(source_packet["skill_path"]).parent
        target_root = workspace / "skills" / "director-deliberative-planning"
        if target_root.exists():
            shutil.rmtree(target_root)
        shutil.copytree(source_root, target_root)
        skill_path = target_root / "SKILL.md"
        schema_path = target_root / "references" / "planning_schema.md"
        return {
            "name": "director-deliberative-planning",
            "skill_path": "skills/director-deliberative-planning/SKILL.md",
            "schema_path": "skills/director-deliberative-planning/references/planning_schema.md",
            "skill_sha256": self._sha256(skill_path),
            "schema_sha256": self._sha256(schema_path),
        }

    def _validate_director_skill_usage(
        self,
        output: dict[str, Any],
        skill_packet: dict[str, str],
    ) -> None:
        usage = output.get("director_skill_usage")
        if not isinstance(usage, dict):
            raise ValueError("Codex Director output is missing director_skill_usage")
        expected = {
            "skill_name": skill_packet["name"],
            "skill_path": skill_packet["skill_path"],
            "schema_path": skill_packet["schema_path"],
            "skill_sha256": skill_packet["skill_sha256"],
            "schema_sha256": skill_packet["schema_sha256"],
        }
        for key, value in expected.items():
            if usage.get(key) != value:
                raise ValueError(
                    f"Codex Director skill usage mismatch for {key}: "
                    f"expected {value!r}, got {usage.get(key)!r}"
                )
        if usage.get("loaded") is not True:
            raise ValueError("Codex Director did not mark director skill as loaded")

    def _sha256(self, path: Path) -> str:
        if not path.exists():
            return ""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _default_node_selection_principles(
        self,
        *,
        stage_id: str,
        node_id: str,
        role: str,
        reason: str,
        source_refs: list[str],
    ) -> dict[str, Any]:
        return {
            "stage_id": stage_id,
            "selected_for": [reason],
            "role_principle": f"{role} is the smallest role that satisfies this node goal.",
            "dependency_principle": "Dependencies follow the staged handoff needed before this node can run.",
            "parallelism_principle": "Parallel or serial placement follows dependency and write-ownership constraints.",
            "evidence_principle": "Expected outputs are the evidence needed by downstream nodes or review.",
            "experience_pattern_ids": [
                ref.removeprefix("experience:")
                for ref in source_refs
                if ref.startswith("experience:")
            ],
            "decision_basis": {
                "basis_id": f"basis-node-{node_id}",
                "source_refs": source_refs,
                "matched_signals": [reason],
                "assumptions": ["This node remains necessary for the selected workflow structure."],
                "invalidation_signals": [
                    "The node duplicates another node's ownership boundary.",
                    "The node cannot produce evidence consumed by downstream stages.",
                ],
                "confidence": "medium",
                "correction_target": f"workflow_skeleton.nodes[{node_id}]",
                "correction_action": "Remove, merge, split, reorder, or change this node role during replanning.",
            },
        }

    def _default_instantiation_principles(
        self,
        *,
        skeleton_node: WorkflowSkeletonNode,
        node: NodeCapsule,
        grounding: PermissionGroundingReport,
    ) -> dict[str, Any]:
        return {
            "stage_id": skeleton_node.node_selection_principles.get("stage_id", skeleton_node.phase),
            "skeleton_node_id": skeleton_node.node_id,
            "executor_principle": f"{node.executor_kind} is selected for the node execution surface.",
            "prompt_principle": "Prompt scope follows the skeleton node goal and ownership boundary.",
            "permission_principle": grounding.sandbox_profile.justification,
            "evidence_principle": "Required evidence and acceptance criteria match downstream review needs.",
            "handoff_principle": "Outputs are handed to declared dependent nodes or final review.",
            "decision_basis": {
                "basis_id": f"basis-instantiation-{node.node_id}",
                "source_refs": [
                    f"workflow_skeleton.nodes[{skeleton_node.node_id}]",
                    "repo_policy",
                    "permission_grounding",
                ],
                "matched_signals": [
                    f"{skeleton_node.role} node requires executable task capsule.",
                ],
                "assumptions": [
                    "The selected executor and permission profile can produce the required evidence.",
                ],
                "invalidation_signals": [
                    "The node needs permissions not grounded by repo policy.",
                    "The executor cannot produce the required evidence.",
                ],
                "confidence": "medium",
                "correction_target": f"node_instantiations[{node.node_id}]",
                "correction_action": "Change executor, prompt, evidence contract, or permission grounding.",
            },
        }

    def _active_director_pattern_ids(
        self,
        output: dict[str, Any],
        seed_matches: list[ExperienceMatch],
    ) -> list[str]:
        known = {match.pattern.pattern_id for match in seed_matches}
        selected: list[str] = []
        skeleton = output.get("workflow_skeleton")
        raw_ids = skeleton.get("experience_pattern_ids", []) if isinstance(skeleton, dict) else []
        for item in raw_ids:
            pattern_id = str(item)
            if pattern_id in known and pattern_id not in selected:
                selected.append(pattern_id)
        if selected:
            return selected
        return [match.pattern.pattern_id for match in seed_matches]

    def _filter_known_patterns(self, raw: Any, known_ids: list[str]) -> list[str]:
        values = raw if isinstance(raw, list) else known_ids
        selected: list[str] = []
        known = set(known_ids)
        for item in values:
            pattern_id = str(item)
            if pattern_id in known and pattern_id not in selected:
                selected.append(pattern_id)
        return selected

    def _experience_guided_skeleton(
        self,
        diagnosis: TaskDiagnosis,
        topology_pattern_ids: list[str],
        review_pattern_ids: list[str],
        handoff_pattern_ids: list[str],
        failure_pattern_ids: list[str],
        matched_pattern_ids: list[str],
    ) -> WorkflowSkeleton:
        explorer_patterns = topology_pattern_ids + handoff_pattern_ids
        writer_patterns = topology_pattern_ids + handoff_pattern_ids
        verifier_patterns = review_pattern_ids + failure_pattern_ids + handoff_pattern_ids
        nodes = [
            WorkflowSkeletonNode(
                node_id="explore-context",
                phase="exploration",
                role="explorer",
                goal="Inspect the repository surface and summarize likely implementation touchpoints.",
                expected_outputs=["analysis_log", "touchpoint_map"],
                experience_pattern_ids=explorer_patterns,
                node_selection_principles=self._default_node_selection_principles(
                    stage_id="stage-1",
                    node_id="explore-context",
                    role="explorer",
                    reason="A source explorer separates read-only touchpoint discovery from later writes.",
                    source_refs=["experience:seed-topology-parallel-explore-implement-verify"],
                ),
            ),
            WorkflowSkeletonNode(
                node_id="explore-tests",
                phase="exploration",
                role="explorer",
                goal="Inspect available tests and validation commands without writing files.",
                expected_outputs=["test_plan", "risk_notes"],
                experience_pattern_ids=explorer_patterns,
                node_selection_principles=self._default_node_selection_principles(
                    stage_id="stage-1",
                    node_id="explore-tests",
                    role="explorer",
                    reason="A validation explorer independently maps test and risk evidence before implementation.",
                    source_refs=["repo_policy.test_commands", "experience:seed-review-verification-aware-planning"],
                ),
            ),
            WorkflowSkeletonNode(
                node_id="implement",
                phase="implementation",
                role="worker",
                goal="Apply the minimal code change after read-only exploration has completed.",
                depends_on=["explore-context", "explore-tests"],
                expected_outputs=["diff", "worker_log"],
                experience_pattern_ids=writer_patterns,
                node_selection_principles=self._default_node_selection_principles(
                    stage_id="stage-2",
                    node_id="implement",
                    role="worker",
                    reason="A single writer serializes writes after parallel exploration to avoid conflicting ownership.",
                    source_refs=["experience:seed-handoff-artifact-chain"],
                ),
            ),
            WorkflowSkeletonNode(
                node_id="verify",
                phase="verification",
                role="worker",
                goal="Run repo-grounded checks and prepare validator-ready evidence.",
                depends_on=["implement"],
                expected_outputs=["test_report", "validator_ready_evidence"],
                experience_pattern_ids=verifier_patterns,
                node_selection_principles=self._default_node_selection_principles(
                    stage_id="stage-3",
                    node_id="verify",
                    role="worker",
                    reason="A verification node turns the worker handoff into validator-ready evidence.",
                    source_refs=["repo_policy.test_commands", "experience:seed-review-verification-aware-planning"],
                ),
            ),
        ]
        return WorkflowSkeleton(
            skeleton_id=f"skeleton-{uuid.uuid4().hex[:10]}",
            topology="parallel_explore_then_single_writer_then_verify",
            nodes=nodes,
            edges=[
                ("explore-context", "implement"),
                ("explore-tests", "implement"),
                ("implement", "verify"),
            ],
            rationale=(
                "Director selected an experience-guided skeleton: parallel read-only "
                "exploration, one writer, then verification."
            ),
            experience_pattern_ids=matched_pattern_ids,
            experience_rationale=[
                f"Matched topology patterns: {', '.join(topology_pattern_ids)}.",
                "Experience use only shapes workflow structure; compiler and permission grounding remain authoritative.",
            ],
        )

    def _guess_touchpoints(self, objective: str, repo_policy: RepoPolicy) -> list[str]:
        mentioned = re.findall(r"[\w./-]+\\.py|[\w./-]+\\.md|[\w./-]+\\.toml", objective)
        return mentioned or ["."]

    def _objective_has_any(self, lower_objective: str, terms: list[str]) -> bool:
        for term in terms:
            escaped = re.escape(term.lower())
            if " " in term or "-" in term:
                if term.lower() in lower_objective:
                    return True
                continue
            if re.search(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", lower_objective):
                return True
        return False

    def _build_task_profile(
        self,
        objective: str,
        *,
        requires_code_change: bool,
        requires_tests: bool,
        repo_policy: RepoPolicy,
    ) -> dict[str, Any]:
        lower = objective.lower()
        families: list[str] = []
        if self._objective_has_any(lower, ["browsecomp", "browse", "source", "web"]) or any(term in objective for term in ["检索", "搜索"]):
            families.append("retrieval")
        if self._objective_has_any(lower, ["finance", "financial", "calculator"]) or any(term in objective for term in ["财务", "收益", "估值"]):
            families.append("finance_calculation")
        if self._objective_has_any(lower, ["plancraft", "planning"]) or any(term in objective for term in ["计划", "状态转移"]):
            families.append("planning_state_transition")
        if self._objective_has_any(lower, ["swe", "workbench", "patch", "bug", "fix"]) or "代码修复" in objective:
            families.append("code_repair")
        if self._objective_has_any(lower, ["terminal-bench", "terminal", "shell", "container"]) or any(term in objective for term in ["终端", "容器"]):
            families.append("terminal_execution")
        if self._objective_has_any(lower, ["opendeepthink", "codeforces", "judge"]) or any(term in objective for term in ["竞赛", "推理", "难题"]):
            families.append("contest_reasoning")
        if requires_code_change and "code_repair" not in families:
            families.append("code_repair")
        if not families:
            families.append("analysis")

        verification = ["human_judgment"]
        if any(family in families for family in ["code_repair"]):
            verification = ["unit_tests", "repo_tests", "patch_review"]
        elif "terminal_execution" in families:
            verification = ["shell_exit_status", "container_test", "checkpoint_artifact"]
        elif "retrieval" in families:
            verification = ["external_fact_evidence", "source_citation"]
        elif "finance_calculation" in families:
            verification = ["formula_check", "answer_match", "source_citation"]
        elif "planning_state_transition" in families:
            verification = ["state_transition_check", "ambiguity_review"]
        elif "contest_reasoning" in families:
            verification = ["judge", "sample_tests", "pairwise_ranking"]
        elif requires_tests:
            verification = ["unit_tests"]

        knowledge_sources = ["prompt"]
        if requires_code_change or any(family in families for family in ["code_repair", "terminal_execution"]):
            knowledge_sources.append("repo")
        if any(family in families for family in ["code_repair", "terminal_execution", "contest_reasoning"]):
            knowledge_sources.append("tool_execution")
        if any(family in families for family in ["retrieval", "finance_calculation"]):
            knowledge_sources.append("network_or_local_corpus")
        if self._objective_has_any(lower, ["dataset", "swe-bench", "modelscope", "benchmark"]):
            knowledge_sources.append("local_dataset")

        failure_modes = ["ambiguity", "long_chain_error"]
        if requires_code_change:
            failure_modes += ["missing_dependency", "patch_risk", "test_environment_error"]
        if "terminal_execution" in families:
            failure_modes += ["permission_insufficient", "environment_error", "checkpoint_drift"]
        if any(family in families for family in ["retrieval", "finance_calculation"]):
            failure_modes += ["external_fact_stale", "source_mismatch"]
        if "finance_calculation" in families:
            failure_modes.append("arithmetic_error")
        if "planning_state_transition" in families:
            failure_modes.append("state_ambiguity")
        if "contest_reasoning" in families:
            failure_modes += ["candidate_quality_low", "judge_noise"]

        base_agents = 1
        if "code_repair" in families:
            base_agents = 4
        elif "terminal_execution" in families:
            base_agents = 3
        elif "contest_reasoning" in families:
            base_agents = 3
        elif any(family in families for family in ["retrieval", "finance_calculation", "planning_state_transition"]):
            base_agents = 2

        high_uncertainty = self._objective_has_any(lower, ["complex", "hard", "difficult"]) or any(term in objective for term in ["复杂", "困难", "难"])
        estimated_agents = base_agents + (2 if high_uncertainty and "contest_reasoning" in families else 0)
        estimated_tokens = 6_000 + estimated_agents * 3_000
        estimated_wall_time = 120 + estimated_agents * 90
        worth_multi_candidate = self._worth_multi_candidate(
            objective,
            families,
            high_uncertainty=high_uncertainty,
        )

        return {
            "primary_task_family": families[0],
            "task_families": families,
            "verification_methods": verification,
            "knowledge_sources": knowledge_sources,
            "predicted_failure_modes": sorted(set(failure_modes)),
            "risk_level": "high" if high_uncertainty or "terminal_execution" in families else ("medium" if requires_code_change else "low"),
            "estimated_difficulty": "high" if high_uncertainty else ("medium" if requires_code_change or requires_tests else "low"),
            "estimated_budget": {
                "estimated_agents": estimated_agents,
                "estimated_tokens": estimated_tokens,
                "estimated_wall_time_sec": estimated_wall_time,
                "worth_multi_candidate": worth_multi_candidate,
            },
            "budget_gate": {
                "max_agents_before_replan": max(estimated_agents, 1),
                "max_tokens_before_replan": estimated_tokens * 2,
                "max_wall_time_sec_before_replan": estimated_wall_time * 2,
            },
            "stop_condition": "Stop when required verification evidence passes or Overlooker rejects with unrecoverable ambiguity.",
            "escalation_condition": "Escalate when required knowledge source or permission is unavailable, verifier reports ambiguity, or retries repeat the same failure.",
            "cheaper_alternative": "Use a single-agent cheap path when all required knowledge is in prompt and verification is direct.",
        }

    def _worth_multi_candidate(
        self,
        objective: str,
        families: list[str],
        *,
        high_uncertainty: bool,
    ) -> bool:
        lower = objective.lower()
        if "contest_reasoning" in families:
            return True
        if "retrieval" in families:
            return self._objective_has_any(
                lower,
                [
                    "conflicting sources",
                    "evidence conflict",
                    "multiple answers",
                    "ambiguous sources",
                    "uncertain answer",
                ],
            )
        if "finance_calculation" in families:
            return self._objective_has_any(
                lower,
                ["scenario", "sensitivity", "multiple methods", "compare methods"],
            )
        if "planning_state_transition" in families:
            return self._objective_has_any(
                lower,
                ["multiple valid plans", "ambiguous transitions", "compare plans"],
            )
        if "terminal_execution" in families:
            return False
        if "code_repair" in families:
            return bool(
                high_uncertainty
                and self._objective_has_any(
                    lower,
                    ["multi-module", "large patch", "multiple packages", "architecture"],
                )
            )
        return False

    def _work_assignment_plan_from_skeleton(
        self,
        skeleton: WorkflowSkeleton,
    ) -> list[dict[str, Any]]:
        assignments: list[dict[str, Any]] = []
        allocation_by_node: dict[str, dict[str, Any]] = {}
        for allocation in skeleton.per_stage_agent_allocation:
            if not isinstance(allocation, dict):
                continue
            for agent in allocation.get("agents", []):
                if not isinstance(agent, dict):
                    continue
                target = str(agent.get("handoff_target") or "")
                if target:
                    allocation_by_node[target] = {
                        "stage_id": allocation.get("stage_id"),
                        "agent_record": agent,
                        "count_reason": allocation.get("count_reason"),
                    }
        for node in skeleton.nodes:
            allocation = allocation_by_node.get(node.node_id, {})
            agent = allocation.get("agent_record", {})
            assignments.append(
                {
                    "node_id": node.node_id,
                    "role": node.role,
                    "stage_id": allocation.get("stage_id")
                    or node.node_selection_principles.get("stage_id", node.phase),
                    "agent_type": self._agent_type_for_role(node.role),
                    "input_schema": {
                        "required": ["objective", "upstream_artifacts", "permission_plan"],
                        "upstream_nodes": node.depends_on,
                    },
                    "output_schema": {
                        "required": node.expected_outputs or ["agent_report"],
                    },
                    "ownership_boundary": agent.get(
                        "ownership_boundary",
                        "Own only the outputs declared by this node.",
                    ),
                    "parallel_safe": not bool(node.depends_on),
                    "parallel_safety_reason": (
                        "No predecessor and no write authority."
                        if not node.depends_on
                        else "Depends on upstream artifacts before execution."
                    ),
                    "failure_takeover": "Overlooker may request retry, fork from accepted upstream state, or Director replan.",
                    "selection_basis": agent.get(
                        "task",
                        f"{node.role} selected for {node.phase}.",
                    ),
                    "capability_needs": self._capability_needs_for_phase(node.phase, node.role),
                }
            )
        return assignments

    def _permission_plan_from_instantiations(
        self,
        instantiations: list[NodeInstantiation],
    ) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        for inst in instantiations:
            profile = inst.permission_grounding.sandbox_profile
            intents = ["read_repo"]
            if profile.allowed_write_paths:
                intents.append("write_patch")
            if profile.allowed_commands:
                intents.append("run_tests" if inst.node.phase == "verification" else "run_shell")
            if profile.network != "none":
                intents.append("network_search")
            plans.append(
                {
                    "node_id": inst.node.node_id,
                    "skeleton_node_id": inst.skeleton_node_id,
                    "permission_intents": intents,
                    "minimum_boundary": {
                        "read_paths": profile.allowed_read_paths,
                        "write_paths": profile.allowed_write_paths,
                        "allowed_commands": profile.allowed_commands,
                        "network": profile.network,
                        "secret_access": False,
                    },
                    "why_needed": profile.justification,
                    "fallback_if_denied": "Return to Overlooker permission review and request Director replan with a lower-permission route.",
                }
            )
        return plans

    def _agent_type_for_role(self, role: str) -> str:
        if role in {"verifier", "reviewer"}:
            return "verification"
        if role in {"explorer", "researcher"}:
            return "tool_or_expert"
        if role in {"aggregator", "judge"}:
            return "candidate_selection"
        if role in {"worker", "implementer"}:
            return "generalist"
        return "specialist"

    def _capability_needs_for_phase(self, phase: str, role: str) -> list[str]:
        text = f"{phase} {role}".lower()
        needs = ["repo_read"]
        if any(term in text for term in ["implement", "worker", "write"]):
            needs.append("write_patch")
        if any(term in text for term in ["verify", "test"]):
            needs.append("test")
        if any(term in text for term in ["terminal", "shell"]):
            needs.append("terminal_shell")
        return needs

    def _command_for(self, phase: str, repo_policy: RepoPolicy) -> list[str]:
        if phase == "verification":
            return repo_policy.test_commands[0]
        return ["python3", "-c", "print('Director Agent v1 node submitted')"]

    def _serialize_matches(self, matches: list[ExperienceMatch]) -> list[dict[str, object]]:
        return [
            {
                "pattern_id": match.pattern.pattern_id,
                "pattern_type": match.pattern.pattern_type,
                "score": match.score,
                "matched_signals": match.matched_signals,
                "description": match.pattern.description,
                "evidence_level": match.pattern.evidence_level,
                "confidence_score": match.pattern.confidence_score,
                "source_refs": match.pattern.source_refs,
            }
            for match in matches
        ]

    def _matched_pattern_ids(
        self,
        diagnosis: TaskDiagnosis,
        pattern_type: str | None = None,
    ) -> list[str]:
        ids: list[str] = []
        for match in diagnosis.experience_matches:
            if pattern_type is not None and match.get("pattern_type") != pattern_type:
                continue
            pattern_id = str(match.get("pattern_id") or "")
            if pattern_id and pattern_id not in ids:
                ids.append(pattern_id)
        return ids
