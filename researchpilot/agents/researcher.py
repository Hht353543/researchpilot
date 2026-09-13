"""ResearchAgent: executes tools per sub-task and turns observations into evidence."""

from __future__ import annotations

from typing import Any

from researchpilot.agents.base import BaseAgent
from researchpilot.llm.prompts import RESEARCHER_SYSTEM, researcher_user
from researchpilot.schemas import Evidence, EvidenceBundle, ResearchPlan, Subtask
from researchpilot.tools.base import ToolContext, ToolRequestContext, ToolResult
from researchpilot.utils import jaccard, overlap_ratio, truncate


class ResearchAgent(BaseAgent):
    name = "ResearchAgent"

    def run(
        self,
        plan: ResearchPlan,
        *,
        subtask_ids: list[str] | None = None,
        follow_up_queries: list[str] | None = None,
        iteration: int = 1,
    ) -> EvidenceBundle:
        runtime = self.runtime
        subtasks = [s for s in plan.subtasks if s.intent != "synthesis"]
        if subtask_ids:
            wanted = set(subtask_ids)
            subtasks = [s for s in subtasks if s.id in wanted]
        if follow_up_queries:
            subtasks = subtasks + [
                Subtask(
                    id=f"F{idx}",
                    question=query,
                    intent="knowledge_search",
                    tools=[t for t in ("knowledge_search", "web_search") if runtime.tools.has(t)],
                    expected_output="补齐缺口所需的证据",
                    priority=4,
                )
                for idx, query in enumerate(follow_up_queries, start=1)
            ]

        bundle = EvidenceBundle(iterations=iteration)
        existing_ids = [e.id for e in runtime.memory.working.evidence]
        next_index = len(existing_ids)
        seen_claims = [e.claim for e in runtime.memory.working.evidence]

        with self.span(input={"iteration": iteration, "subtasks": [s.id for s in subtasks]}) as span:
            for subtask in subtasks:
                ctx = runtime.tool_context
                ctx.scratch["agent"] = self.name
                observations = self._execute_subtask(subtask, ctx)
                for observation in observations:
                    runtime.memory.add_observation(
                        observation["tool"], observation["arguments"], observation["summary"]
                    )
                items = [item for observation in observations for item in observation["items"]]
                bundle.tool_calls += len(observations)
                if not observations:
                    bundle.gaps.append(f"{subtask.id}: 无可用工具或无结果")
                    continue
                extra = self._extract_evidence(
                    subtask,
                    observations,
                    existing_ids + [e.id for e in bundle.evidence],
                    iteration=iteration,
                )
                for evidence in extra:
                    if any(jaccard(claim, evidence.claim) >= 0.8 for claim in seen_claims):
                        continue  # the same fact was already captured (possibly via another tool)
                    seen_claims.append(evidence.claim)
                    next_index += 1
                    bundle.evidence.append(
                        evidence.model_copy(update={"id": f"E{next_index}", "subtask_id": subtask.id})
                    )
                if not items:
                    bundle.gaps.append(f"{subtask.id}: 观测结果为空")
            bundle.sources = list(runtime.sources.all())
            added = runtime.memory.add_evidence(bundle.evidence)
            self.note(
                f"iteration {iteration}: {len(bundle.evidence)} evidence (+{len(added)} new), "
                f"{bundle.tool_calls} tool calls"
            )
            runtime.tracer.end_span(
                span,
                output={
                    "evidence": len(bundle.evidence),
                    "tool_calls": bundle.tool_calls,
                    "gaps": bundle.gaps,
                    "iteration": iteration,
                },
            )
        return bundle

    # -- tool execution ---------------------------------------------------- #
    def _execute_subtask(self, subtask: Subtask, ctx: ToolContext) -> list[dict[str, Any]]:
        runtime = self.runtime
        tools = list(subtask.tools) or (["knowledge_search"] if runtime.tools.has("knowledge_search") else [])
        observations: list[dict[str, Any]] = []
        for tool_name in tools:
            if not runtime.tools.has(tool_name):
                continue
            # Argument policy lives with the tool itself: the agent never needs an
            # if/elif chain over tool names to know how to call them.
            arguments = runtime.tools.get(tool_name).build_arguments(
                ToolRequestContext(
                    question=subtask.question,
                    subtask_id=subtask.id,
                    agent=self.name,
                    top_k=runtime.settings.top_k,
                    doc_id_hint=self._doc_id_for(subtask),
                    settings=runtime.settings,
                )
            )
            if arguments is None:
                continue
            result: ToolResult = runtime.tools.invoke(tool_name, arguments, ctx)
            if not result.ok:
                runtime.errors.append(
                    f"tool {tool_name} failed after {result.attempts} attempt(s): "
                    f"{result.error or 'unknown error'}"
                )
            observations.append(self._observation(tool_name, arguments, result))
            runtime.sources.register_tool_result(result)
        return observations

    def _doc_id_for(self, subtask: Subtask) -> str:
        runtime = self.runtime
        for evidence in runtime.memory.working.evidence:
            if evidence.subtask_id == subtask.id:
                source = runtime.sources.get(evidence.source_id)
                if source is not None and source.doc_id:
                    return source.doc_id
        ranked = [source for source in runtime.sources.all() if source.doc_id]
        if ranked:
            return max(ranked, key=lambda source: source.score).doc_id
        return ""

    @staticmethod
    def _observation(tool_name: str, arguments: dict[str, Any], result: ToolResult) -> dict[str, Any]:
        return {
            "tool": tool_name,
            "arguments": arguments,
            "ok": result.ok,
            "error": result.error,
            "items": [item.as_prompt_dict() for item in result.items],
            "rendered": truncate(result.rendered(), 4000),
            "summary": {
                "ok": result.ok,
                "items": len(result.items),
                "error": result.error,
                "latency_ms": result.latency_ms,
            },
        }

    # -- evidence extraction ----------------------------------------------- #
    def _extract_evidence(
        self,
        subtask: Subtask,
        observations: list[dict[str, Any]],
        existing_ids: list[str],
        *,
        iteration: int,
    ) -> list[Evidence]:
        runtime = self.runtime
        try:
            result = runtime.llm.run(
                EvidenceBundle,
                agent=self.name,
                system=RESEARCHER_SYSTEM,
                user=researcher_user(subtask.question, observations, []),
                hints={
                    "subtask_question": subtask.question,
                    "subtask_id": subtask.id,
                    "observations": observations,
                    "existing_evidence_ids": existing_ids,
                    "iteration": iteration,
                },
                purpose="researcher",
                max_tokens=2000,
            )
            bundle: EvidenceBundle = result.value  # type: ignore[assignment]
            evidence = list(bundle.evidence)
        except Exception as exc:
            runtime.errors.append(f"researcher[{subtask.id}]: {type(exc).__name__}: {exc}")
            evidence = self._extractive_fallback(subtask, observations)
        return self._validate_evidence(evidence, observations)

    def _validate_evidence(
        self, evidence: list[Evidence], observations: list[dict[str, Any]]
    ) -> list[Evidence]:
        """Drop items whose quote cannot be linked back to a real observation."""
        runtime = self.runtime
        valid_items = [item for observation in observations for item in observation["items"]]
        by_id = {item["source_id"]: item for item in valid_items}
        cleaned: list[Evidence] = []
        for item in evidence:
            if not item.quote.strip():
                continue
            if item.source_id not in by_id:
                match = self._match_item(item.quote, valid_items)
                if match is None:
                    runtime.errors.append(
                        f"researcher: dropped evidence with unknown source {item.source_id!r}"
                    )
                    continue
                item = item.model_copy(update={"source_id": match})
            cleaned.append(item)
        return cleaned

    @staticmethod
    def _match_item(quote: str, items: list[dict[str, Any]]) -> str | None:
        best: tuple[float, str] | None = None
        for item in items:
            score = overlap_ratio(quote, str(item.get("content", "")))
            if score > 0.3 and (best is None or score > best[0]):
                best = (score, str(item["source_id"]))
        return best[1] if best else None

    @staticmethod
    def _extractive_fallback(subtask: Subtask, observations: list[dict[str, Any]]) -> list[Evidence]:
        """Deterministic extraction used when structured generation fails."""
        from researchpilot.llm.mock_provider import MockLLMProvider

        bundle = MockLLMProvider()._evidence(
            {
                "subtask_question": subtask.question,
                "subtask_id": subtask.id,
                "observations": observations,
            }
        )
        return list(bundle.evidence)
