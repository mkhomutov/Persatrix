"""Data-driven task agent.

A single agent class that reads behavior from YAML ``instructions``
instead of hard-coding a system prompt per agent role.  Replaces the
v0.1 ``CoderAgent``, ``ReviewerAgent``, and ``PlannerAgent``.
"""

from .base import BaseAgent, TaskInput, TaskOutput


class TaskAgent(BaseAgent):
    """Executes tasks using instructions from YAML agent config.

    The system prompt is composed as::

        Role: {config.role}

        {config.instructions}      # may be empty

    This makes new agent roles a config-only change — no Python code needed.
    """

    async def handle(self, task: TaskInput) -> TaskOutput:
        instructions = self.config.get("instructions", "")
        system_prompt = f"Role: {self.role}"
        if instructions:
            system_prompt = f"{system_prompt}\n\n{instructions}"
        return await self._run_llm_loop(task, system_prompt=system_prompt)
