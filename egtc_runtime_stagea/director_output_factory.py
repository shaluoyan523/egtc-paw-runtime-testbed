from __future__ import annotations

from typing import Any

from .experience import ExperienceMatch
from .models import to_plain_dict
from .phaseb_models import RepoPolicy


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

    nodes = [
        _skeleton_node(
            node_id="explore-context",
            phase="exploration",
            role="explorer",
            goal="Inspect repository touchpoints and summarize likely implementation surfaces.",
            depends_on=[],
            expected_outputs=["touchpoint_map", "analysis_log"],
            stage_id="stage-1",
            reason="Read-only source discovery is separable from tests and writes.",
            refs=["objective", "repo_policy.allowed_read_paths", *pattern_refs[:2]],
            pattern_ids=selected_pattern_ids,
            dependency="No predecessor is needed because this is initial discovery.",
            parallelism="This can run in parallel with test exploration because both are read-only.",
        ),
        _skeleton_node(
            node_id="explore-tests",
            phase="exploration",
            role="explorer",
            goal="Inspect validation commands, expected test evidence, and failure risks.",
            depends_on=[],
            expected_outputs=["test_plan", "risk_notes"],
            stage_id="stage-1",
            reason="Validation discovery has independent ownership from source touchpoint discovery.",
            refs=["repo_policy.test_commands", *pattern_refs[:3]],
            pattern_ids=selected_pattern_ids,
            dependency="No predecessor is needed because validation discovery starts from repo policy.",
            parallelism="This runs in parallel with source exploration and joins before implementation.",
        ),
        _skeleton_node(
            node_id="implement",
            phase="implementation",
            role="worker",
            goal="Apply the minimal code change after exploration outputs are available.",
            depends_on=["explore-context", "explore-tests"],
            expected_outputs=["diff", "worker_log"],
            stage_id="stage-2",
            reason="A single bounded writer avoids conflicting edits until ownership is proven wider.",
            refs=["workflow_skeleton.nodes[explore-context]", "workflow_skeleton.nodes[explore-tests]", *pattern_refs[:3]],
            pattern_ids=selected_pattern_ids,
            dependency="Implementation waits for both exploration artifacts.",
            parallelism="This is serial because write ownership is not yet proven independent.",
        ),
        _skeleton_node(
            node_id="verify",
            phase="verification",
            role="verifier",
            goal="Run read-only verification and prepare validator-ready evidence.",
            depends_on=["implement"],
            expected_outputs=["test_report", "validator_ready_evidence"],
            stage_id="stage-3",
            reason="A terminal verifier checks the implementation handoff before acceptance.",
            refs=["repo_policy.test_commands", "workflow_skeleton.nodes[implement]", *pattern_refs],
            pattern_ids=selected_pattern_ids,
            dependency="Verification depends on implementation output.",
            parallelism="This is a serial terminal review gate.",
        ),
    ]

    instantiations = [
        _instantiation(
            skeleton_node_id="explore-context",
            node_id="model-explore-context",
            phase="exploration",
            goal="Map repo touchpoints without writing files.",
            executor_kind=executor_kind,
            model_provider=model_provider,
            model=model,
            pattern_ids=selected_pattern_ids,
            write_paths=[],
            prompt="Inspect the workspace and produce a concise touchpoint map. Do not edit files.",
            stage_id="stage-1",
        ),
        _instantiation(
            skeleton_node_id="explore-tests",
            node_id="model-explore-tests",
            phase="exploration",
            goal="Map validation commands and risk notes without writing source files.",
            executor_kind=executor_kind,
            model_provider=model_provider,
            model=model,
            pattern_ids=selected_pattern_ids,
            write_paths=[],
            prompt="Inspect available validation commands and produce a test plan. Do not edit source files.",
            stage_id="stage-1",
        ),
        _instantiation(
            skeleton_node_id="implement",
            node_id="model-implement",
            phase="implementation",
            goal="Apply a bounded implementation change after exploration.",
            executor_kind=executor_kind,
            model_provider=model_provider,
            model=model,
            pattern_ids=selected_pattern_ids,
            write_paths=list(repo_policy.allowed_write_paths),
            prompt="Implement the smallest change required by the objective using exploration outputs.",
            stage_id="stage-2",
        ),
        _instantiation(
            skeleton_node_id="verify",
            node_id="model-verify",
            phase="verification",
            goal="Run read-only verification and report evidence.",
            executor_kind=executor_kind,
            model_provider=model_provider,
            model=model,
            pattern_ids=selected_pattern_ids,
            write_paths=[],
            prompt="Run or inspect repo-grounded verification and produce validator-ready evidence.",
            stage_id="stage-3",
        ),
    ]

    return {
        "director_skill_usage": {
            "skill_name": skill_packet["name"],
            "skill_path": skill_packet["skill_path"],
            "schema_path": skill_packet["schema_path"],
            "skill_sha256": skill_packet["skill_sha256"],
            "schema_sha256": skill_packet["schema_sha256"],
            "loaded": True,
            "applied_required_fields": [
                "linear_requirement_flow",
                "stage_structure_decisions",
                "research_route_decisions",
                "per_stage_agent_allocation",
                "plan_derivation_trace",
                "node_selection_principles",
                "instantiation_principles",
                "draft_plan_review",
                "decision_basis",
            ],
        },
        "task_diagnosis": {
            "task_kind": "implementation",
            "risk_level": "medium",
            "requires_code_change": True,
            "requires_tests": True,
            "repo_touchpoints": ["."],
            "unknowns": [
                "Exact changed files remain unknown until exploration completes."
            ],
            "experience_matches": _serialized_matches(seed_matches[:6]),
            "objective": objective,
        },
        "workflow_skeleton": {
            "topology": "parallel_explore_single_writer_verify_model_agents",
            "agent_allocation": {
                "total_agents": len(nodes),
                "roles": {"explorer": 2, "worker": 1, "verifier": 1},
                "allocation_rationale": [
                    "Two read-only exploration surfaces can run independently.",
                    "One writer is enough until exploration proves independent write ownership.",
                    "A separate verifier gives explicit read-only acceptance evidence.",
                ],
                "agent_count_confidence": "medium",
            },
            "alternative_skeletons": [
                {
                    "name": "small_single_agent",
                    "estimated_agents": 1,
                    "stage_mapping": {"all": ["explore", "implement", "verify"]},
                    "strengths": ["lowest overhead"],
                    "weaknesses": ["weak uncertainty coverage", "no independent verification role"],
                    "selected": False,
                    "rejection_reason": "The objective needs explicit multi-agent planning and evidence gates.",
                },
                {
                    "name": "selected_parallel_explore_single_writer_verify",
                    "estimated_agents": 4,
                    "stage_mapping": {
                        "stage-1": ["explore-context", "explore-tests"],
                        "stage-2": ["implement"],
                        "stage-3": ["verify"],
                    },
                    "strengths": ["parallel discovery", "bounded writes", "clear terminal verification"],
                    "weaknesses": ["does not pre-allocate large specialist pools before evidence exists"],
                    "selected": True,
                    "rejection_reason": "",
                },
                {
                    "name": "large_dynamic_hierarchy",
                    "estimated_agents": 12,
                    "stage_mapping": {
                        "stage-1": ["specialist explorers"],
                        "stage-2": ["multiple bounded writers"],
                        "stage-3": ["review and synthesis board"],
                    },
                    "strengths": ["can cover many independent modules"],
                    "weaknesses": ["too heavy before exploration proves that breadth"],
                    "selected": False,
                    "rejection_reason": "Scale triggers are defined, but current evidence does not justify this width.",
                },
            ],
            "scaling_policy": {
                "scale_triggers": [
                    "explorers identify more than three independent write ownership areas",
                    "validation requires multiple incompatible environments",
                    "overlooker reports missing specialist knowledge or repeated same-failure retries",
                    "implementation diff spans unrelated packages or languages",
                ],
                "max_planned_agents_for_current_task": len(nodes),
                "expansion_strategy": [
                    "split exploration by package or domain",
                    "add specialist workers only for disjoint write paths",
                    "add synthesis and integration review nodes before merge",
                    "promote repeated dynamic corrections into experience-library patterns",
                ],
                "requires_replan_when": [
                    "exploration invalidates the single-writer assumption",
                    "verification requires unavailable permissions",
                    "overlooker recommends request_director_replan",
                ],
            },
            "deliberation_trace": [
                "Compared single-agent, selected four-agent, and larger hierarchical candidates.",
                "Selected model_agent units so the runtime is not bound to Codex CLI.",
                "Kept large-scale expansion as trigger-based rather than pre-allocating unused agents.",
            ],
            "linear_requirement_flow": [
                _flow_stage(
                    "stage-1",
                    1,
                    "Explore repo and validation surface",
                    "Separate read-only uncertainty reduction from writing.",
                    ["objective", "repo_policy", "experience_candidates"],
                    ["touchpoint_map", "test_plan"],
                    "medium",
                    ["analysis_log", "test_plan"],
                    ["objective", "repo_policy", *pattern_refs[:2]],
                ),
                _flow_stage(
                    "stage-2",
                    2,
                    "Implement bounded change",
                    "Apply the smallest change after ownership is grounded.",
                    ["touchpoint_map", "test_plan"],
                    ["diff", "worker_log"],
                    "medium",
                    ["diff", "worker_log"],
                    ["linear_requirement_flow[stage-1]", *pattern_refs[:3]],
                ),
                _flow_stage(
                    "stage-3",
                    3,
                    "Verify and prepare acceptance evidence",
                    "Confirm the implementation with read-only evidence before Overlooker acceptance.",
                    ["diff", "test_plan"],
                    ["test_report", "validator_ready_evidence"],
                    "medium",
                    ["test_report", "resource_report"],
                    ["repo_policy.test_commands", *pattern_refs],
                ),
            ],
            "stage_structure_decisions": [
                _structure_decision(
                    "stage-1",
                    "parallel_exploration",
                    "Two independent read-only surfaces are useful before a writer runs.",
                    selected_pattern_ids,
                ),
                _structure_decision(
                    "stage-2",
                    "single_agent",
                    "Write ownership is not yet proven independent, so one bounded worker is safer.",
                    selected_pattern_ids,
                ),
                _structure_decision(
                    "stage-3",
                    "review_gate",
                    "Verification is a terminal gate that must be read-only.",
                    selected_pattern_ids,
                ),
            ],
            "research_route_decisions": [
                _research_decision("stage-1", True, "Local repo and experience search are needed before implementation."),
                _research_decision("stage-2", False, "Implementation should consume exploration evidence rather than external research."),
                _research_decision("stage-3", False, "Verification is grounded by repo policy test commands."),
            ],
            "per_stage_agent_allocation": [
                _allocation(
                    "stage-1",
                    [
                        ("explorer", "Map source touchpoints.", "touchpoint_map", "explore-context"),
                        ("explorer", "Map validation commands.", "test_plan", "explore-tests"),
                    ],
                    "Two explorers cover source and validation surfaces without write contention.",
                ),
                _allocation(
                    "stage-2",
                    [("worker", "Apply bounded implementation.", "diff", "implement")],
                    "One writer is selected until independent write boundaries are proven.",
                ),
                _allocation(
                    "stage-3",
                    [("verifier", "Verify implementation evidence.", "test_report", "verify")],
                    "One verifier is sufficient for the current bounded implementation.",
                ),
            ],
            "plan_derivation_trace": [
                "basis-structure-stage-1: stage-1 selected parallel_exploration, producing nodes explore-context and explore-tests.",
                "basis-allocation-stage-2: stage-2 selected single_agent, producing node implement.",
                "basis-structure-stage-3: stage-3 selected review_gate, producing node verify.",
            ],
            "draft_plan_review": {
                "review_id": "draft-review-model-agent-1",
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
                "structural_verdict": "revise_before_final",
                "structure_findings": [
                    {
                        "finding_id": "draft-finding-1",
                        "severity": "warning",
                        "target": "node_instantiations[*].executor_kind",
                        "finding": "The draft must avoid assuming Codex CLI as the only agent runtime.",
                        "recommendation": "Use model_agent executor units and keep Codex only as an optional provider.",
                        "decision_basis": _basis(
                            "basis-draft-review-1",
                            ["draft_plan.node_instantiations", "objective"],
                            ["replace codex binding", "provider-agnostic agent units"],
                            "node_instantiations[*].executor_kind",
                            "Switch agent nodes to model_agent and attach provider metadata.",
                        ),
                    }
                ],
                "missing_capabilities": ["none"],
                "recommended_changes": [
                    {
                        "change_id": "draft-change-1",
                        "change_type": "change_role",
                        "target": "node_instantiations[*].executor_kind",
                        "rationale": "Agent nodes should run through the model-agent provider interface.",
                    }
                ],
                "applied_changes": [
                    {
                        "change_id": "draft-change-1",
                        "applied": True,
                        "final_targets": ["node_instantiations[*].executor_kind"],
                        "result": "All agent node instantiations use model_agent.",
                    }
                ],
                "rejected_changes": [],
                "final_structure_summary": "The final graph keeps four justified nodes, clear handoffs, read-only verification, and provider-agnostic model-agent execution.",
            },
            "experience_pattern_ids": selected_pattern_ids,
            "experience_rationale": [
                "Selected patterns guide topology, artifact handoff, review, and scale triggers.",
                "Patterns are used only for structure; compiler and permission grounding still enforce execution safety.",
            ],
            "nodes": nodes,
            "edges": [
                ["explore-context", "implement"],
                ["explore-tests", "implement"],
                ["implement", "verify"],
            ],
        },
        "node_instantiations": instantiations,
    }


