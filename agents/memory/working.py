"""
Working memory — context window management.

Implements priority-weighted retention and automatic summarization
of low-priority sections when the token budget is exceeded.
"""

import asyncio
import logging
from dataclasses import dataclass

from ..llm_client import LLMClient
from ..prompt_loader import load_snippet

logger = logging.getLogger(__name__)


def estimate_tokens(text: str, *, accurate: bool = False) -> int:
    """Estimate token count for a string.

    Default (``accurate=False``): chars/4, ~85% accurate for English prose.
    When ``accurate=True``, uses tiktoken ``cl100k_base`` encoding if available,
    falling back to chars/4 when tiktoken is not installed.
    """
    if accurate:
        try:
            import tiktoken  # type: ignore[import-untyped]

            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            logger.debug("tiktoken not installed, accurate=True falling back to chars/4")
        except Exception:
            # Defensive: if tiktoken is installed but broken (e.g. corrupted
            # C extension, encoding registry issue), fall back instead of
            # crashing callers — estimate_tokens is a utility, not critical path.
            logger.warning("tiktoken encoding failed, falling back to chars/4", exc_info=True)
    return len(text) // 4


@dataclass
class ContextSection:
    """A named section of the LLM context window."""

    name: str  # e.g. "system", "persona", "memories", "conversation"
    content: str
    priority: int  # higher = kept longer (system=100, persona=90, ...)
    token_count: int
    compressible: bool = True  # False for system prompt


class WorkingMemory:
    """Context window manager with priority-weighted retention.

    Tracks named sections of context, each with a priority and token count.
    When total tokens exceed the budget, lowest-priority compressible
    sections are summarized via LLM.
    """

    def __init__(
        self,
        max_tokens: int = 100_000,
        compression_model: str = "claude-haiku-4-5",
    ) -> None:
        self._max_tokens = max_tokens
        self._compression_model = compression_model
        self._sections: list[ContextSection] = []
        self._compression_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        """No-op for in-memory working memory. Satisfies MemoryLifecycle protocol."""

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def add_section(self, section: ContextSection) -> None:
        """Add or replace a named section.

        If a section with the same ``name`` already exists, it is replaced.
        """
        self._sections = [s for s in self._sections if s.name != section.name]
        self._sections.append(section)

    def remove_section(self, name: str) -> None:
        """Remove a section by name. No-op if not found."""
        self._sections = [s for s in self._sections if s.name != name]

    def get_section(self, name: str) -> ContextSection | None:
        """Return a section by name, or None."""
        for s in self._sections:
            if s.name == name:
                return s
        return None

    def total_tokens(self) -> int:
        """Total estimated tokens across all sections."""
        return sum(s.token_count for s in self._sections)

    def build_context(self) -> list[dict[str, str]]:
        """Return ordered context sections for prompt assembly.

        Each dict has ``{"role": <section_name>, "content": <text>}``.
        Note: ``role`` is the section name (e.g. "system", "persona",
        "memories"), **not** an LLM API role.  Callers must map sections
        to valid LLM message format before passing to a provider.

        Sections are sorted by priority (highest first).  Sections whose
        cumulative token count would exceed the budget are dropped,
        starting from the lowest-priority end.
        """
        sorted_sections = sorted(self._sections, key=lambda s: s.priority, reverse=True)

        # Include as many high-priority sections as fit
        included: list[ContextSection] = []
        running_total = 0
        for section in sorted_sections:
            if running_total + section.token_count <= self._max_tokens:
                included.append(section)
                running_total += section.token_count
            else:
                logger.debug(
                    "Dropping section '%s' (%d tokens) — budget exhausted",
                    section.name,
                    section.token_count,
                )

        logger.debug(
            "Context built: %d/%d tokens, %d/%d sections included",
            running_total,
            self._max_tokens,
            len(included),
            len(self._sections),
        )

        # Return in priority order (highest first)
        return [{"role": s.name, "content": s.content} for s in included]

    def try_start_compression(self, llm_client: LLMClient) -> None:
        """Spawn a background compression task if none is in flight.

        Idempotent — returns immediately if a compression task is already
        running.
        """
        if self._compression_task is None or self._compression_task.done():
            self._compression_task = asyncio.create_task(
                self.compress_if_needed(llm_client)
            )

    async def await_pending_compression(self) -> None:
        """Await the outstanding compression task, if any.

        Used during shutdown to ensure compressed results are not lost.
        """
        if self._compression_task is not None:
            try:
                await self._compression_task
            except Exception:
                logger.warning("Compression task failed during shutdown")

    async def compress_if_needed(self, llm_client: LLMClient) -> None:
        """Summarize lowest-priority compressible sections when over budget.

        Selects compressible sections in ascending priority order and
        summarizes them via LLM until total tokens fit within the budget.
        """
        if self.total_tokens() <= self._max_tokens:
            return

        original_total = self.total_tokens()

        # Sort compressible sections by priority ascending (compress lowest first)
        compressible = sorted(
            [s for s in self._sections if s.compressible],
            key=lambda s: s.priority,
        )

        for section in compressible:
            if self.total_tokens() <= self._max_tokens:
                break

            try:
                response = await llm_client.create_message(
                    model=self._compression_model,
                    messages=[{"role": "user", "content": section.content}],
                    system=load_snippet("working-memory-compressor"),
                    tools=[],
                    max_tokens=max(section.token_count // 2, 64),
                    temperature=0.2,
                )
                summary = response.text
                if summary is None:
                    logger.warning(
                        "Compression of section '%s' returned no text, preserving original",
                        section.name,
                    )
                    continue
                new_token_count = estimate_tokens(summary, accurate=True)
                # Guard against LLM producing a summary that is as long or
                # longer than the original — replacing it would not reduce
                # total tokens and could infinite-loop if callers retry
                # compression (PR #54 review: compression size guard).
                if new_token_count >= section.token_count:
                    logger.warning(
                        "Compression of section '%s' produced %d tokens "
                        "(original %d), skipping replacement",
                        section.name,
                        new_token_count,
                        section.token_count,
                    )
                    continue
                self.add_section(
                    ContextSection(
                        name=section.name,
                        content=summary,
                        priority=section.priority,
                        token_count=new_token_count,
                        compressible=section.compressible,
                    )
                )
                logger.info(
                    "Compressed section '%s': %d → %d tokens",
                    section.name,
                    section.token_count,
                    new_token_count,
                )
            except Exception:
                logger.warning(
                    "Failed to compress section '%s'", section.name, exc_info=True
                )

        # Only log at info level when compression actually reduced tokens;
        # a no-op pass (all sections skipped/non-compressible) would add
        # noise at info level (PR #59 review: guard unchanged state).
        final_total = self.total_tokens()
        if final_total != original_total:
            logger.info(
                "Compression pass: %d → %d total tokens",
                original_total,
                final_total,
            )
        else:
            logger.debug(
                "Compression pass: no change (%d total tokens)",
                original_total,
            )

    async def close(self) -> None:
        """Await outstanding compression and clear sections."""
        await self.await_pending_compression()
        self._sections.clear()
