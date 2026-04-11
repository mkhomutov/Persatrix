"""Task agent: Planner.

Decomposes high-level goals into actionable step-by-step plans with
dependency ordering and resource estimates.
"""

from .base import BaseAgent, TaskInput, TaskOutput

_PLANNER_INSTRUCTIONS = """\
You are a planning agent. Your job is to decompose high-level goals into
actionable step-by-step plans.

Guidelines:
- Break complex tasks into ordered, concrete steps.
- Identify dependencies between steps.
- Estimate relative effort for each step (small/medium/large).
- Output your plan as structured YAML or JSON with:
  - "steps": list of objects with "id", "description", "depends_on", "effort"
  - "summary": brief description of the overall approach
- Each step should be independently assignable to a task agent.
- Consider error scenarios and include fallback steps where appropriate.
- Do not execute the plan — only produce it.
"""


class PlannerAgent(BaseAgent):
    """Decomposes high-level goals into actionable step-by-step plans."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        system_prompt = f"Role: {self.role}\n\n{_PLANNER_INSTRUCTIONS}"
        return await self._run_llm_loop(task, system_prompt=system_prompt)