def _selected_pattern_ids(seed_matches: list[ExperienceMatch]) -> list[str]:
    preferred = [
        "seed-topology-parallel-explore-implement-verify",
        "seed-handoff-artifact-chain",
        "seed-review-verification-aware-planning",
        "seed-role-overlooker-review-rework",
        "seed-scaling-large-dynamic-hierarchy",
    ]
    available = [match.pattern.pattern_id for match in seed_matches]
    selected: list[str] = []
    for pattern_id in preferred + available:
        if pattern_id in available and pattern_id not in selected:
            selected.append(pattern_id)
        if len(selected) >= 4:
            break
    return selected or available[:4]


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
) -> dict[str, Any]:
    permission = {
        "network": "none",
        "allowed_read_paths": ["."],
        "allowed_write_paths": write_paths,
        "allowed_commands": [],
        "grounded_by": [
            "repo_policy.allowed_read_paths",
            "repo_policy.allowed_write_paths",
            "repo_policy.test_commands",
            "repo_policy.network_allowed_by_default",
        ],
        "justification": "Model-agent node is grounded by repo policy; verification and exploration are read-only.",
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
        "model_config": {
            "output_file": f"{node_id}_model_output.json",
            "output_json": True,
        },
        "required_evidence": ["diff", "test", "log", "sandbox_events", "resource_report"],
        "acceptance_criteria": [
            "Worker may only submit results.",
            "Evidence must include log, diff, test, sandbox events, and resource report.",
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
