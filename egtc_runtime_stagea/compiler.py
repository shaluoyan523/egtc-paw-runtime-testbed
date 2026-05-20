from __future__ import annotations

from pathlib import PurePosixPath

from .experience import ExperienceLibrary
from .models import CompiledGraphPatch, GraphPatch, GraphPatchOperation, NodeCapsule
from .phaseb_models import (
    CompiledWorkflow,
    CompilerFinding,
    PermissionGroundingReport,
    RepoPolicy,
    SandboxProfile,
    WorkflowBlueprint,
)


class PermissionGrounder:
    def __init__(self, repo_policy: RepoPolicy) -> None:
        self.repo_policy = repo_policy

    def derive(self, node: NodeCapsule, phase: str) -> PermissionGroundingReport:
        if phase == "verification":
            allowed_write_paths = []
            allowed_commands = self.repo_policy.test_commands
            justification = "Verification nodes only need read access and repo-grounded test commands."
        elif phase == "implementation":
            allowed_write_paths = self.repo_policy.allowed_write_paths
            allowed_commands = [node.command] if node.command else []
            justification = "Implementation nodes may write only within repo policy write paths."
        else:
            allowed_write_paths = []
            allowed_commands = [node.command] if node.command else []
            justification = "Diagnosis nodes are read-only by default."
        return PermissionGroundingReport(
            node_id=node.node_id,
            sandbox_profile=SandboxProfile(
                network="none",
                allowed_read_paths=self.repo_policy.allowed_read_paths,
                allowed_write_paths=allowed_write_paths,
                allowed_commands=allowed_commands,
                justification=justification,
            ),
            grounded_by=[
                "repo_policy.allowed_read_paths",
                "repo_policy.allowed_write_paths",
                "repo_policy.test_commands",
                "repo_policy.network_allowed_by_default",
            ],
        )


