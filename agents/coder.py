"""Task agent: Code Writer.

Generates clean, tested code from specifications. Uses file_write and
shell_exec tools to create files and run tests.
"""

from .base import BaseAgent, TaskInput, TaskOutput

_CODER_INSTRUCTIONS = """\
You are a code generation agent. Your job is to write clean, well-tested code
based on the specifications you receive.

Guidelines:
- Write idiomatic, production-quality code.
- Include type hints and docstrings where appropriate.
- Create or update unit tests for any code you write.
- Use the file_write tool to create or modify files.
- Use the shell_exec tool to run tests and verify your changes.
- Use the file_read tool to understand existing code before modifying it.
- If a task is ambiguous, make reasonable assumptions and document them.
- Return a concise summary of what you created or changed.
"""


class CoderAgent(BaseAgent):
    """Writes clean, tested code from specifications."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        system_prompt = f"Role: {self.role}\n\n{_CODER_INSTRUCTIONS}"
        return await self._run_llm_loop(task, system_prompt=system_prompt)
