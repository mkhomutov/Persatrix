"""Sample task agent: Code Reviewer."""

from .base import BaseAgent, TaskInput, TaskOutput


class ReviewerAgent(BaseAgent):
    """Reviews code for correctness, style, and security."""

    @property
    def capabilities(self) -> list[str]:
        return ["code_review", "security_audit"]

    async def handle(self, task: TaskInput) -> TaskOutput:
        # TODO: Build review-focused system prompt
        # TODO: Call LLM with code to review
        # TODO: Return structured review (approved: bool, issues: list)
        raise NotImplementedError
