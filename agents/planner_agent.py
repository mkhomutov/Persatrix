"""Sample task agent: Planner."""

from .base import BaseAgent, TaskInput, TaskOutput


class PlannerAgent(BaseAgent):
    """Decomposes high-level goals into actionable step-by-step plans."""

    @property
    def capabilities(self) -> list[str]:
        return ["planning", "decomposition", "prioritization"]

    async def handle(self, task: TaskInput) -> TaskOutput:
        # TODO: Build planning system prompt
        # TODO: Call LLM to decompose task
        # TODO: Return structured plan
        raise NotImplementedError
