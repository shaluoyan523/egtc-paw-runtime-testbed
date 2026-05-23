from __future__ import annotations

import re
from typing import Any

from .experience import ExperienceMatch
from .models import to_plain_dict
from .phaseb_models import RepoPolicy
from .tool_registry import merge_model_config_tooling


def build_deterministic_model_director_output(
    *,
    objective: str,
    repo_policy: RepoPolicy,
    seed_matches: list[ExperienceMatch],
    skill_packet: dict[str, str],
    executor_kind: str = "model_agent",
    model_provider: str = "deterministic",
    model: str | None = None,
) -> dict[str, Any]:
    selected_pattern_ids = _selected_pattern_ids(seed_matches)
    pattern_refs = [f"experience:{pattern_id}" for pattern_id in selected_pattern_ids]
    scaling_pattern_refs = [
        f"experience:{pattern_id}"
        for pattern_id in selected_pattern_ids
        if pattern_id
        in {
            "seed-scaling-adaptive-population-curriculum",
            "seed-scaling-large-dynamic-hierarchy",
        }
    ] or pattern_refs[:1]

    family = _selected_primary_family(objective)
    template = _workflow_template(
        family=family,
        repo_policy=repo_policy,
        selected_pattern_ids=selected_pattern_ids,
        pattern_refs=pattern_refs,
        scaling_pattern_refs=scaling_pattern_refs,
    )
    node_specs = template["node_specs"]
    nodes = [
        _skeleton_node(
            node_id=spec["node_id"],
            phase=spec["phase"],
            role=spec["role"],
            goal=spec["goal"],
            depends_on=spec.get("depends_on", []),
            expected_outputs=spec["expected_outputs"],
            stage_id=spec["stage_id"],
            reason=spec["reason"],
            refs=spec.get("refs", ["objective", *pattern_refs[:2]]),
            pattern_ids=selected_pattern_ids,
            dependency=spec["dependency"],
            parallelism=spec["parallelism"],
        )
        for spec in node_specs
    ]
    instantiations = [
        _instantiation(
            skeleton_node_id=spec["node_id"],
            node_id=f"model-{spec['node_id']}",
            phase=spec["phase"],
            goal=spec.get("instantiation_goal", spec["goal"]),
            executor_kind=executor_kind,
            model_provider=model_provider,
            model=model,
            pattern_ids=selected_pattern_ids,
            write_paths=spec.get("write_paths", []),
            prompt=spec["prompt"],
            stage_id=spec["stage_id"],
            read_paths=spec.get("read_paths", ["."]),
            allowed_commands=spec.get("allowed_commands", []),
            network=spec.get("network", "none"),
            permission_intents=spec.get("permission_intents"),
            required_evidence=spec.get("required_evidence"),
            acceptance_criteria=spec.get("acceptance_criteria"),
            permission_justification=spec.get("permission_justification"),
        )
        for spec in node_specs
    ]
    task_profile = _task_profile(objective, len(nodes))

    return {
        "director_skill_usage": {
            "skill_name": skill_packet["name"],
            "skill_path": skill_packet["skill_path"],
            "schema_path": skill_packet["schema_path"],
            "skill_sha256": skill_packet["skill_sha256"],
            "schema_sha256": skill_packet["schema_sha256"],
            "loaded": True,
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
                "decision_basis",
            ],
        },
        "task_diagnosis": {
            "task_kind": template["task_kind"],
            "risk_level": task_profile.get("risk_level", "medium"),
            "requires_code_change": template["requires_code_change"],
            "requires_tests": template["requires_tests"],
            "repo_touchpoints": template["repo_touchpoints"],
            "unknowns": template["unknowns"],
            "experience_matches": _serialized_matches(seed_matches[:6]),
            "objective": objective,
            "task_profile": task_profile,
        },
        "workflow_skeleton": {
            "topology": template["topology"],
            "agent_allocation": _agent_allocation(node_specs, template["allocation_rationale"]),
            "alternative_skeletons": _alternative_skeletons(template, node_specs),
            "scaling_policy": _scaling_policy(
                family,
                len(nodes),
                task_profile,
                scaling_pattern_refs,
            ),
            "execution_estimate": _execution_estimate(task_profile, len(nodes)),
            "deliberation_trace": template["deliberation_trace"],
            "linear_requirement_flow": _linear_requirement_flow(template, pattern_refs),
            "stage_structure_decisions": _stage_structure_decisions(template, selected_pattern_ids),
            "research_route_decisions": _research_route_decisions(template),
            "per_stage_agent_allocation": _per_stage_agent_allocation(template),
            "plan_derivation_trace": _plan_derivation_trace(template),
            "draft_plan_review": _draft_plan_review(family, node_specs, selected_pattern_ids),
            "experience_pattern_ids": selected_pattern_ids,
            "experience_rationale": template["experience_rationale"],
            "nodes": nodes,
            "edges": template["edges"],
        },
        "task_profile": task_profile,
        "work_assignment_plan": _work_assignment_plan(nodes, node_specs),
        "permission_plan": _permission_plan(instantiations),
        "node_instantiations": instantiations,
    }


def _selected_pattern_ids(seed_matches: list[ExperienceMatch]) -> list[str]:
    preferred = [
        "seed-topology-parallel-explore-implement-verify",
        "seed-handoff-artifact-chain",
        "seed-review-verification-aware-planning",
        "seed-role-overlooker-review-rework",
        "seed-scaling-adaptive-population-curriculum",
        "seed-scaling-large-dynamic-hierarchy",
    ]
    available = [match.pattern.pattern_id for match in seed_matches]
    selected: list[str] = []
    for pattern_id in preferred + available:
        if pattern_id in available and pattern_id not in selected:
            selected.append(pattern_id)
        if len(selected) >= 6:
            break
    return selected or available[:6]


def _serialized_matches(matches: list[ExperienceMatch]) -> list[dict[str, Any]]:
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


def _has_any(lower_objective: str, terms: list[str]) -> bool:
    for term in terms:
        value = term.lower()
        if " " in value or "-" in value:
            if value in lower_objective:
                return True
            continue
        if re.search(rf"(?<![a-z0-9_]){re.escape(value)}(?![a-z0-9_])", lower_objective):
            return True
    return False


def _task_families_from_objective(objective: str) -> list[str]:
    lower = objective.lower()
    families: list[str] = []
    if _has_any(lower, ["browsecomp", "browse", "retrieval", "search", "source citation", "citation", "web"]) or any(term in objective for term in ["检索", "搜索", "引用", "证据"]):
        families.append("retrieval")
    if _has_any(lower, ["finance", "financial", "portfolio", "portfolio return", "calculator", "valuation"]) or any(term in objective for term in ["财务", "金融", "收益", "估值", "公式"]):
        families.append("finance_calculation")
    if _has_any(lower, ["plancraft", "state transition", "state-transition", "planning state"]) or any(term in objective for term in ["状态转移", "规划任务"]):
        families.append("planning_state_transition")
    if _has_any(lower, ["swe", "swe-bench", "workbench", "patch", "bug", "fix", "code repair"]) or any(term in objective for term in ["代码修复", "补丁", "修 bug"]):
        families.append("code_repair")
    if _has_any(lower, ["terminal-bench", "terminal", "shell", "container", "checkpoint"]) or any(term in objective for term in ["终端", "容器", "检查点"]):
        families.append("terminal_execution")
    if _has_any(lower, ["opendeepthink", "codeforces", "judge", "contest", "olympiad"]) or any(term in objective for term in ["竞赛", "难题", "判题"]):
        families.append("contest_reasoning")
    return families or ["analysis"]


def _selected_primary_family(objective: str) -> str:
    families = _task_families_from_objective(objective)
    if families != ["analysis"]:
        return families[0]
    lower = objective.lower()
    if _has_any(lower, ["implement", "design", "add", "change", "modify", "refactor", "build"]) or any(
        term in objective for term in ["实现", "设计", "新增", "修改", "落地", "中控"]
    ):
        return "code_repair"
    return "analysis"


