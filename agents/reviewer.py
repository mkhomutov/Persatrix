"""Task agent: Code Reviewer.

Reviews code for correctness, style, and security. Returns structured
feedback with an approval decision and a list of issues.
"""

from .base import BaseAgent, TaskInput, TaskOutput

_REVIEWER_INSTRUCTIONS = """\
You are a code review agent. Your job is to review code for correctness,
style, and security issues.

Guidelines:
- Examine the code carefully for bugs, logic errors, and edge cases.
- Check for security vulnerabilities (injection, path traversal, etc.).
- Verify code style and consistency with project conventions.
- Use the file_read tool to read source files for context.
- Provide your review as structured output with:
  - "approved": true/false
  - "issues": list of objects with "severity", "file", "line", "description"
  - "summary": brief overall assessment
- Be specific about issues — include file names and line numbers when possible.
- Distinguish between blocking issues and nitpicks.
"""


class ReviewerAgent(BaseAgent):
    """Reviews code for correctness, style, and security."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        system_prompt = f"Role: {self.role}\n\n{_REVIEWER_INSTRUCTIONS}"
        return await self._run_llm_loop(task, system_prompt=system_prompt)
