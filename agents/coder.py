"""Sample task agent: Code Writer."""

from .base import BaseAgent, TaskInput, TaskOutput


class CoderAgent(BaseAgent):
    """Writes clean, tested code from specifications."""

    @property
    def capabilities(self) -> list[str]:
        return ["code_generation", "code_review", "unit_testing"]

    async def handle(self, task: TaskInput) -> TaskOutput:
        # TODO: Build system prompt from agent config
        # TODO: Call LLM with task payload + context
        # TODO: Handle tool_use responses (file_write, shell_exec)
        # TODO: Return final output
        raise NotImplementedError