class WorkflowCompiler:
    STAGE_D_GRAPH_PATCH_OPS = {"retry_node"}
    PHASE_E_GRAPH_PATCH_OPS = {
        "retry_node",
        "replace_worker",
        "split_node",
        "insert_node",
        "add_edge",
        "remove_edge",
        "update_join_policy",
    }
    DEFERRED_GRAPH_PATCH_OPS = {"update_schedule"}
    FORBIDDEN_PATCH_VALUE_KEYS = {
        "allowed_write_paths",
        "capability_tokens",
        "network",
        "permissions",
        "sandbox_profile",
        "secret_refs",
    }
    REQUIRED_DECISION_BASIS_KEYS = {
        "basis_id",
        "source_refs",
        "matched_signals",
        "assumptions",
        "invalidation_signals",
        "confidence",
        "correction_target",
        "correction_action",
    }

    def compile(
        self,
        blueprint: WorkflowBlueprint,
        experience_library: ExperienceLibrary | None = None,
    ) -> CompiledWorkflow:
        findings: list[CompilerFinding] = []
        node_ids = [inst.node.node_id for inst in blueprint.node_instantiations]
        if len(node_ids) != len(set(node_ids)):
            findings.append(
                CompilerFinding("error", "duplicate_node_id", "Node ids must be unique.")
            )

        skeleton_ids = {node.node_id for node in blueprint.workflow_skeleton.nodes}
        instantiated_ids = {inst.skeleton_node_id for inst in blueprint.node_instantiations}
        missing = sorted(skeleton_ids - instantiated_ids)
        for skeleton_node_id in missing:
            findings.append(
                CompilerFinding(
                    "error",
                    "missing_instantiation",
                    "Every skeleton node must have one instantiation.",
                    skeleton_node_id,
                )
            )

        for inst in blueprint.node_instantiations:
            findings.extend(self._check_node(blueprint.repo_policy, inst.node, inst.permission_grounding))

        findings.extend(self._check_experience_usage(blueprint, experience_library))
        findings.extend(self._check_director_deliberation(blueprint))

        accepted = not any(finding.severity == "error" for finding in findings)
        return CompiledWorkflow(
            accepted=accepted,
            blueprint_id=blueprint.blueprint_id,
            executable_nodes=[inst.node for inst in blueprint.node_instantiations] if accepted else [],
            findings=findings,
        )

    def _check_node(
        self,
        repo_policy: RepoPolicy,
        node: NodeCapsule,
        grounding: PermissionGroundingReport,
    ) -> list[CompilerFinding]:
        findings: list[CompilerFinding] = []
        profile = grounding.sandbox_profile
        if profile.network != "none" and not repo_policy.network_allowed_by_default:
            findings.append(
                CompilerFinding(
                    "error",
                    "network_not_grounded",
                    "Network access was requested without repo policy grounding.",
                    node.node_id,
                )
            )
        for path in profile.allowed_write_paths:
            if self._is_sensitive(path, repo_policy.sensitive_paths):
                findings.append(
                    CompilerFinding(
                        "error",
                        "sensitive_write_path",
                        f"Write path overlaps sensitive path: {path}",
                        node.node_id,
                    )
                )
        if node.command and profile.allowed_commands and node.command not in profile.allowed_commands:
            findings.append(
                CompilerFinding(
                    "error",
                    "command_not_allowed",
                    f"Node command is not in allowed_commands: {node.command}",
                    node.node_id,
                )
            )
        if not node.acceptance_criteria:
            findings.append(
                CompilerFinding(
                    "error",
                    "missing_acceptance_criteria",
                    "Node must define Overlooker acceptance criteria.",
                    node.node_id,
                )
            )
        if not grounding.grounded_by:
            findings.append(
                CompilerFinding(
                    "error",
                    "missing_permission_grounding",
                    "PermissionGroundingReport must cite repo policy sources.",
                    node.node_id,
                )
            )
        if node.executor_kind == "model_agent":
            findings.extend(self._check_model_agent_node(node))
        return findings

    def _check_model_agent_node(self, node: NodeCapsule) -> list[CompilerFinding]:
        findings: list[CompilerFinding] = []
        provider = node.model_provider or node.model_config.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            findings.append(
                CompilerFinding(
                    "error",
                    "model_agent_missing_provider",
                    "model_agent nodes must declare model_provider or model_config.provider.",
                    node.node_id,
                )
            )
        if node.command:
            findings.append(
                CompilerFinding(
                    "error",
                    "model_agent_command_not_allowed",
                    "model_agent nodes must not also declare a subprocess command.",
                    node.node_id,
                )
            )
        if not node.prompt:
            findings.append(
                CompilerFinding(
                    "error",
                    "model_agent_missing_prompt",
                    "model_agent nodes must include a prompt.",
                    node.node_id,
                )
            )
        output_file = node.model_config.get("output_file")
        if output_file is not None and (
            not isinstance(output_file, str)
            or not output_file.strip()
            or output_file.startswith("/")
            or ".." in PurePosixPath(output_file).parts
        ):
            findings.append(
                CompilerFinding(
                    "error",
                    "model_agent_invalid_output_file",
                    "model_agent model_config.output_file must be a relative workspace path.",
                    node.node_id,
                )
            )
        tools = node.model_config.get("tools")
        if tools is not None and not isinstance(tools, list):
            findings.append(
                CompilerFinding(
                    "error",
                    "model_agent_invalid_tools",
                    "model_agent model_config.tools must be a list of tool descriptors.",
                    node.node_id,
                )
            )
            tools = []
        mcp_servers = node.model_config.get("mcp_servers")
        if mcp_servers is not None and not isinstance(mcp_servers, list):
            findings.append(
                CompilerFinding(
                    "error",
                    "model_agent_invalid_mcp_servers",
                    "model_agent model_config.mcp_servers must be a list of MCP server descriptors.",
                    node.node_id,
                )
            )
            mcp_servers = []
        tool_server_ids = {
            str(server.get("server_id"))
            for server in (mcp_servers or [])
            if isinstance(server, dict) and server.get("server_id")
        }
        known_tool_ids: set[str] = set()
        for index, tool in enumerate(tools or []):
            if not isinstance(tool, dict):
                findings.append(
                    CompilerFinding(
                        "error",
                        "model_agent_invalid_tool_descriptor",
                        f"model_config.tools[{index}] must be an object.",
                        node.node_id,
                    )
                )
                continue
            tool_id = tool.get("tool_id")
            if not isinstance(tool_id, str) or not tool_id.strip():
                findings.append(
                    CompilerFinding(
                        "error",
                        "model_agent_tool_missing_id",
                        f"model_config.tools[{index}] must declare tool_id.",
                        node.node_id,
                    )
                )
            else:
                known_tool_ids.add(tool_id)
            if tool.get("mcp_server_id") and str(tool.get("mcp_server_id")) not in tool_server_ids:
                findings.append(
                    CompilerFinding(
                        "error",
                        "model_agent_tool_unknown_mcp_server",
                        f"Tool {tool_id!r} references unknown mcp_server_id {tool.get('mcp_server_id')!r}.",
                        node.node_id,
                    )
                )
            if bool(tool.get("requires_network")) and self._node_network_mode(node) == "none":
                findings.append(
                    CompilerFinding(
                        "warning",
                        "model_agent_tool_requires_network",
                        f"Tool {tool_id!r} requires network but node sandbox_profile.network is none; Director may plan it but runtime must not execute it without permission grounding.",
                        node.node_id,
                    )
                )
        allowed_mcp_tools = node.model_config.get("allowed_mcp_tools")
        if allowed_mcp_tools is not None and not isinstance(allowed_mcp_tools, list):
            findings.append(
                CompilerFinding(
                    "error",
                    "model_agent_invalid_allowed_mcp_tools",
                    "model_agent model_config.allowed_mcp_tools must be a list when present.",
                    node.node_id,
                )
            )
            allowed_mcp_tools = []
        for tool_id in allowed_mcp_tools or []:
            if not isinstance(tool_id, str) or tool_id not in known_tool_ids:
                findings.append(
                    CompilerFinding(
                        "error",
                        "model_agent_allowed_mcp_tool_unknown",
                        f"allowed_mcp_tools references unknown tool id {tool_id!r}.",
                        node.node_id,
                    )
                )
        tool_env = node.model_config.get("tool_env")
        if tool_env is not None and not isinstance(tool_env, dict):
            findings.append(
                CompilerFinding(
                    "error",
                    "model_agent_invalid_tool_env",
                    "model_agent model_config.tool_env must be an object when present.",
                    node.node_id,
                )
            )
        for index, server in enumerate(mcp_servers or []):
            if not isinstance(server, dict):
                findings.append(
                    CompilerFinding(
                        "error",
                        "model_agent_invalid_mcp_descriptor",
                        f"model_config.mcp_servers[{index}] must be an object.",
                        node.node_id,
                    )
                )
                continue
            server_id = server.get("server_id")
            if not isinstance(server_id, str) or not server_id.strip():
                findings.append(
                    CompilerFinding(
                        "error",
                        "model_agent_mcp_missing_id",
                        f"model_config.mcp_servers[{index}] must declare server_id.",
                        node.node_id,
                    )
                )
            if not isinstance(server.get("tools"), list):
                findings.append(
                    CompilerFinding(
                        "error",
                        "model_agent_mcp_missing_tools",
                        f"model_config.mcp_servers[{index}] must declare a tools list.",
                        node.node_id,
                    )
                )
            if bool(server.get("requires_network")) and self._node_network_mode(node) == "none":
                findings.append(
                    CompilerFinding(
                        "warning",
                        "model_agent_mcp_requires_network",
                        f"MCP server {server_id!r} requires network but node sandbox_profile.network is none.",
                        node.node_id,
                    )
                )
        return findings

    def _node_network_mode(self, node: NodeCapsule) -> str:
        profile = node.sandbox_profile
        if isinstance(profile, dict):
            return str(profile.get("network") or "none")
        return "none"

    def _check_director_deliberation(self, blueprint: WorkflowBlueprint) -> list[CompilerFinding]:
        findings: list[CompilerFinding] = []
        if blueprint.director_mode not in {"codex", "model_agent"}:
            return findings
        director_label = "Agent Director"
        skeleton = blueprint.workflow_skeleton
        total_agents = skeleton.agent_allocation.get("total_agents")
        if not isinstance(total_agents, int) or total_agents != len(skeleton.nodes):
            findings.append(
                CompilerFinding(
                    "error",
                    "director_agent_allocation_mismatch",
                    f"{director_label} must make total_agents equal the selected skeleton node count.",
                )
            )
        alternatives = skeleton.alternative_skeletons
        if len(alternatives) < 3:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_alternative_comparison",
                    f"{director_label} must compare at least three candidate skeletons before selecting one.",
                )
            )
        selected_count = sum(
            1 for alternative in alternatives if bool(alternative.get("selected"))
        )
        if selected_count != 1:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_invalid_selected_alternative",
                    f"{director_label} must mark exactly one alternative skeleton as selected.",
                )
            )
        if len(skeleton.deliberation_trace) < 2:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_deliberation_trace",
                    f"{director_label} must provide a deliberation trace comparing evidence and task signals.",
                )
            )
        scaling = skeleton.scaling_policy
        required_scaling_keys = {
            "scale_triggers",
            "max_planned_agents_for_current_task",
            "expansion_strategy",
            "requires_replan_when",
        }
        missing = sorted(required_scaling_keys - set(scaling))
        if missing:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_scaling_policy",
                    f"{director_label} scaling_policy is missing keys: {missing}",
                )
            )
        elif not scaling.get("scale_triggers") or not scaling.get("expansion_strategy"):
            findings.append(
                CompilerFinding(
                    "error",
                    "director_scaling_policy_too_weak",
                    f"{director_label} scaling_policy must include non-empty scale triggers and expansion strategy.",
                )
            )
        if len(skeleton.experience_rationale) < 2:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_experience_rationale",
                    f"{director_label} must explain why selected experience patterns fit the task.",
                )
            )
        findings.extend(self._check_director_skill_usage(blueprint))
        findings.extend(self._check_director_planning_skill(skeleton))
        findings.extend(self._check_director_draft_plan_review(skeleton))
        findings.extend(self._check_node_selection_principles(blueprint))
        return findings

    def _check_director_skill_usage(self, blueprint: WorkflowBlueprint) -> list[CompilerFinding]:
        usage = blueprint.director_skill_usage
        if not usage:
            return [
                CompilerFinding(
                    "error",
                    "director_missing_skill_usage",
                    "Codex Director must report loading the director-deliberative-planning skill.",
                )
            ]
        required = {
            "skill_name",
            "skill_path",
            "schema_path",
            "skill_sha256",
            "schema_sha256",
            "loaded",
            "applied_required_fields",
        }
        findings: list[CompilerFinding] = []
        missing = sorted(required - set(usage))
        if missing:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_skill_usage_missing_keys",
                    f"director_skill_usage is missing keys: {missing}",
                )
            )
        if usage.get("skill_name") != "director-deliberative-planning":
            findings.append(
                CompilerFinding(
                    "error",
                    "director_skill_usage_wrong_skill",
                    "Codex Director must use director-deliberative-planning.",
                )
            )
        if usage.get("loaded") is not True:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_skill_not_loaded",
                    "Codex Director must mark director_skill_usage.loaded=true.",
                )
            )
        required_fields = {
            "linear_requirement_flow",
            "stage_structure_decisions",
            "research_route_decisions",
            "per_stage_agent_allocation",
            "plan_derivation_trace",
            "node_selection_principles",
            "instantiation_principles",
            "draft_plan_review",
            "decision_basis",
        }
        applied = usage.get("applied_required_fields")
        if not isinstance(applied, list) or not required_fields.issubset(set(applied)):
            findings.append(
                CompilerFinding(
                    "error",
                    "director_skill_usage_missing_applied_fields",
                    "director_skill_usage must list all required planning fields applied from the skill.",
                )
            )
        for key in ["skill_sha256", "schema_sha256", "skill_path", "schema_path"]:
            value = usage.get(key)
            if not isinstance(value, str) or not value.strip():
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_skill_usage_empty_value",
                        f"director_skill_usage.{key} must be a non-empty string.",
                    )
                )
        return findings

    def _check_director_planning_skill(self, skeleton) -> list[CompilerFinding]:
        findings: list[CompilerFinding] = []
        flow = skeleton.linear_requirement_flow
        structure_decisions = skeleton.stage_structure_decisions
        research_decisions = skeleton.research_route_decisions
        allocations = skeleton.per_stage_agent_allocation
        derivation = skeleton.plan_derivation_trace
        if not flow:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_linear_requirement_flow",
                    "Codex Director must first decompose the objective into a linear requirement flow.",
                )
            )
        if not structure_decisions:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_stage_structure_decisions",
                    "Codex Director must choose a structure for each linear stage before final nodes.",
                )
            )
        if not research_decisions:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_research_route_decisions",
                    "Codex Director must decide whether each stage needs research or local expert-route discovery.",
                )
            )
        if not allocations:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_per_stage_agent_allocation",
                    "Codex Director must allocate agents per stage after structure selection.",
                )
            )
        if not derivation:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_missing_plan_derivation_trace",
                    "Codex Director must trace final nodes back to stage decisions.",
                )
            )
        if not flow or not structure_decisions or not research_decisions or not allocations:
            return findings

        stage_ids = {
            str(stage.get("stage_id"))
            for stage in flow
            if isinstance(stage, dict) and stage.get("stage_id")
        }
        if len(stage_ids) != len(flow):
            findings.append(
                CompilerFinding(
                    "error",
                    "director_invalid_linear_requirement_flow",
                    "Every linear_requirement_flow item must have a unique stage_id.",
                )
            )
        for stage in flow:
            if isinstance(stage, dict):
                findings.extend(
                    self._check_decision_basis(
                        stage,
                        f"linear_requirement_flow[{stage.get('stage_id', '?')}]",
                    )
                )
        for collection_name, collection in [
            ("stage_structure_decisions", structure_decisions),
            ("research_route_decisions", research_decisions),
            ("per_stage_agent_allocation", allocations),
        ]:
            decision_stage_ids = {
                str(item.get("stage_id"))
                for item in collection
                if isinstance(item, dict) and item.get("stage_id")
            }
            missing = sorted(stage_ids - decision_stage_ids)
            if missing:
                findings.append(
                    CompilerFinding(
                        "error",
                        f"director_stage_decisions_missing_{collection_name}",
                        f"{collection_name} is missing stages: {missing}",
                    )
                )
        for decision in structure_decisions:
            if not isinstance(decision, dict):
                continue
            findings.extend(
                self._check_decision_basis(
                    decision,
                    f"stage_structure_decisions[{decision.get('stage_id', '?')}]",
                )
            )
            if not decision.get("selected_structure"):
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_stage_structure_missing_selection",
                        "Every stage structure decision must include selected_structure.",
                    )
                )
            if not decision.get("anti_signals"):
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_stage_structure_missing_anti_signals",
                        "Every stage structure decision must include anti_signals.",
                    )
                )
        for decision in research_decisions:
            if isinstance(decision, dict):
                findings.extend(
                    self._check_decision_basis(
                        decision,
                        f"research_route_decisions[{decision.get('stage_id', '?')}]",
                    )
                )
        total_stage_agents = 0
        for allocation in allocations:
            if not isinstance(allocation, dict):
                continue
            findings.extend(
                self._check_decision_basis(
                    allocation,
                    f"per_stage_agent_allocation[{allocation.get('stage_id', '?')}]",
                )
            )
            count = allocation.get("agent_count")
            if not isinstance(count, int) or count < 0:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_invalid_stage_agent_count",
                        "Every per-stage allocation must include a non-negative integer agent_count.",
                    )
                )
                continue
            total_stage_agents += count
            agents = allocation.get("agents")
            if not isinstance(agents, list) or len(agents) != count:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_stage_agent_list_mismatch",
                        "Each per-stage allocation must include one agent record per agent_count.",
                    )
                )
            if isinstance(agents, list):
                for agent in agents:
                    if isinstance(agent, dict):
                        findings.extend(
                            self._check_decision_basis(
                                agent,
                                (
                                    "per_stage_agent_allocation"
                                    f"[{allocation.get('stage_id', '?')}].agents"
                                    f"[{agent.get('role', '?')}]"
                                ),
                            )
                        )
            if not allocation.get("count_reason"):
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_stage_agent_count_missing_reason",
                        "Each per-stage allocation must explain why that many agents are needed.",
                    )
                )
        total_agents = skeleton.agent_allocation.get("total_agents")
        if isinstance(total_agents, int) and total_stage_agents != total_agents:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_stage_agent_allocation_mismatch",
                    "The sum of per-stage agent counts must equal agent_allocation.total_agents.",
                )
            )
        node_ids = {node.node_id for node in skeleton.nodes}
        missing_from_trace = [
            node_id
            for node_id in sorted(node_ids)
            if not any(node_id in item for item in derivation)
        ]
        if missing_from_trace:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_plan_trace_missing_nodes",
                    f"plan_derivation_trace must mention every final node id: {missing_from_trace}",
                )
            )
        return findings

    def _check_director_draft_plan_review(self, skeleton) -> list[CompilerFinding]:
        review = skeleton.draft_plan_review
        if not isinstance(review, dict) or not review:
            return [
                CompilerFinding(
                    "error",
                    "director_missing_draft_plan_review",
                    "Codex Director must feed the draft plan back into itself and emit draft_plan_review.",
                )
            ]
        findings: list[CompilerFinding] = []
        required = {
            "review_id",
            "reviewed_draft_fields",
            "structural_verdict",
            "structure_findings",
            "missing_capabilities",
            "recommended_changes",
            "applied_changes",
            "rejected_changes",
            "final_structure_summary",
        }
        missing = sorted(required - set(review))
        if missing:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_draft_plan_review_missing_keys",
                    f"draft_plan_review is missing keys: {missing}",
                )
            )
        reviewed_fields = review.get("reviewed_draft_fields")
        required_reviewed_fields = {
            "linear_requirement_flow",
            "stage_structure_decisions",
            "research_route_decisions",
            "per_stage_agent_allocation",
            "nodes",
            "edges",
            "node_instantiations",
            "experience_pattern_ids",
        }
        if not isinstance(reviewed_fields, list) or not required_reviewed_fields.issubset(
            {str(item) for item in reviewed_fields}
        ):
            findings.append(
                CompilerFinding(
                    "error",
                    "director_draft_plan_review_incomplete_scope",
                    "draft_plan_review.reviewed_draft_fields must cover planning fields, nodes, edges, instantiations, and experience ids.",
                )
            )
        verdict = review.get("structural_verdict")
        if verdict not in {"pass", "revise_before_final", "needs_human_review"}:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_draft_plan_review_invalid_verdict",
                    "draft_plan_review.structural_verdict must be pass, revise_before_final, or needs_human_review.",
                )
            )
        structure_findings = review.get("structure_findings")
        if not isinstance(structure_findings, list) or not structure_findings:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_draft_plan_review_missing_findings",
                    "draft_plan_review.structure_findings must contain at least one finding.",
                )
            )
        else:
            for index, finding in enumerate(structure_findings):
                if not isinstance(finding, dict):
                    findings.append(
                        CompilerFinding(
                            "error",
                            "director_draft_plan_review_invalid_finding",
                            "Each draft_plan_review.structure_findings item must be an object.",
                        )
                    )
                    continue
                for key in ["finding_id", "severity", "target", "finding", "recommendation"]:
                    value = finding.get(key)
                    if not isinstance(value, str) or not value.strip():
                        findings.append(
                            CompilerFinding(
                                "error",
                                "director_draft_plan_review_finding_missing_field",
                                f"draft_plan_review.structure_findings[{index}].{key} must be non-empty.",
                            )
                        )
                if finding.get("severity") not in {"info", "warning", "error"}:
                    findings.append(
                        CompilerFinding(
                            "error",
                            "director_draft_plan_review_invalid_finding_severity",
                            "draft_plan_review finding severity must be info, warning, or error.",
                        )
                    )
                findings.extend(
                    self._check_decision_basis(
                        finding,
                        f"draft_plan_review.structure_findings[{index}]",
                    )
                )
        for key, code in [
            ("missing_capabilities", "director_draft_plan_review_missing_capabilities"),
            ("recommended_changes", "director_draft_plan_review_missing_recommended_changes"),
            ("applied_changes", "director_draft_plan_review_missing_applied_changes"),
        ]:
            value = review.get(key)
            if not isinstance(value, list) or not value:
                findings.append(
                    CompilerFinding(
                        "error",
                        code,
                        f"draft_plan_review.{key} must be a non-empty list.",
                    )
                )
        rejected = review.get("rejected_changes")
        if not isinstance(rejected, list):
            findings.append(
                CompilerFinding(
                    "error",
                    "director_draft_plan_review_invalid_rejected_changes",
                    "draft_plan_review.rejected_changes must be a list, even when empty.",
                )
            )
        for change_key in ["recommended_changes", "applied_changes"]:
            changes = review.get(change_key)
            if not isinstance(changes, list):
                continue
            for index, change in enumerate(changes):
                if not isinstance(change, dict):
                    findings.append(
                        CompilerFinding(
                            "error",
                            "director_draft_plan_review_invalid_change",
                            f"draft_plan_review.{change_key}[{index}] must be an object.",
                        )
                    )
                    continue
                if not isinstance(change.get("change_id"), str) or not change.get("change_id", "").strip():
                    findings.append(
                        CompilerFinding(
                            "error",
                            "director_draft_plan_review_change_missing_id",
                            f"draft_plan_review.{change_key}[{index}] must include change_id.",
                        )
                    )
        summary = review.get("final_structure_summary")
        if not isinstance(summary, str) or not summary.strip():
            findings.append(
                CompilerFinding(
                    "error",
                    "director_draft_plan_review_missing_summary",
                    "draft_plan_review.final_structure_summary must explain why the final graph is sound.",
                )
            )
        return findings

    def _check_node_selection_principles(
        self,
        blueprint: WorkflowBlueprint,
    ) -> list[CompilerFinding]:
        findings: list[CompilerFinding] = []
        skeleton_nodes = {
            node.node_id: node
            for node in blueprint.workflow_skeleton.nodes
        }
        required_node_keys = {
            "stage_id",
            "selected_for",
            "role_principle",
            "dependency_principle",
            "parallelism_principle",
            "evidence_principle",
            "decision_basis",
        }
        for node in blueprint.workflow_skeleton.nodes:
            principles = node.node_selection_principles
            if not principles:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_node_missing_selection_principles",
                        "Every final skeleton node must explain why it was selected.",
                        node.node_id,
                    )
                )
                continue
            missing = sorted(required_node_keys - set(principles))
            if missing:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_node_selection_principles_missing_keys",
                        f"node_selection_principles is missing keys: {missing}",
                        node.node_id,
                    )
                )
            for key in [
                "role_principle",
                "dependency_principle",
                "parallelism_principle",
                "evidence_principle",
            ]:
                if not isinstance(principles.get(key), str) or not principles.get(key, "").strip():
                    findings.append(
                        CompilerFinding(
                            "error",
                            "director_node_selection_principle_empty",
                            f"node_selection_principles.{key} must be non-empty.",
                            node.node_id,
                        )
                    )
            selected_for = principles.get("selected_for")
            if not isinstance(selected_for, list) or not selected_for:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_node_selection_missing_reason",
                        "node_selection_principles.selected_for must explain why this node exists.",
                        node.node_id,
                    )
                )
            findings.extend(
                self._check_decision_basis(
                    principles,
                    f"workflow_skeleton.nodes[{node.node_id}].node_selection_principles",
                    node.node_id,
                )
            )

        required_instantiation_keys = {
            "stage_id",
            "skeleton_node_id",
            "executor_principle",
            "prompt_principle",
            "permission_principle",
            "evidence_principle",
            "handoff_principle",
            "decision_basis",
        }
        for inst in blueprint.node_instantiations:
            principles = inst.instantiation_principles
            if inst.skeleton_node_id not in skeleton_nodes:
                continue
            if not principles:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_node_missing_instantiation_principles",
                        "Every node instantiation must explain why this execution form was selected.",
                        inst.node.node_id,
                    )
                )
                continue
            missing = sorted(required_instantiation_keys - set(principles))
            if missing:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_node_instantiation_principles_missing_keys",
                        f"instantiation_principles is missing keys: {missing}",
                        inst.node.node_id,
                    )
                )
            for key in [
                "executor_principle",
                "prompt_principle",
                "permission_principle",
                "evidence_principle",
                "handoff_principle",
            ]:
                if not isinstance(principles.get(key), str) or not principles.get(key, "").strip():
                    findings.append(
                        CompilerFinding(
                            "error",
                            "director_node_instantiation_principle_empty",
                            f"instantiation_principles.{key} must be non-empty.",
                            inst.node.node_id,
                        )
                    )
            if principles.get("skeleton_node_id") != inst.skeleton_node_id:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_node_instantiation_principle_mismatch",
                        "instantiation_principles.skeleton_node_id must match the instantiated skeleton node.",
                        inst.node.node_id,
                    )
                )
            findings.extend(
                self._check_decision_basis(
                    principles,
                    f"node_instantiations[{inst.node.node_id}].instantiation_principles",
                    inst.node.node_id,
                )
            )
        return findings

    def _check_decision_basis(
        self,
        record: dict[str, object],
        location: str,
        node_id: str | None = None,
    ) -> list[CompilerFinding]:
        basis = record.get("decision_basis")
        if not isinstance(basis, dict):
            return [
                CompilerFinding(
                    "error",
                    "director_missing_decision_basis",
                    f"{location} must include decision_basis for dynamic replanning.",
                    node_id,
                )
            ]
        findings: list[CompilerFinding] = []
        missing = sorted(self.REQUIRED_DECISION_BASIS_KEYS - set(basis))
        if missing:
            findings.append(
                CompilerFinding(
                    "error",
                    "director_decision_basis_missing_keys",
                    f"{location}.decision_basis is missing keys: {missing}",
                )
            )
        for key in [
            "source_refs",
            "matched_signals",
            "assumptions",
            "invalidation_signals",
        ]:
            value = basis.get(key)
            if not isinstance(value, list) or not value:
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_decision_basis_empty_evidence",
                        f"{location}.decision_basis.{key} must be a non-empty list.",
                    )
                )
        for key in ["basis_id", "confidence", "correction_target", "correction_action"]:
            value = basis.get(key)
            if not isinstance(value, str) or not value.strip():
                findings.append(
                    CompilerFinding(
                        "error",
                        "director_decision_basis_empty_correction",
                        f"{location}.decision_basis.{key} must be a non-empty string.",
                    )
                )
        return findings

    def _check_experience_usage(
        self,
        blueprint: WorkflowBlueprint,
        experience_library: ExperienceLibrary | None,
    ) -> list[CompilerFinding]:
        findings: list[CompilerFinding] = []
        blueprint_pattern_ids = set(blueprint.experience_pattern_ids)
        skeleton_pattern_ids = set(blueprint.workflow_skeleton.experience_pattern_ids)
        if not skeleton_pattern_ids.issubset(blueprint_pattern_ids):
            findings.append(
                CompilerFinding(
                    "error",
                    "experience_skeleton_not_declared",
                    "WorkflowSkeleton references experience patterns not declared by the blueprint.",
                )
            )
        node_pattern_ids: set[str] = set()
        for inst in blueprint.node_instantiations:
            node_pattern_ids.update(inst.node.experience_pattern_ids)
            missing = sorted(set(inst.node.experience_pattern_ids) - blueprint_pattern_ids)
            if missing:
                findings.append(
                    CompilerFinding(
                        "error",
                        "experience_node_not_declared",
                        f"Node references experience patterns not declared by the blueprint: {missing}",
                        inst.node.node_id,
                    )
                )
        if not blueprint_pattern_ids and not node_pattern_ids:
            return findings
        if experience_library is None:
            findings.append(
                CompilerFinding(
                    "warning",
                    "experience_library_not_provided",
                    "Experience pattern references were not checked against an active library.",
                )
            )
            return findings
        active = {pattern.pattern_id: pattern for pattern in experience_library.load_patterns()}
        for pattern_id in sorted(blueprint_pattern_ids | node_pattern_ids):
            pattern = active.get(pattern_id)
            if pattern is None:
                findings.append(
                    CompilerFinding(
                        "error",
                        "unknown_experience_pattern",
                        f"Experience pattern is not active in the library: {pattern_id}",
                    )
                )
                continue
            forbidden = self._forbidden_experience_keys(pattern.recommended_structure)
            if forbidden:
                findings.append(
                    CompilerFinding(
                        "error",
                        "experience_attempts_permission_change",
                        (
                            "Experience patterns may shape workflow structure but cannot "
                            f"request permissions or sandbox changes: {forbidden}"
                        ),
                    )
                )
        return findings

    def _forbidden_experience_keys(self, value: object) -> list[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in self.FORBIDDEN_PATCH_VALUE_KEYS:
                    found.add(key)
                found.update(self._forbidden_experience_keys(nested))
        elif isinstance(value, list):
            for item in value:
                found.update(self._forbidden_experience_keys(item))
        return sorted(found)

    def validate_patch(
        self,
        patch: GraphPatch,
        known_node_ids: set[str],
        graph_id: str | None = None,
        phase: str = "D",
    ) -> CompiledGraphPatch:
        findings: list[dict[str, object]] = []
        phase_name = phase.upper()
        simulated_node_ids = set(known_node_ids)
        if not patch.patch_id:
            findings.append(
                self._patch_finding(
                    "error",
                    "missing_patch_id",
                    "GraphPatch must have a patch_id.",
                )
            )
        if graph_id is not None and patch.graph_id != graph_id:
            findings.append(
                self._patch_finding(
                    "error",
                    "graph_id_mismatch",
                    f"GraphPatch targets {patch.graph_id!r}, expected {graph_id!r}.",
                    patch.triggering_node_id,
                )
            )
        if not patch.director_id:
            findings.append(
                self._patch_finding(
                    "error",
                    "missing_director_id",
                    "GraphPatch must cite a Director actor.",
                )
            )
        if patch.triggering_node_id not in known_node_ids:
            findings.append(
                self._patch_finding(
                    "error",
                    "unknown_triggering_node",
                    f"Triggering node is not in graph: {patch.triggering_node_id}",
                    patch.triggering_node_id,
                )
            )
        if not patch.operations:
            findings.append(
                self._patch_finding(
                    "error",
                    "empty_patch",
                    "GraphPatch must contain at least one operation.",
                )
            )
        for operation in patch.operations:
            op_findings = self._check_patch_operation(
                operation,
                patch,
                simulated_node_ids,
                phase,
            )
            findings.extend(op_findings)
            if phase_name != "D" and not any(
                finding["severity"] == "error" for finding in op_findings
            ):
                if operation.op == "insert_node":
                    new_node = operation.value.get("node")
                    if isinstance(new_node, dict):
                        new_node_id = str(new_node.get("node_id") or "")
                        if new_node_id:
                            simulated_node_ids.add(new_node_id)
        if phase_name != "D":
            findings.extend(self._check_phase_e_reentry(patch, known_node_ids))
        return CompiledGraphPatch(
            accepted=not any(finding["severity"] == "error" for finding in findings),
            patch_id=patch.patch_id,
            graph_id=patch.graph_id,
            operations=patch.operations,
            findings=findings,
        )

    def _check_phase_e_reentry(
        self,
        patch: GraphPatch,
        known_node_ids: set[str],
    ) -> list[dict[str, object]]:
        if patch.triggering_event != "overlooker_rejected_node":
            return []
        triggering = patch.triggering_node_id
        if not triggering:
            return []
        inserted_ids: set[str] = set()
        for operation in patch.operations:
            if operation.op == "insert_node":
                raw_node = operation.value.get("node")
                if isinstance(raw_node, dict) and raw_node.get("node_id"):
                    inserted_ids.add(str(raw_node["node_id"]))
            elif operation.op == "split_node":
                raw_nodes = operation.value.get("nodes")
                if isinstance(raw_nodes, list):
                    for raw_node in raw_nodes:
                        if isinstance(raw_node, dict) and raw_node.get("node_id"):
                            inserted_ids.add(str(raw_node["node_id"]))
        reenters = any(
            (
                operation.op in {"retry_node", "replace_worker"}
                and (operation.node_id or operation.target_node_id) == triggering
            )
            or (
                operation.op == "add_edge"
                and operation.target_node_id == triggering
                and (
                    operation.source_node_id in inserted_ids
                    or operation.source_node_id in known_node_ids
                )
            )
            for operation in patch.operations
        )
        if reenters:
            return []
        return [
            self._patch_finding(
                "error",
                "phase_e_patch_missing_reentry",
                (
                    "Phase E patches for rejected nodes must retry/replace the triggering "
                    "node or add a dependency edge that reconnects new work to it."
                ),
                triggering,
            )
        ]

    def _check_patch_operation(
        self,
        operation: GraphPatchOperation,
        patch: GraphPatch,
        known_node_ids: set[str],
        phase: str,
    ) -> list[dict[str, object]]:
        findings: list[dict[str, object]] = []
        allowed_ops = self.STAGE_D_GRAPH_PATCH_OPS if phase == "D" else self.PHASE_E_GRAPH_PATCH_OPS
        if operation.op in self.DEFERRED_GRAPH_PATCH_OPS:
            findings.append(
                self._patch_finding(
                    "error",
                    "graph_patch_op_deferred",
                    f"GraphPatch op {operation.op!r} is deferred to a later stage.",
                    operation.node_id,
                )
            )
            return findings
        if operation.op not in allowed_ops:
            findings.append(
                self._patch_finding(
                    "error",
                    "unknown_graph_patch_op",
                    f"Unsupported GraphPatch op: {operation.op}",
                    operation.node_id,
                )
            )
            return findings

        target_node_id = operation.node_id or operation.target_node_id
        if operation.op == "retry_node":
            if target_node_id not in known_node_ids:
                findings.append(
                    self._patch_finding(
                        "error",
                        "unknown_retry_node",
                        f"retry_node target is not in graph: {target_node_id}",
                        target_node_id,
                    )
                )
            if phase == "D" and target_node_id != patch.triggering_node_id:
                findings.append(
                    self._patch_finding(
                        "error",
                        "retry_must_target_triggering_node",
                        "Stage D retry_node patches may only retry the node that triggered the patch.",
                        target_node_id,
                    )
                )
            if not operation.rationale:
                findings.append(
                    self._patch_finding(
                        "warning",
                        "missing_operation_rationale",
                        "GraphPatch operation should include a rationale.",
                        target_node_id,
                    )
                )
        elif operation.op == "insert_node":
            new_node = operation.value.get("node")
            if not isinstance(new_node, dict):
                findings.append(
                    self._patch_finding(
                        "error",
                        "insert_node_missing_node_payload",
                        "insert_node requires value.node payload.",
                        target_node_id,
                    )
                )
            else:
                new_node_id = str(new_node.get("node_id") or "")
                if not new_node_id:
                    findings.append(
                        self._patch_finding(
                            "error",
                            "insert_node_missing_node_id",
                            "insert_node value.node must include node_id.",
                            target_node_id,
                        )
                    )
                if new_node_id in known_node_ids:
                    findings.append(
                        self._patch_finding(
                            "error",
                            "insert_node_duplicate_node_id",
                            f"insert_node target already exists: {new_node_id}",
                            new_node_id,
                        )
                    )
                if not new_node.get("acceptance_criteria"):
                    findings.append(
                        self._patch_finding(
                            "error",
                            "insert_node_missing_acceptance_criteria",
                            "Inserted node must define Overlooker acceptance criteria.",
                            new_node_id or target_node_id,
                        )
                    )
                if not new_node.get("required_evidence"):
                    findings.append(
                        self._patch_finding(
                            "error",
                            "insert_node_missing_required_evidence",
                            "Inserted node must define required_evidence.",
                            new_node_id or target_node_id,
                        )
                    )
        elif operation.op in {"add_edge", "remove_edge"}:
            if operation.source_node_id not in known_node_ids:
                findings.append(
                    self._patch_finding(
                        "error",
                        "unknown_edge_source",
                        f"Edge source is not in graph: {operation.source_node_id}",
                        operation.source_node_id,
                    )
                )
            if operation.target_node_id not in known_node_ids:
                findings.append(
                    self._patch_finding(
                        "error",
                        "unknown_edge_target",
                        f"Edge target is not in graph: {operation.target_node_id}",
                        operation.target_node_id,
                    )
                )
        elif operation.op in {"replace_worker", "split_node", "update_join_policy"}:
            if target_node_id not in known_node_ids:
                findings.append(
                    self._patch_finding(
                        "error",
                        "unknown_graph_patch_node",
                        f"GraphPatch node is not in graph: {target_node_id}",
                        target_node_id,
                    )
                )
            if operation.op == "replace_worker" and "executor_kind" not in operation.value and "prompt" not in operation.value:
                findings.append(
                    self._patch_finding(
                        "error",
                        "replace_worker_missing_change",
                        "replace_worker requires a prompt or executor_kind change.",
                        target_node_id,
                    )
                )
            if operation.op == "split_node" and "nodes" not in operation.value:
                findings.append(
                    self._patch_finding(
                        "error",
                        "split_node_missing_nodes",
                        "split_node requires value.nodes.",
                        target_node_id,
                    )
                )
        forbidden = sorted(self.FORBIDDEN_PATCH_VALUE_KEYS.intersection(operation.value))
        if forbidden:
            code = (
                "patch_requires_permission_review"
                if phase != "D"
                else "patch_attempts_permission_change"
            )
            findings.append(
                self._patch_finding(
                    "error",
                    code,
                    f"GraphPatch cannot change permissions or sandbox policy without permission review: {forbidden}",
                    target_node_id,
                )
            )
        return findings

    def _patch_finding(
        self,
        severity: str,
        code: str,
        message: str,
        node_id: str | None = None,
    ) -> dict[str, object]:
        finding: dict[str, object] = {
            "severity": severity,
            "code": code,
            "message": message,
        }
        if node_id is not None:
            finding["node_id"] = node_id
        return finding

    def _is_sensitive(self, path: str, sensitive_paths: list[str]) -> bool:
        normalized = PurePosixPath(path).as_posix().strip("/")
        if normalized in {"", "."}:
            return False
        return any(
            normalized == sensitive.strip("/")
            or normalized.startswith(f"{sensitive.strip('/')}/")
            for sensitive in sensitive_paths
        )