def _worth_multi_candidate(objective: str, families: list[str]) -> bool:
    lower = objective.lower()
    if "contest_reasoning" in families:
        return True
    if "retrieval" in families:
        return _has_any(
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
        return _has_any(lower, ["scenario", "sensitivity", "multiple methods", "compare methods"])
    if "code_repair" in families:
        return _has_any(lower, ["complex", "hard", "difficult", "multi-module", "large patch"])
    return False


def _task_profile(objective: str, estimated_agents: int) -> dict[str, Any]:
    task_families = _task_families_from_objective(objective)
    primary = _selected_primary_family(objective)
    if task_families == ["analysis"] and primary != "analysis":
        task_families = [primary]
    elif primary not in task_families:
        task_families.append(primary)
    verification_by_family = {
        "retrieval": ["external_fact_evidence", "source_citation"],
        "finance_calculation": ["formula_check", "answer_match", "source_citation"],
        "planning_state_transition": ["state_transition_check", "ambiguity_review"],
        "code_repair": ["unit_tests", "repo_tests", "patch_review"],
        "terminal_execution": ["shell_exit_status", "container_test", "checkpoint_artifact"],
        "contest_reasoning": ["judge", "sample_tests", "pairwise_ranking"],
        "analysis": ["human_judgment"],
    }
    failure_modes = ["ambiguity", "long_chain_error"]
    if "code_repair" in task_families:
        failure_modes += ["missing_dependency", "patch_risk", "test_environment_error"]
    if "terminal_execution" in task_families:
        failure_modes += ["permission_insufficient", "environment_error", "checkpoint_drift"]
    if "retrieval" in task_families:
        failure_modes += ["external_fact_stale", "source_mismatch"]
    if "finance_calculation" in task_families:
        failure_modes += ["arithmetic_error", "formula_mismatch", "source_mismatch"]
    if "planning_state_transition" in task_families:
        failure_modes += ["state_ambiguity", "invalid_transition"]
    if "contest_reasoning" in task_families:
        failure_modes += ["candidate_quality_low", "judge_noise"]

    knowledge_sources = ["prompt", "experience_library"]
    if any(family in task_families for family in ["code_repair", "terminal_execution"]):
        knowledge_sources.append("repo")
    if any(family in task_families for family in ["code_repair", "terminal_execution", "contest_reasoning"]):
        knowledge_sources.append("tool_execution")
    if any(family in task_families for family in ["retrieval", "finance_calculation"]):
        knowledge_sources.append("network_or_local_corpus")
    if _has_any(objective.lower(), ["dataset", "benchmark", "modelscope", "swe-bench", "terminal-bench"]):
        knowledge_sources.append("local_dataset")

    high_uncertainty = _has_any(objective.lower(), ["complex", "hard", "difficult"]) or any(term in objective for term in ["复杂", "困难", "难"])
    estimated_tokens = 5_000 + estimated_agents * 3_000
    estimated_wall_time = 120 + estimated_agents * 90
    return {
        "primary_task_family": primary,
        "task_families": task_families,
        "verification_methods": verification_by_family.get(primary, ["human_judgment"]),
        "knowledge_sources": sorted(set(knowledge_sources)),
        "predicted_failure_modes": sorted(set(failure_modes)),
        "risk_level": "high" if high_uncertainty or primary == "terminal_execution" else ("medium" if primary != "analysis" else "low"),
        "estimated_difficulty": "high" if high_uncertainty else ("medium" if primary != "analysis" else "low"),
        "estimated_budget": {
            "estimated_agents": estimated_agents,
            "estimated_tokens": estimated_tokens,
            "estimated_wall_time_sec": estimated_wall_time,
            "worth_multi_candidate": _worth_multi_candidate(objective, task_families),
        },
        "budget_gate": {
            "max_agents_before_replan": max(estimated_agents, 1),
            "max_tokens_before_replan": estimated_tokens * 2,
            "max_wall_time_sec_before_replan": estimated_wall_time * 2,
        },
        "stop_condition": _stop_condition(primary),
        "escalation_condition": _escalation_condition(primary),
        "cheaper_alternative": _cheaper_alternative(primary),
    }


def _workflow_template(
    *,
    family: str,
    repo_policy: RepoPolicy,
    selected_pattern_ids: list[str],
    pattern_refs: list[str],
    scaling_pattern_refs: list[str],
) -> dict[str, Any]:
    del selected_pattern_ids, scaling_pattern_refs
    if family == "retrieval":
        node_specs = [
            _node_spec(
                "research-sources",
                "research",
                "researcher",
                "Collect local-corpus or approved browser evidence and record source quality.",
                [],
                ["source_evidence_set", "query_log"],
                "stage-1",
                "Evidence collection is the primary uncertainty, not code modification.",
                "No predecessor is needed because this starts from the prompt and available corpus.",
                "Runs alone because downstream synthesis needs one coherent evidence ledger.",
                "Search available local corpus and, only if already permitted, browser sources. Do not edit files.",
                ["read_repo", "dataset_read", "browser"] + (["network_search"] if repo_policy.network_allowed_by_default else []),
                ["source_evidence_set", "query_log", "resource_report"],
                ["objective", "available_tooling_profiles", *pattern_refs[:2]],
                capability_needs=["dataset_read", "browser", "source_evidence"],
                ownership_boundary="Own source ledger and query rationale only.",
                network=("enabled" if repo_policy.network_allowed_by_default else "none"),
            ),
            _node_spec(
                "answer-synthesis",
                "synthesis",
                "synthesizer",
                "Synthesize an answer from collected evidence without inventing unsupported facts.",
                ["research-sources"],
                ["answer_candidate", "citation_map"],
                "stage-2",
                "Retrieval tasks need answer synthesis after evidence is gathered.",
                "Synthesis waits for the source evidence ledger.",
                "Serial join point because a single answer must reconcile all cited evidence.",
                "Use the source ledger to produce an answer candidate and citation map. Do not request repo writes.",
                ["read_repo", "dataset_read"],
                ["answer_candidate", "citation_map", "reasoning_log"],
                ["workflow_skeleton.nodes[research-sources]", *pattern_refs[:2]],
                capability_needs=["answer_synthesis", "citation_mapping"],
                ownership_boundary="Own answer candidate and citation map only.",
            ),
            _node_spec(
                "source-verify",
                "verification",
                "verifier",
                "Verify that the final answer is directly supported by cited evidence.",
                ["answer-synthesis"],
                ["source_verification_report", "final_answer"],
                "stage-3",
                "A source verifier catches stale, mismatched, or unsupported retrieval answers.",
                "Verification depends on the synthesized answer and citation map.",
                "Terminal review gate; read-only and evidence-grounded.",
                "Check every cited claim against the source ledger and produce final answer evidence.",
                ["read_repo", "dataset_read", "browser"] + (["network_search"] if repo_policy.network_allowed_by_default else []),
                ["source_verification_report", "final_answer", "resource_report"],
                ["workflow_skeleton.nodes[answer-synthesis]", *pattern_refs[:3]],
                capability_needs=["source_verification", "dataset_read"],
                ownership_boundary="Own read-only source verification evidence.",
                network=("enabled" if repo_policy.network_allowed_by_default else "none"),
            ),
        ]
        return _template(
            family,
            "research_synthesize_source_verify",
            "retrieval",
            False,
            False,
            node_specs,
            [["research-sources", "answer-synthesis"], ["answer-synthesis", "source-verify"]],
            ["."],
            ["External web may be unavailable; use local corpus first and escalate through Overlooker if evidence is insufficient."],
            ["One researcher builds evidence, one synthesizer answers, one verifier checks sources."],
            [
                "Classified as retrieval, so no writer node is planned.",
                "Selected a research and source-verifier route before considering multi-candidate reasoning.",
                "Large agent pools are deferred unless evidence conflict remains after source verification.",
            ],
            [
                "Selected retrieval patterns favor source evidence, citation mapping, and verifier gates.",
                "Experience is applied as routing knowledge; compiler permissions still block ungrounded network access.",
            ],
        )

    if family == "finance_calculation":
        node_specs = [
            _node_spec(
                "collect-financial-inputs",
                "research",
                "researcher",
                "Collect financial inputs, assumptions, and source notes for the requested calculation.",
                [],
                ["financial_inputs", "source_notes"],
                "stage-1",
                "Finance tasks fail first on missing inputs or stale assumptions.",
                "No predecessor is needed because input collection starts from the prompt.",
                "Runs before calculation to freeze assumptions.",
                "Extract required values, dates, units, and source notes. Do not edit files.",
                ["read_repo", "dataset_read"] + (["network_search"] if repo_policy.network_allowed_by_default else []),
                ["financial_inputs", "source_notes", "assumption_log"],
                ["objective", "available_tooling_profiles", *pattern_refs[:2]],
                capability_needs=["finance_inputs", "dataset_read"],
                ownership_boundary="Own financial inputs and assumptions only.",
                network=("enabled" if repo_policy.network_allowed_by_default else "none"),
            ),
            _node_spec(
                "calculate-answer",
                "calculation",
                "calculator",
                "Apply the relevant formula and produce calculator-backed numerical evidence.",
                ["collect-financial-inputs"],
                ["formula_trace", "calculated_answer"],
                "stage-2",
                "A dedicated calculator separates arithmetic from source collection.",
                "Calculation waits for frozen inputs and assumptions.",
                "Serial because the formula trace must use one input ledger.",
                "Compute the answer with explicit formula steps and calculator evidence.",
                ["read_repo", "finance_calculator"],
                ["formula_trace", "calculated_answer", "calculator_log"],
                ["workflow_skeleton.nodes[collect-financial-inputs]", *pattern_refs[:2]],
                capability_needs=["finance_calculator", "formula_application"],
                ownership_boundary="Own formula trace and computed answer only.",
            ),
            _node_spec(
                "formula-verify",
                "verification",
                "verifier",
                "Verify formulas, units, arithmetic, and answer formatting.",
                ["calculate-answer"],
                ["formula_check_report", "final_answer"],
                "stage-3",
                "Finance outputs require independent formula and arithmetic review.",
                "Verification depends on the calculated answer.",
                "Terminal read-only review gate.",
                "Check formulas, units, and arithmetic against the input ledger.",
                ["read_repo", "finance_calculator", "dataset_read"],
                ["formula_check_report", "final_answer", "resource_report"],
                ["workflow_skeleton.nodes[calculate-answer]", *pattern_refs[:3]],
                capability_needs=["formula_check", "finance_calculator"],
                ownership_boundary="Own read-only formula verification evidence.",
            ),
        ]
        return _template(
            family,
            "finance_inputs_calculation_formula_verify",
            "analysis",
            False,
            False,
            node_specs,
            [["collect-financial-inputs", "calculate-answer"], ["calculate-answer", "formula-verify"]],
            ["."],
            ["External market data may be unavailable; use provided/local inputs first and escalate if required values are missing."],
            ["One input researcher, one calculator, and one verifier are enough unless scenarios multiply."],
            [
                "Classified as finance calculation, so the plan centers on inputs, formula trace, and independent arithmetic verification.",
                "Rejected a code-repair template because no patch ownership or repo tests are intrinsic to the task.",
                "Multi-candidate work is deferred unless the objective asks for scenarios or conflicting methods.",
            ],
            [
                "Selected finance patterns emphasize formula evidence and source-grounded assumptions.",
                "Experience is applied as calculation structure; permission grounding keeps repo writes unavailable.",
            ],
        )

    if family == "terminal_execution":
        shell_commands = repo_policy.test_commands
        node_specs = [
            _node_spec(
                "plan-terminal-actions",
                "planning",
                "tool_planner",
                "Plan shell/container steps, write boundaries, and checkpoint evidence before execution.",
                [],
                ["shell_plan", "permission_risks"],
                "stage-1",
                "Terminal tasks need command and environment planning before execution.",
                "No predecessor is needed because this is the permission and checkpoint plan.",
                "Serial by design so execution has a single approved command plan.",
                "Produce a shell/container action plan with checkpoint evidence and permission risks. Do not execute commands.",
                ["read_repo"],
                ["shell_plan", "permission_risks", "checkpoint_plan"],
                ["objective", "repo_policy", *pattern_refs[:2]],
                capability_needs=["terminal_planning", "permission_review"],
                ownership_boundary="Own command plan and permission-risk notes only.",
            ),
            _node_spec(
                "execute-checkpoint",
                "execution",
                "executor",
                "Execute approved shell/container steps and capture checkpoint evidence.",
                ["plan-terminal-actions"],
                ["checkpoint_artifact", "execution_log"],
                "stage-2",
                "A separate executor makes shell/container permission use explicit and auditable.",
                "Execution waits for the command plan and Overlooker permission review.",
                "Serial because shell state changes must be ordered and checkpointed.",
                "Use only approved shell/container commands from the plan and record checkpoint artifacts.",
                ["read_repo", "run_shell", "container_exec"],
                ["checkpoint_artifact", "execution_log", "sandbox_events", "resource_report"],
                ["workflow_skeleton.nodes[plan-terminal-actions]", "repo_policy.test_commands", *pattern_refs[:2]],
                capability_needs=["terminal_shell", "container_exec", "checkpoint"],
                ownership_boundary="Own approved execution log and checkpoint artifacts only.",
                allowed_commands=shell_commands,
                permission_justification="Terminal execution needs shell/container permission, bounded by Overlooker-approved command plans and repo policy commands.",
            ),
            _node_spec(
                "verify-checkpoint",
                "verification",
                "verifier",
                "Verify checkpoint state, exit evidence, and resource report.",
                ["execute-checkpoint"],
                ["checkpoint_verification_report", "final_state_report"],
                "stage-3",
                "Terminal-Bench style tasks need checkpoint verification rather than patch review.",
                "Verification depends on execution evidence.",
                "Terminal read-only gate over command evidence and artifacts.",
                "Verify checkpoint artifacts and command outcomes without adding new state changes.",
                ["read_repo", "run_shell", "container_exec"],
                ["checkpoint_verification_report", "final_state_report", "resource_report"],
                ["workflow_skeleton.nodes[execute-checkpoint]", *pattern_refs[:3]],
                capability_needs=["checkpoint_verification", "terminal_shell"],
                ownership_boundary="Own checkpoint verification evidence only.",
                allowed_commands=shell_commands,
                permission_justification="Checkpoint verification may need read-only shell/container checks under the same command boundary.",
            ),
        ]
        return _template(
            family,
            "terminal_plan_execute_checkpoint_verify",
            "terminal_execution",
            False,
            True,
            node_specs,
            [["plan-terminal-actions", "execute-checkpoint"], ["execute-checkpoint", "verify-checkpoint"]],
            ["."],
            ["Concrete shell commands must be approved by Overlooker before execution if repo policy has no whitelist."],
            ["A planner, executor, and checkpoint verifier separate permission review from shell state changes."],
            [
                "Classified as terminal execution, so the selected topology is tool planning, checkpoint execution, and verification.",
                "Rejected code repair because Terminal-Bench success is environment state, not a source patch by default.",
                "Scaling adds specialist executors only if checkpoints split into independent environments.",
            ],
            [
                "Selected terminal patterns emphasize permission-gated execution and checkpoint evidence.",
                "Experience is applied as shell/container structure; Overlooker remains responsible for permission escalation.",
            ],
        )

    if family == "planning_state_transition":
        node_specs = [
            _node_spec(
                "ambiguity-check",
                "analysis",
                "ambiguity_detector",
                "Detect state-transition ambiguity before committing to a plan.",
                [],
                ["ambiguity_report", "state_assumptions"],
                "stage-1",
                "PlanCraft style tasks should not execute before ambiguous state rules are surfaced.",
                "No predecessor is needed because this is prompt-local diagnosis.",
                "Runs first and serializes downstream planning on clarified assumptions.",
                "Identify ambiguous state variables, transition rules, and required assumptions.",
                ["read_repo"],
                ["ambiguity_report", "state_assumptions"],
                ["objective", *pattern_refs[:2]],
                capability_needs=["ambiguity_detection", "state_modeling"],
                ownership_boundary="Own ambiguity report and state assumptions only.",
            ),
            _node_spec(
                "state-plan",
                "planning",
                "planner",
                "Produce a state-transition plan under the accepted assumptions.",
                ["ambiguity-check"],
                ["transition_plan", "state_trace"],
                "stage-2",
                "Planning must consume the ambiguity report before selecting actions.",
                "Depends on explicit state assumptions.",
                "Serial because one transition trace must be checked end to end.",
                "Create the transition plan and state trace from accepted assumptions.",
                ["read_repo"],
                ["transition_plan", "state_trace"],
                ["workflow_skeleton.nodes[ambiguity-check]", *pattern_refs[:2]],
                capability_needs=["state_planning"],
                ownership_boundary="Own transition plan and state trace only.",
            ),
            _node_spec(
                "transition-verify",
                "verification",
                "verifier",
                "Verify the state trace and flag unresolved ambiguity.",
                ["state-plan"],
                ["state_transition_report", "final_plan"],
                "stage-3",
                "A state verifier prevents ambiguous tasks from being treated as ordinary execution.",
                "Verification depends on transition trace.",
                "Terminal review gate over state consistency.",
                "Check every transition against the assumptions and report unresolved ambiguity.",
                ["read_repo"],
                ["state_transition_report", "final_plan"],
                ["workflow_skeleton.nodes[state-plan]", *pattern_refs[:3]],
                capability_needs=["state_transition_check", "ambiguity_review"],
                ownership_boundary="Own state-transition verification only.",
            ),
        ]
        return _template(
            family,
            "ambiguity_check_state_plan_verify",
            "analysis",
            False,
            False,
            node_specs,
            [["ambiguity-check", "state-plan"], ["state-plan", "transition-verify"]],
            ["."],
            ["If ambiguity remains after stage 1, stop for clarification rather than punishing workers."],
            ["One ambiguity detector, one planner, and one verifier are enough for the current deterministic route."],
            [
                "Classified as planning/state transition, so ambiguity detection is the first-class gate.",
                "Rejected retrieval and code repair paths because success is state consistency.",
                "Scaling adds candidate plans only after ambiguity is resolved.",
            ],
            [
                "Selected planning patterns emphasize ambiguity attribution and state verification.",
                "Experience is applied as a prompt-local reasoning structure, not a repo-write workflow.",
            ],
        )

    if family == "contest_reasoning":
        node_specs = [
            _node_spec(
                "candidate-generate",
                "reasoning",
                "proposer",
                "Generate diverse candidate solution strategies.",
                [],
                ["candidate_set", "strategy_notes"],
                "stage-1",
                "Hard judged reasoning benefits from multiple candidate strategies.",
                "No predecessor is needed because candidate generation starts from the prompt.",
                "This is the initial candidate pool; future scale can add parallel proposers.",
                "Generate diverse candidate strategies with assumptions and expected checks.",
                ["read_repo"],
                ["candidate_set", "strategy_notes"],
                ["objective", *pattern_refs[:2]],
                capability_needs=["candidate_generation", "contest_reasoning"],
                ownership_boundary="Own candidate strategies only.",
            ),
            _node_spec(
                "candidate-judge",
                "judging",
                "judge",
                "Rank candidates using sample checks, judge criteria, or pairwise comparison.",
                ["candidate-generate"],
                ["ranked_candidates", "judge_trace"],
                "stage-2",
                "A judge node separates candidate quality assessment from generation.",
                "Judging waits for candidate set.",
                "Serial here, but scale triggers can add pairwise judges.",
                "Evaluate candidates and explain ranking uncertainty.",
                ["read_repo", "run_shell"],
                ["ranked_candidates", "judge_trace", "validator_pass_rate"],
                ["workflow_skeleton.nodes[candidate-generate]", *pattern_refs[:3]],
                capability_needs=["judge", "pairwise_ranking"],
                ownership_boundary="Own candidate ranking and judge trace only.",
                allowed_commands=repo_policy.test_commands,
            ),
            _node_spec(
                "solution-synthesis",
                "synthesis",
                "synthesizer",
                "Synthesize the final solution from the highest-ranked candidate.",
                ["candidate-judge"],
                ["final_solution_candidate", "synthesis_trace"],
                "stage-3",
                "Final synthesis should use judged evidence instead of raw first-pass reasoning.",
                "Synthesis depends on candidate ranking.",
                "Serial join point before final verification.",
                "Create the final answer from ranked candidates and preserve reasoning evidence.",
                ["read_repo"],
                ["final_solution_candidate", "synthesis_trace"],
                ["workflow_skeleton.nodes[candidate-judge]", *pattern_refs[:3]],
                capability_needs=["solution_synthesis"],
                ownership_boundary="Own final solution candidate only.",
            ),
            _node_spec(
                "final-verify",
                "verification",
                "verifier",
                "Verify final solution with available judge/sample evidence.",
                ["solution-synthesis"],
                ["validation_report", "final_answer"],
                "stage-4",
                "Judged reasoning needs a final validator distinct from proposer and judge.",
                "Verification depends on synthesized solution.",
                "Terminal review gate.",
                "Validate final answer against judge criteria and sample checks.",
                ["read_repo", "run_shell"],
                ["validation_report", "final_answer", "resource_report"],
                ["workflow_skeleton.nodes[solution-synthesis]", *pattern_refs[:3]],
                capability_needs=["validator", "sample_tests"],
                ownership_boundary="Own final validation evidence only.",
                allowed_commands=repo_policy.test_commands,
            ),
        ]
        return _template(
            family,
            "candidate_generate_judge_synthesize_verify",
            "analysis",
            False,
            True,
            node_specs,
            [["candidate-generate", "candidate-judge"], ["candidate-judge", "solution-synthesis"], ["solution-synthesis", "final-verify"]],
            ["."],
            ["If judge confidence remains low, scale candidate generation and pairwise judging before final answer."],
            ["Four nodes are selected because generation, judging, synthesis, and validation have different failure modes."],
            [
                "Classified as contest reasoning, so multi-candidate work is worthwhile.",
                "Selected candidate generation plus judge before final synthesis.",
                "Scaling policy can expand to larger populations when ranking entropy remains high.",
            ],
            [
                "Selected reasoning patterns emphasize candidate diversity, judging, and validation.",
                "Experience is applied as adaptive scaling guidance rather than a fixed agent count.",
            ],
        )

    if family == "code_repair":
        return _code_repair_template(repo_policy, pattern_refs)

    return _analysis_template(pattern_refs)


def _analysis_template(pattern_refs: list[str]) -> dict[str, Any]:
    node_specs = [
        _node_spec(
            "analyze-task",
            "analysis",
            "analyst",
            "Analyze the prompt-local objective and produce a grounded answer plan.",
            [],
            ["analysis_report", "answer_plan"],
            "stage-1",
            "Prompt-local analysis should not allocate a writer or shell executor by default.",
            "No predecessor is needed because this starts from the objective.",
            "Runs first and remains read-only.",
            "Analyze the objective and produce the smallest sufficient answer plan. Do not edit files.",
            ["read_repo"],
            ["analysis_report", "answer_plan"],
            ["objective", *pattern_refs[:2]],
            capability_needs=["analysis"],
            ownership_boundary="Own prompt-local analysis and answer plan only.",
        ),
        _node_spec(
            "verify-answer-plan",
            "verification",
            "verifier",
            "Verify the answer plan for internal consistency and unsupported assumptions.",
            ["analyze-task"],
            ["verification_report", "final_answer"],
            "stage-2",
            "Even cheap prompt-local paths need an explicit consistency check.",
            "Verification depends on the analysis report.",
            "Terminal read-only review gate.",
            "Check the answer plan for consistency and unsupported assumptions.",
            ["read_repo"],
            ["verification_report", "final_answer"],
            ["workflow_skeleton.nodes[analyze-task]", *pattern_refs[:2]],
            capability_needs=["consistency_check"],
            ownership_boundary="Own read-only answer verification only.",
        ),
    ]
    return _template(
        "analysis",
        "prompt_analysis_then_consistency_verify",
        "analysis",
        False,
        False,
        node_specs,
        [["analyze-task", "verify-answer-plan"]],
        ["."],
        ["No concrete external tool, dataset, terminal, finance, or repo patch need was detected."],
        ["One analyst and one verifier keep prompt-local tasks cheap while preserving an evidence gate."],
        [
            "Classified as analysis, so no writer, browser, calculator, shell, or candidate pool is pre-allocated.",
            "Selected the cheapest verifiable path with an explicit consistency verifier.",
            "Scale only if verification exposes missing knowledge or ambiguity.",
        ],
        [
            "Selected analysis patterns emphasize cheap prompt-local reasoning with a verifier gate.",
            "Experience is applied as a minimal structure; compiler still enforces permission grounding.",
        ],
    )


def _code_repair_template(repo_policy: RepoPolicy, pattern_refs: list[str]) -> dict[str, Any]:
    node_specs = [
        _node_spec(
            "explore-context",
            "exploration",
            "explorer",
            "Inspect repository touchpoints and summarize likely implementation surfaces.",
            [],
            ["touchpoint_map", "analysis_log"],
            "stage-1",
            "Read-only source discovery is separable from tests and writes.",
            "No predecessor is needed because this is initial discovery.",
            "This can run in parallel with test exploration because both are read-only.",
            "Inspect the workspace and produce a concise touchpoint map. Do not edit files.",
            ["read_repo"],
            ["touchpoint_map", "analysis_log", "resource_report"],
            ["objective", "repo_policy.allowed_read_paths", *pattern_refs[:2]],
            capability_needs=["repo_read"],
            ownership_boundary="Own read-only source touchpoint map only.",
        ),
        _node_spec(
            "explore-tests",
            "exploration",
            "explorer",
            "Inspect validation commands, expected test evidence, and failure risks.",
            [],
            ["test_plan", "risk_notes"],
            "stage-1",
            "Validation discovery has independent ownership from source touchpoint discovery.",
            "No predecessor is needed because validation discovery starts from repo policy.",
            "This runs in parallel with source exploration and joins before implementation.",
            "Inspect available validation commands and produce a test plan. Do not edit source files.",
            ["read_repo"],
            ["test_plan", "risk_notes", "resource_report"],
            ["repo_policy.test_commands", *pattern_refs[:3]],
            capability_needs=["repo_read", "test_planning"],
            ownership_boundary="Own read-only validation map only.",
        ),
        _node_spec(
            "implement",
            "implementation",
            "worker",
            "Apply the minimal code change after exploration outputs are available.",
            ["explore-context", "explore-tests"],
            ["diff", "worker_log"],
            "stage-2",
            "A single bounded writer avoids conflicting edits until ownership is proven wider.",
            "Implementation waits for both exploration artifacts.",
            "This is serial because write ownership is not yet proven independent.",
            "Implement the smallest change required by the objective using exploration outputs.",
            ["read_repo", "write_patch"],
            ["diff", "worker_log", "sandbox_events", "resource_report"],
            ["workflow_skeleton.nodes[explore-context]", "workflow_skeleton.nodes[explore-tests]", *pattern_refs[:3]],
            capability_needs=["repo_read", "write_patch"],
            ownership_boundary="Own bounded patch under repo policy write paths.",
            write_paths=list(repo_policy.allowed_write_paths),
        ),
        _node_spec(
            "verify",
            "verification",
            "verifier",
            "Run read-only verification and prepare validator-ready evidence.",
            ["implement"],
            ["test_report", "validator_ready_evidence"],
            "stage-3",
            "A terminal verifier checks the implementation handoff before acceptance.",
            "Verification depends on implementation output.",
            "This is a serial terminal review gate.",
            "Run or inspect repo-grounded verification and produce validator-ready evidence.",
            ["read_repo", "run_tests"],
            ["test_report", "validator_ready_evidence", "resource_report"],
            ["repo_policy.test_commands", "workflow_skeleton.nodes[implement]", *pattern_refs],
            capability_needs=["repo_read", "test"],
            ownership_boundary="Own read-only verification evidence.",
            allowed_commands=repo_policy.test_commands,
        ),
    ]
    return _template(
        "code_repair",
        "parallel_explore_single_writer_verify_model_agents",
        "implementation",
        True,
        True,
        node_specs,
        [["explore-context", "implement"], ["explore-tests", "implement"], ["implement", "verify"]],
        ["."],
        ["Exact changed files remain unknown until exploration completes."],
        [
            "Two read-only exploration surfaces can run independently.",
            "One writer is enough until exploration proves independent write ownership.",
            "A separate verifier gives explicit read-only acceptance evidence.",
        ],
        [
            "Compared single-agent, selected four-agent, and larger hierarchical candidates.",
            "Selected model_agent units so the runtime is not bound to Codex CLI.",
            "Kept large-scale expansion as trigger-based rather than pre-allocating unused agents.",
        ],
        [
            "Selected code repair patterns guide topology, artifact handoff, review, and scale triggers.",
            "Patterns are used only for structure; compiler and permission grounding still enforce execution safety.",
        ],
    )


def _node_spec(
    node_id: str,
    phase: str,
    role: str,
    goal: str,
    depends_on: list[str],
    expected_outputs: list[str],
    stage_id: str,
    reason: str,
    dependency: str,
    parallelism: str,
    prompt: str,
    permission_intents: list[str],
    required_evidence: list[str],
    refs: list[str],
    *,
    capability_needs: list[str],
    ownership_boundary: str,
    write_paths: list[str] | None = None,
    read_paths: list[str] | None = None,
    allowed_commands: list[list[str]] | None = None,
    network: str = "none",
    permission_justification: str | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "phase": phase,
        "role": role,
        "goal": goal,
        "depends_on": depends_on,
        "expected_outputs": expected_outputs,
        "stage_id": stage_id,
        "reason": reason,
        "dependency": dependency,
        "parallelism": parallelism,
        "prompt": prompt,
        "permission_intents": permission_intents,
        "required_evidence": required_evidence,
        "refs": refs,
        "capability_needs": capability_needs,
        "ownership_boundary": ownership_boundary,
        "write_paths": write_paths or [],
        "read_paths": read_paths or ["."],
        "allowed_commands": allowed_commands or [],
        "network": network,
        "permission_justification": permission_justification,
        "acceptance_criteria": acceptance_criteria,
    }


def _template(
    family: str,
    topology: str,
    task_kind: str,
    requires_code_change: bool,
    requires_tests: bool,
    node_specs: list[dict[str, Any]],
    edges: list[list[str]],
    repo_touchpoints: list[str],
    unknowns: list[str],
    allocation_rationale: list[str],
    deliberation_trace: list[str],
    experience_rationale: list[str],
) -> dict[str, Any]:
    return {
        "family": family,
        "topology": topology,
        "task_kind": task_kind,
        "requires_code_change": requires_code_change,
        "requires_tests": requires_tests,
        "node_specs": node_specs,
        "edges": edges,
        "repo_touchpoints": repo_touchpoints,
        "unknowns": unknowns,
        "allocation_rationale": allocation_rationale,
        "deliberation_trace": deliberation_trace,
        "experience_rationale": experience_rationale,
    }


def _work_assignment_plan(nodes: list[dict[str, Any]], node_specs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    spec_by_id = {str(spec.get("node_id")): spec for spec in (node_specs or [])}
    assignments: list[dict[str, Any]] = []
    for node in nodes:
        role = str(node.get("role") or "worker")
        phase = str(node.get("phase") or "analysis")
        node_id = str(node.get("node_id") or "")
        depends_on = [str(item) for item in node.get("depends_on", [])]
        expected_outputs = [str(item) for item in node.get("expected_outputs", [])]
        spec = spec_by_id.get(node_id, {})
        assignments.append(
            {
                "node_id": node_id,
                "role": role,
                "stage_id": node.get("node_selection_principles", {}).get("stage_id", phase),
                "agent_type": _agent_type_for_role(role),
                "input_schema": {
                    "required": ["objective", "upstream_artifacts", "permission_plan"],
                    "upstream_nodes": depends_on,
                },
                "output_schema": {"required": expected_outputs or ["agent_report"]},
                "ownership_boundary": spec.get("ownership_boundary") or _ownership_boundary_for_role(role),
                "parallel_safe": not bool(depends_on) and "write_patch" not in spec.get("permission_intents", []),
                "parallel_safety_reason": (
                    "Dependency-free and no write_patch permission is requested."
                    if not depends_on and "write_patch" not in spec.get("permission_intents", [])
                    else "Runs after declared upstream handoff."
                ),
                "failure_takeover": "Overlooker may fork from accepted upstream state or request Director replan.",
                "selection_basis": f"{role} selected for {phase} because its outputs are required downstream.",
                "capability_needs": spec.get("capability_needs") or _capability_needs_for_role(role, phase),
            }
        )
    return assignments


def _permission_plan(instantiations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for inst in instantiations:
        grounding = inst.get("permission_grounding", {})
        read_paths = grounding.get("allowed_read_paths", ["."])
        write_paths = grounding.get("allowed_write_paths", [])
        commands = grounding.get("allowed_commands", [])
        network = grounding.get("network", "none")
        explicit_intents = grounding.get("permission_intents")
        if isinstance(explicit_intents, list) and explicit_intents:
            intents = [str(intent) for intent in explicit_intents]
        else:
            intents = ["read_repo"]
            if write_paths:
                intents.append("write_patch")
            if commands:
                intents.append("run_tests" if inst.get("phase") == "verification" else "run_shell")
            if network != "none":
                intents.append("network_search")
        plans.append(
            {
                "node_id": inst["node_id"],
                "skeleton_node_id": inst["skeleton_node_id"],
                "permission_intents": sorted(set(intents), key=intents.index),
                "minimum_boundary": {
                    "read_paths": read_paths,
                    "write_paths": write_paths,
                    "allowed_commands": commands,
                    "network": network,
                    "secret_access": False,
                },
                "why_needed": grounding.get("justification", "Permission is grounded by repo policy."),
                "fallback_if_denied": "Return to Overlooker permission review and request Director replan with a lower-permission path.",
            }
        )
    return plans


def _agent_allocation(node_specs: list[dict[str, Any]], rationale: list[str]) -> dict[str, Any]:
    roles: dict[str, int] = {}
    for spec in node_specs:
        role = str(spec.get("role") or "worker")
        roles[role] = roles.get(role, 0) + 1
    return {
        "total_agents": len(node_specs),
        "roles": roles,
        "allocation_rationale": rationale,
        "agent_count_confidence": "medium",
    }


def _alternative_skeletons(template: dict[str, Any], node_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stage_mapping = _stage_mapping(node_specs)
    family = str(template["family"])
    return [
        {
            "name": "small_single_agent",
            "estimated_agents": 1,
            "stage_mapping": {"all": [spec["node_id"] for spec in node_specs]},
            "strengths": ["lowest overhead"],
            "weaknesses": ["collapses specialist verification into the same context"],
            "selected": False,
            "rejection_reason": "Rejected unless all evidence is prompt-local and direct verification is trivial.",
        },
        {
            "name": f"selected_{template['topology']}",
            "estimated_agents": len(node_specs),
            "stage_mapping": stage_mapping,
            "strengths": ["task-family-specific roles", "explicit evidence handoffs", "bounded permissions"],
            "weaknesses": ["does not pre-allocate large specialist pools before evidence exists"],
            "selected": True,
            "rejection_reason": "",
        },
        {
            "name": "large_dynamic_hierarchy",
            "estimated_agents": 12 if family != "contest_reasoning" else 20,
            "stage_mapping": {
                "stage-1": ["specialist discovery or candidate pool"],
                "stage-2": ["parallel specialists or judges"],
                "stage-3": ["synthesis and verification board"],
            },
            "strengths": ["can scale to many independent evidence sources, environments, or candidate strategies"],
            "weaknesses": ["too costly before uncertainty proves breadth is useful"],
            "selected": False,
            "rejection_reason": "Scale triggers are defined, but current deterministic evidence does not justify this width.",
        },
    ]


def _scaling_policy(
    family: str,
    node_count: int,
    task_profile: dict[str, Any],
    scaling_pattern_refs: list[str],
) -> dict[str, Any]:
    worth_multi_candidate = bool(task_profile.get("estimated_budget", {}).get("worth_multi_candidate"))
    current_level = 2 if worth_multi_candidate else 1
    scale_name = (
        "medium_pool_5_to_8_candidates_pairwise_K2_or_K3"
        if worth_multi_candidate
        else "single_candidate_baseline_with_specialist_verifier"
    )
    family_triggers = {
        "retrieval": [
            "source verifier finds conflicting evidence",
            "citation map has unsupported answer claims",
            "local corpus lacks required evidence and browser permission is unavailable",
        ],
        "finance_calculation": [
            "required inputs are missing or stale",
            "formula verifier finds unit or arithmetic mismatch",
            "objective introduces multiple scenarios or sensitivity analysis",
        ],
        "terminal_execution": [
            "checkpoint verification fails for environment reasons",
            "command plan needs unavailable shell or container permission",
            "independent checkpoints can be split across environments",
        ],
        "planning_state_transition": [
            "ambiguity detector reports unresolved state rules",
            "state verifier finds invalid transition",
            "multiple valid plans remain after clarification",
        ],
        "contest_reasoning": [
            "candidate diversity is low or duplicate strategies dominate",
            "ranking entropy remains high after judging",
            "validator pass rate is low but some candidates are partially correct",
        ],
        "code_repair": [
            "explorers identify more than three independent write ownership areas",
            "implementation diff spans unrelated packages or languages",
            "validation requires multiple incompatible environments",
        ],
    }
    return {
        "policy_id": "seed-scaling-adaptive-population-curriculum",
        "current_scale_level": current_level,
        "requested_scale_level": current_level,
        "scale_level_name": scale_name,
        "scale_triggers": family_triggers.get(family, ["verification uncertainty remains high"]),
        "scale_down_triggers": [
            "single-agent cheap path has direct prompt-local verification",
            "all candidates fail for the same missing-source or missing-permission reason",
            "validator evidence shows more agents would increase cost without new information",
        ],
        "max_planned_agents_for_current_task": node_count,
        "expansion_strategy": [
            "add specialist nodes only for independent evidence sources, environments, write areas, or candidate pools",
            "increase candidate pool only when validator evidence shows partial competence or unresolved ranking uncertainty",
            "route to permission review before adding agents that require blocked tools or network",
            "promote repeated dynamic corrections into experience-library patterns",
        ],
        "requires_replan_when": [
            "task_profile classification is contradicted by node evidence",
            "verification requires unavailable permissions",
            "Overlooker recommends request_director_replan",
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
            "next_scaling_hint",
        ],
        "budget_gate": {
            "max_candidate_count": 8 if worth_multi_candidate else 2,
            "max_comparison_count": 24 if worth_multi_candidate else 2,
            "max_mutation_rounds": 1 if worth_multi_candidate else 0,
            "max_planned_agents": node_count,
        },
        "decision_basis": _basis(
            "basis-scaling-policy",
            ["objective", "task_profile", "workflow_skeleton.alternative_skeletons", *scaling_pattern_refs],
            [family, "adaptive scaling is evidence-triggered, not a fixed agent count"],
            "workflow_skeleton.scaling_policy",
            "Scale up, scale down, or route to research/specialists before recompiling the graph.",
        ),
    }


def _execution_estimate(task_profile: dict[str, Any], node_count: int) -> dict[str, Any]:
    budget = task_profile.get("estimated_budget", {})
    return {
        "estimated_agents": node_count,
        "estimated_tokens": int(budget.get("estimated_tokens", 5_000 + node_count * 3_000)),
        "estimated_wall_time_sec": int(budget.get("estimated_wall_time_sec", 120 + node_count * 90)),
        "expected_success_probability": _success_probability(str(task_profile.get("primary_task_family") or "analysis")),
        "budget_gate": task_profile.get(
            "budget_gate",
            {
                "max_agents_before_replan": node_count,
                "max_tokens_before_replan": 12_000 + node_count * 4_000,
                "max_wall_time_sec_before_replan": 600,
            },
        ),
        "stop_condition": str(task_profile.get("stop_condition") or "Stop when verification evidence passes."),
        "escalation_condition": str(task_profile.get("escalation_condition") or "Escalate when verification requires unavailable permissions."),
        "cheaper_alternative": str(task_profile.get("cheaper_alternative") or "Use a single-agent cheap path for prompt-local tasks."),
    }


def _linear_requirement_flow(template: dict[str, Any], pattern_refs: list[str]) -> list[dict[str, Any]]:
    flow = []
    for order, (stage_id, specs) in enumerate(_stage_specs(template["node_specs"]).items(), start=1):
        outputs: list[str] = []
        evidence: list[str] = []
        inputs = ["objective", "experience_candidates"]
        if order > 1:
            previous_stage = f"stage-{order - 1}"
            inputs.append(f"linear_requirement_flow[{previous_stage}]")
        for spec in specs:
            outputs.extend(str(item) for item in spec.get("expected_outputs", []))
            evidence.extend(str(item) for item in spec.get("required_evidence", [])[:2])
        flow.append(
            _flow_stage(
                stage_id,
                order,
                _stage_name(specs),
                _stage_purpose(specs),
                inputs,
                sorted(set(outputs), key=outputs.index),
                "medium",
                sorted(set(evidence), key=evidence.index) or ["agent_report"],
                ["objective", *pattern_refs[:3]],
            )
        )
    return flow


def _stage_structure_decisions(template: dict[str, Any], selected_pattern_ids: list[str]) -> list[dict[str, Any]]:
    decisions = []
    for stage_id, specs in _stage_specs(template["node_specs"]).items():
        roles = {str(spec.get("role")) for spec in specs}
        if len(specs) > 1:
            selected = "parallel_exploration" if "explorer" in roles else "specialist_pool"
        elif any(role in roles for role in {"verifier", "judge"}):
            selected = "review_gate"
        elif any(role in roles for role in {"tool_planner", "executor"}):
            selected = "tool_planning"
        elif any(role in roles for role in {"researcher"}):
            selected = "research_route"
        else:
            selected = "single_agent"
        decisions.append(
            _structure_decision(
                stage_id,
                selected,
                _stage_purpose(specs),
                selected_pattern_ids,
            )
        )
    return decisions


def _research_route_decisions(template: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = []
    for stage_id, specs in _stage_specs(template["node_specs"]).items():
        needed = any(
            intent in spec.get("permission_intents", [])
            for spec in specs
            for intent in ["dataset_read", "browser", "network_search", "finance_calculator"]
        ) or any(str(spec.get("role")) in {"researcher", "calculator"} for spec in specs)
        decisions.append(
            _research_decision(
                stage_id,
                needed,
                _research_reason(specs, needed),
            )
        )
    return decisions


def _per_stage_agent_allocation(template: dict[str, Any]) -> list[dict[str, Any]]:
    allocations = []
    for stage_id, specs in _stage_specs(template["node_specs"]).items():
        allocations.append(
            _allocation(
                stage_id,
                [
                    (
                        str(spec.get("role") or "worker"),
                        str(spec.get("goal") or "Produce stage evidence."),
                        str((spec.get("expected_outputs") or ["agent_report"])[0]),
                        str(spec.get("node_id")),
                    )
                    for spec in specs
                ],
                _allocation_reason(specs),
            )
        )
    return allocations


def _plan_derivation_trace(template: dict[str, Any]) -> list[str]:
    traces = []
    for stage_id, specs in _stage_specs(template["node_specs"]).items():
        node_ids = ", ".join(str(spec["node_id"]) for spec in specs)
        traces.append(
            f"basis-structure-{stage_id}: {stage_id} selected {_stage_purpose(specs)}, producing nodes {node_ids}."
        )
    return traces


def _draft_plan_review(
    family: str,
    node_specs: list[dict[str, Any]],
    selected_pattern_ids: list[str],
) -> dict[str, Any]:
    node_ids = [str(spec["node_id"]) for spec in node_specs]
    finding_text = (
        "The draft uses a task-family-specific workflow and avoids falling back to the code repair template."
        if family != "code_repair"
        else "The draft keeps the code repair topology because task_profile.primary_task_family is code_repair."
    )
    return {
        "review_id": f"draft-review-{family}-model-agent-1",
        "reviewed_draft_fields": [
            "linear_requirement_flow",
            "stage_structure_decisions",
            "research_route_decisions",
            "per_stage_agent_allocation",
            "nodes",
            "edges",
            "node_instantiations",
            "experience_pattern_ids",
        ],
        "structural_verdict": "pass",
        "structure_findings": [
            {
                "finding_id": "draft-finding-task-family-template",
                "severity": "info",
                "target": "workflow_skeleton.nodes",
                "finding": finding_text,
                "recommendation": "Keep task-family-specific nodes unless evidence contradicts the task_profile.",
                "decision_basis": _basis(
                    "basis-draft-review-task-family",
                    ["draft_plan.nodes", "task_profile", *[f"experience:{item}" for item in selected_pattern_ids[:3]]],
                    [family, "template matches task family", *node_ids],
                    "workflow_skeleton.nodes",
                    "Replace the topology if Overlooker or verification evidence contradicts the task family.",
                ),
            }
        ],
        "missing_capabilities": ["none"],
        "recommended_changes": [
            {
                "change_id": "draft-change-task-family-template",
                "change_type": "no_change",
                "target": "workflow_skeleton.nodes",
                "rationale": "The final node set matches the diagnosed task family and permission needs.",
            }
        ],
        "applied_changes": [
            {
                "change_id": "draft-change-task-family-template",
                "applied": True,
                "final_targets": [f"workflow_skeleton.nodes[{node_id}]" for node_id in node_ids],
                "result": "The deterministic Director kept the reviewed task-family-specific node set.",
            }
        ],
        "rejected_changes": [],
        "final_structure_summary": "The final graph has explicit task profile, assignment, permission intent, evidence handoff, and verification boundaries.",
    }


def _basis(
    basis_id: str,
    source_refs: list[str],
    matched_signals: list[str],
    target: str,
    action: str,
) -> dict[str, Any]:
    return {
        "basis_id": basis_id,
        "source_refs": source_refs or ["objective"],
        "matched_signals": matched_signals,
        "assumptions": ["The current evidence is enough for this structural choice."],
        "invalidation_signals": [
            "Exploration finds broader independent ownership.",
            "Verification or Overlooker evidence contradicts the assumption.",
        ],
        "confidence": "medium",
        "correction_target": target,
        "correction_action": action,
    }


def _flow_stage(
    stage_id: str,
    order: int,
    name: str,
    purpose: str,
    inputs: list[str],
    outputs: list[str],
    risk_level: str,
    evidence: list[str],
    refs: list[str],
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "order": order,
        "name": name,
        "purpose": purpose,
        "inputs": inputs,
        "outputs": outputs,
        "risk_level": risk_level,
        "acceptance_evidence": evidence,
        "decision_basis": _basis(
            f"basis-{stage_id}",
            refs,
            [name, purpose],
            f"linear_requirement_flow[{stage_id}]",
            "Merge, split, or reorder this stage during replanning.",
        ),
    }


def _structure_decision(
    stage_id: str,
    selected: str,
    reason: str,
    pattern_ids: list[str],
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "candidate_structures": [
            {"structure": "single_agent", "fit": "medium", "reason": "Lower overhead but weaker parallel coverage."},
            {"structure": selected, "fit": "high", "reason": reason},
            {"structure": "hierarchical_subteams", "fit": "low", "reason": "Reserve for scale triggers."},
        ],
        "selected_structure": selected,
        "selection_reason": reason,
        "anti_signals": [
            "Task proves trivial and the stage can be merged.",
            "Stage requires unavailable permission or external research.",
        ],
        "experience_pattern_ids": pattern_ids,
        "decision_basis": _basis(
            f"basis-structure-{stage_id}",
            [f"linear_requirement_flow[{stage_id}]", *[f"experience:{item}" for item in pattern_ids]],
            [selected, reason],
            f"stage_structure_decisions[{stage_id}]",
            "Switch structure, split stage, or request Director replan.",
        ),
    }


def _research_decision(stage_id: str, needed: bool, reason: str) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "research_needed": needed,
        "reason": reason,
        "available_sources": ["experience_candidates", "repo_files", "repo_policy"],
        "blocked_sources": ["external_web"],
        "planned_queries_or_searches": [
            "search local repo paths named by the objective",
            "inspect local experience candidates and compiler constraints",
        ],
        "adopted_expert_route": "Use local experience patterns and repository evidence before requesting external research.",
        "fallback_if_research_blocked": "Proceed conservatively and request replan if local evidence is insufficient.",
        "decision_basis": _basis(
            f"basis-research-{stage_id}",
            [f"linear_requirement_flow[{stage_id}]", "network:none"],
            ["local research route", reason],
            f"research_route_decisions[{stage_id}]",
            "Add a research node or request permission review if local evidence is insufficient.",
        ),
    }


def _allocation(
    stage_id: str,
    agents: list[tuple[str, str, str, str]],
    reason: str,
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "agent_count": len(agents),
        "count_reason": reason,
        "decision_basis": _basis(
            f"basis-allocation-{stage_id}",
            [f"stage_structure_decisions[{stage_id}]"],
            ["agent count derived from width, uncertainty, risk, and validation burden"],
            f"per_stage_agent_allocation[{stage_id}]",
            "Add, remove, split, or merge agents and recompile graph.",
        ),
        "agents": [
            {
                "role": role,
                "task": task,
                "inputs": ["objective", f"linear_requirement_flow[{stage_id}]"],
                "outputs": [output],
                "ownership_boundary": f"Owns {output} only.",
                "write_authority": "bounded write path" if role == "worker" else "none",
                "handoff_target": node_id,
                "decision_basis": _basis(
                    f"basis-agent-{stage_id}-{node_id}",
                    [f"per_stage_agent_allocation[{stage_id}]"],
                    [role, task],
                    f"node:{node_id}",
                    "Replace, remove, split, or add handoff constraints.",
                ),
            }
            for role, task, output, node_id in agents
        ],
    }


def _stage_specs(node_specs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    stages: dict[str, list[dict[str, Any]]] = {}
    for spec in node_specs:
        stage_id = str(spec.get("stage_id") or "stage-1")
        stages.setdefault(stage_id, []).append(spec)
    return dict(sorted(stages.items(), key=lambda item: item[0]))


def _stage_mapping(node_specs: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        stage_id: [str(spec["node_id"]) for spec in specs]
        for stage_id, specs in _stage_specs(node_specs).items()
    }


def _stage_name(specs: list[dict[str, Any]]) -> str:
    roles = {str(spec.get("role") or "") for spec in specs}
    if "researcher" in roles:
        return "Collect task evidence"
    if "calculator" in roles:
        return "Calculate answer"
    if "tool_planner" in roles:
        return "Plan terminal actions"
    if "executor" in roles:
        return "Execute checkpoint"
    if "ambiguity_detector" in roles:
        return "Detect ambiguity"
    if "judge" in roles:
        return "Judge candidates"
    if "worker" in roles:
        return "Implement bounded change"
    if "verifier" in roles:
        return "Verify evidence"
    return "Produce stage artifact"


def _stage_purpose(specs: list[dict[str, Any]]) -> str:
    if len(specs) == 1:
        return str(specs[0].get("reason") or specs[0].get("goal") or "Produce required evidence.")
    outputs = ", ".join(str(spec.get("node_id")) for spec in specs)
    return f"Coordinate independent specialist nodes for {outputs} before downstream handoff."


def _research_reason(specs: list[dict[str, Any]], needed: bool) -> str:
    if needed:
        return "This stage needs local corpus, source, tool, calculator, or dataset evidence before downstream work."
    return "This stage consumes upstream artifacts and does not need a new research route."


def _allocation_reason(specs: list[dict[str, Any]]) -> str:
    if len(specs) == 1:
        spec = specs[0]
        return f"One {spec.get('role')} is enough because the stage owns {', '.join(spec.get('expected_outputs', []))}."
    return "Multiple agents are allocated only because their outputs are independent and join downstream."


def _agent_type_for_role(role: str) -> str:
    if role in {"verifier", "reviewer"}:
        return "verification"
    if role in {"researcher", "explorer", "tool_planner", "calculator", "ambiguity_detector"}:
        return "tool_or_expert"
    if role in {"judge", "proposer", "synthesizer"}:
        return "candidate_selection"
    if role in {"worker", "executor"}:
        return "generalist"
    return "specialist"


def _ownership_boundary_for_role(role: str) -> str:
    if role in {"verifier", "reviewer"}:
        return "Read-only verification evidence."
    if role in {"researcher", "explorer"}:
        return "Read-only discovery outputs only."
    if role == "worker":
        return "Bounded patch ownership under repo policy write paths."
    return "Own declared stage artifact only."


def _capability_needs_for_role(role: str, phase: str) -> list[str]:
    text = f"{role} {phase}".lower()
    needs = ["repo_read"]
    if any(term in text for term in ["worker", "implement"]):
        needs.append("write_patch")
    if any(term in text for term in ["verify", "test"]):
        needs.append("test")
    if any(term in text for term in ["terminal", "shell", "executor"]):
        needs.append("terminal_shell")
    if "research" in text:
        needs.append("dataset_read")
    if "calculator" in text:
        needs.append("finance_calculator")
    return needs


def _stop_condition(family: str) -> str:
    return {
        "retrieval": "Stop when the answer is supported by cited source evidence and source verification passes.",
        "finance_calculation": "Stop when formula, units, arithmetic, and answer formatting pass verification.",
        "planning_state_transition": "Stop when ambiguity is resolved or explicitly reported and the state trace verifies.",
        "code_repair": "Stop when implementation evidence and read-only repo verification pass.",
        "terminal_execution": "Stop when checkpoint artifact, shell/container exit evidence, and verifier report pass.",
        "contest_reasoning": "Stop when final answer passes judge/sample verification or ranking uncertainty is exhausted.",
    }.get(family, "Stop when required verification evidence passes.")


def _escalation_condition(family: str) -> str:
    return {
        "retrieval": "Escalate when required evidence is unavailable locally, sources conflict, or browser/network permission is needed.",
        "finance_calculation": "Escalate when required values are missing, formulas conflict, or source freshness cannot be verified.",
        "planning_state_transition": "Escalate when state rules remain ambiguous after the ambiguity detector.",
        "code_repair": "Escalate when exploration invalidates single-writer ownership or verification needs unavailable permissions.",
        "terminal_execution": "Escalate when shell/container permission is unavailable or checkpoint verification fails from environment drift.",
        "contest_reasoning": "Escalate when judge evidence is noisy, candidate ranking remains high entropy, or validation tools are unavailable.",
    }.get(family, "Escalate when verifier needs permissions or knowledge sources not in the plan.")


def _cheaper_alternative(family: str) -> str:
    return {
        "retrieval": "Use one source-answer agent when the prompt already contains sufficient evidence and citations are direct.",
        "finance_calculation": "Use one calculator agent when all inputs and formula are fully specified in the prompt.",
        "planning_state_transition": "Use one planner when state rules are unambiguous and directly checkable.",
        "code_repair": "Use one model_agent only when the exact file and test target are already known.",
        "terminal_execution": "Use one tool planner/executor only when commands are explicit, whitelisted, and low risk.",
        "contest_reasoning": "Use one reasoning agent only for easy problems with deterministic sample checks.",
    }.get(family, "Use a single-agent cheap path when all required knowledge is prompt-local and direct verification is available.")


def _success_probability(family: str) -> float:
    return {
        "retrieval": 0.68,
        "finance_calculation": 0.74,
        "planning_state_transition": 0.7,
        "code_repair": 0.72,
        "terminal_execution": 0.62,
        "contest_reasoning": 0.58,
    }.get(family, 0.65)


def _skeleton_node(
    *,
    node_id: str,
    phase: str,
    role: str,
    goal: str,
    depends_on: list[str],
    expected_outputs: list[str],
    stage_id: str,
    reason: str,
    refs: list[str],
    pattern_ids: list[str],
    dependency: str,
    parallelism: str,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "phase": phase,
        "role": role,
        "goal": goal,
        "depends_on": depends_on,
        "expected_outputs": expected_outputs,
        "experience_pattern_ids": pattern_ids,
        "node_selection_principles": {
            "stage_id": stage_id,
            "selected_for": [reason],
            "role_principle": f"{role} is selected because {reason.lower()}",
            "dependency_principle": dependency,
            "parallelism_principle": parallelism,
            "evidence_principle": f"{', '.join(expected_outputs)} are sufficient handoff artifacts for downstream review.",
            "experience_pattern_ids": pattern_ids,
            "decision_basis": _basis(
                f"basis-node-{node_id}",
                refs,
                [role, reason],
                f"workflow_skeleton.nodes[{node_id}]",
                "Remove, merge, split, reorder, or change this node role.",
            ),
        },
    }


def _instantiation(
    *,
    skeleton_node_id: str,
    node_id: str,
    phase: str,
    goal: str,
    executor_kind: str,
    model_provider: str,
    model: str | None,
    pattern_ids: list[str],
    write_paths: list[str],
    prompt: str,
    stage_id: str,
    read_paths: list[str] | None = None,
    allowed_commands: list[list[str]] | None = None,
    network: str = "none",
    permission_intents: list[str] | None = None,
    required_evidence: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    permission_justification: str | None = None,
) -> dict[str, Any]:
    permission = {
        "network": network,
        "allowed_read_paths": read_paths or ["."],
        "allowed_write_paths": write_paths,
        "allowed_commands": allowed_commands or [],
        "permission_intents": permission_intents or ["read_repo"],
        "grounded_by": [
            "repo_policy.allowed_read_paths",
            "repo_policy.allowed_write_paths",
            "repo_policy.test_commands",
            "repo_policy.network_allowed_by_default",
        ],
        "justification": permission_justification or "Model-agent node is grounded by repo policy and task-profile permission intent.",
    }
    return {
        "skeleton_node_id": skeleton_node_id,
        "node_id": node_id,
        "phase": phase,
        "goal": goal,
        "executor_kind": executor_kind,
        "command": [],
        "prompt": prompt,
        "model_provider": model_provider,
        "model": model,
        "model_config": merge_model_config_tooling(
            {
                "output_file": f"{node_id}_model_output.json",
                "output_json": True,
            }
        ),
        "required_evidence": required_evidence or ["log", "sandbox_events", "resource_report"],
        "acceptance_criteria": acceptance_criteria or [
            "Worker may only submit results.",
            "Evidence must include the node-specific required evidence, sandbox events, and resource report.",
            "Overlooker acceptance must cite evidence_ref.",
        ],
        "experience_pattern_ids": pattern_ids,
        "instantiation_principles": {
            "stage_id": stage_id,
            "skeleton_node_id": skeleton_node_id,
            "executor_principle": f"{executor_kind} is selected so this agent can run through a provider-agnostic model unit.",
            "prompt_principle": "Prompt scope follows the node ownership boundary and avoids overlap with peer nodes.",
            "permission_principle": permission["justification"],
            "evidence_principle": "Required evidence supports deterministic validators and Overlooker acceptance.",
            "handoff_principle": "The node output is consumed by declared downstream nodes or final review.",
            "decision_basis": _basis(
                f"basis-instantiation-{node_id}",
                [f"workflow_skeleton.nodes[{skeleton_node_id}]", "repo_policy"],
                [executor_kind, "provider-agnostic agent"],
                f"node_instantiations[{node_id}]",
                "Change executor, provider, prompt, evidence contract, or permission grounding.",
            ),
        },
        "permission_grounding": to_plain_dict(permission),
    }
