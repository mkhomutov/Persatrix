#!/usr/bin/env python3
"""Check for broken internal documentation links.

Validates that markdown links in docs/ and root-level .md files point to
existing files, and that any ``#anchor`` fragment resolves to a real
heading (or explicit ``<a id=…>``) in the target document.  Anchors are
matched against GitHub's slug algorithm (``github-slugger``), covering both
cross-file (``file.md#anchor``) and in-page (``#anchor``) links.  GitHub
line anchors (``#L42``) and ``#fragment`` on non-markdown targets are left
to the file-existence check.

Usage::

    python scripts/checks/doc_links.py [--verbose]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# Repo root: three levels up from this file (scripts/checks/doc_links.py).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402

# Markdown link pattern: [text](path) or [text](path#anchor)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)#]*)(#[^)]+)?\)")

# Patterns to strip before scanning for links (code blocks and inline code
# can contain bracket/paren sequences that look like markdown links).
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"``[^`]+``|`[^`]+`")

# --- anchor (#fragment) validation -----------------------------------------
# GitHub renders line-number anchors (``#L45``, ``#L45-L67``) on any file,
# including markdown blobs; they are not heading slugs, so they are exempt
# from heading validation.
_LINE_ANCHOR_RE = re.compile(r"^L\d+(-L\d+)?$")

# ATX heading, e.g. ``## Section title`` (setext ``===``/``---`` underlines
# are intentionally ignored — no anchor link in the doc set targets one, and
# a setext detector would misfire on YAML front-matter / thematic breaks).
_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")

# Fenced code-block delimiter (``` or ~~~), possibly indented.
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# Explicit HTML anchors GitHub honours as link targets: <a id="…"> / <a name="…">.
_HTML_ANCHOR_RE = re.compile(r"""<a\s+(?:id|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _github_slug(text: str) -> str:
    """Slugify heading text the way GitHub does (``github-slugger``).

    Lowercase; strip every character that is not a word character
    (letters, digits, underscore), whitespace, or hyphen; then convert
    spaces to hyphens.  Consecutive hyphens are **not** collapsed and
    leading/trailing hyphens are **not** trimmed — both faithfully match
    GitHub (e.g. ``## ⚠️ Cost Warning`` → ``-cost-warning``; a ``·—·``
    em-dash separator yields a doubled ``--``).
    """
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = slug.replace(" ", "-")
    return slug


def _render_heading_text(raw: str) -> str:
    """Reduce a raw markdown heading to the visible text GitHub slugs.

    GitHub derives the anchor from the *rendered* heading, so inline
    markdown contributes only its visible text: links/images collapse to
    their text/alt, and code-span backticks drop while their content
    stays.  Emphasis markers (``*``) need no special handling —
    :func:`_github_slug` strips ``*`` as punctuation, and the doc set has
    no underscore-emphasis headings (underscores are load-bearing in
    identifiers, so they are preserved).
    """
    raw = re.sub(r"[ \t]+#+[ \t]*$", "", raw)             # ATX closing ###
    raw = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", raw)   # image  -> alt text
    raw = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw)     # inline link -> text
    raw = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", raw)    # reference link -> text
    raw = re.sub(r"\[([^\]]+)\]", r"\1", raw)              # shortcut link -> text
    raw = raw.replace("`", "")                             # code-span backticks
    return raw


def _extract_anchors(content: str) -> set[str]:
    """Return every anchor a markdown document exposes to GitHub.

    That is the slug of each ATX heading (outside fenced code blocks, with
    ``github-slugger``'s duplicate ``-1``/``-2`` disambiguation) plus any
    explicit ``<a id=…>`` / ``<a name=…>`` HTML anchors.
    """
    anchors: set[str] = set()
    # Mirrors github-slugger's occurrence table: every returned slug is a
    # key; the counter lives under the *base* slug and increments until a
    # free ``base-N`` is found.
    occurrences: dict[str, int] = {}

    def _add_heading(text: str) -> None:
        base = _github_slug(_render_heading_text(text))
        slug = base
        while slug in occurrences:
            occurrences[base] += 1
            slug = f"{base}-{occurrences[base]}"
        occurrences[slug] = 0
        anchors.add(slug)

    in_fence = False
    fence_marker = ""
    for line in content.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif line.strip().startswith(fence_marker):
                in_fence, fence_marker = False, ""
            continue
        if in_fence:
            continue
        heading = _ATX_HEADING_RE.match(line)
        if heading:
            _add_heading(heading.group(2))

    for match in _HTML_ANCHOR_RE.finditer(content):
        anchors.add(match.group(1))

    return anchors


def _strip_inline_code_outside_links(text: str) -> str:
    """Remove backtick spans that are not part of markdown link text.

    Inline code like ``[a-z0-9]`` looks like a markdown link after backtick
    stripping.  This function removes backtick content only when it appears
    *outside* of ``[text](url)`` link syntax, so real link text is preserved.
    """
    link_positions: set[int] = set()
    for lm in _LINK_RE.finditer(text):
        for pos in range(lm.start(), lm.end()):
            link_positions.add(pos)
    result: list[str] = []
    last = 0
    for cm in _INLINE_CODE_RE.finditer(text):
        if cm.start() in link_positions:
            continue  # inside a link — keep it
        result.append(text[last:cm.start()])
        last = cm.end()
    result.append(text[last:])
    return "".join(result)


class BrokenLink(NamedTuple):
    file: str
    link: str
    target: str
    reason: str


def _collect_md_files(repo_root: Path) -> list[Path]:
    """Return every tracked markdown file under ``repo_root``.

    ISSUE-0036: the source of truth is ``git ls-files '*.md'``, so the
    set automatically excludes (a) untracked working-tree artifacts
    (e.g. ``PR_BODY.md`` left behind by ``git stash``), (b) files under
    ``.git/``, and (c) gitignored paths — without ad-hoc filter chains.
    Crucially, it also catches markdown files at depth ≥ 3 outside
    ``docs/`` (e.g. ``.github/instructions/*.md``,
    ``prompts/runtime/safety/*.md``) which the previous glob shape
    silently dropped from the link scan.

    ``docs/pr-reviews/`` stays excluded by project convention (see
    ``.github/copilot-instructions.md``) — PR review reports are
    local-only artifacts, gitignored anyway, and their links resolve
    against repo-root paths rather than ``docs/pr-reviews/``.

    Falls back to a glob-and-filter walk when ``repo_root`` is not a
    git checkout (downstream tarball consumers, extracted-source
    builds). The fallback prints a one-line WARN so the divergence is
    visible.
    """
    pr_reviews_dir = (repo_root / "docs" / "pr-reviews").resolve()
    pr_reviews_prefix = os.path.normcase(str(pr_reviews_dir)) + os.sep

    tracked = _git_ls_md_files(repo_root)
    if tracked is None:
        files = _glob_md_files_fallback(repo_root)
    else:
        files = sorted(tracked)

    return [
        f for f in files
        if not os.path.normcase(str(f.resolve())).startswith(pr_reviews_prefix)
    ]


def _git_ls_md_files(repo_root: Path) -> list[Path] | None:
    """Return ``git ls-files '*.md'`` resolved against ``repo_root``.

    Returns ``None`` when the call is not viable (no ``git`` on PATH,
    or ``repo_root`` is outside a working tree). The caller falls back
    to the glob walk in those cases.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "*.md"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    return [
        repo_root / Path(name.decode("utf-8"))
        for name in result.stdout.split(b"\0")
        if name
    ]


def _glob_md_files_fallback(repo_root: Path) -> list[Path]:
    """Legacy glob walk used when git is unavailable.

    Preserved verbatim from the pre-ISSUE-0036 implementation so
    downstream tarball consumers still get a useful link scan. Walks
    ``docs/**/*.md`` plus repo-root-and-one-level deep, with the same
    ``.git/`` and ``docs/`` filters as before.
    """
    print(
        "[WARN] doc_links: git ls-files unavailable; falling back to glob "
        "walk (depth-limited).",
        file=sys.stderr,
    )

    docs_dir = repo_root / "docs"
    files: list[Path] = []

    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.rglob("*.md")))

    root_files = set(repo_root.glob("*.md"))
    root_files.update(repo_root.glob("*/*.md"))
    git_dir = os.path.normcase(str((repo_root / ".git").resolve())) + os.sep
    root_files = {
        f for f in root_files
        if not os.path.normcase(str(f.resolve())).startswith(git_dir)
    }
    if docs_dir.is_dir():
        docs_resolved = os.path.normcase(str(docs_dir.resolve()))
        root_files = {
            f for f in root_files
            if not os.path.normcase(str(f.resolve())).startswith(
                docs_resolved + os.sep
            )
            and os.path.normcase(str(f.resolve())) != docs_resolved
        }
    files.extend(sorted(root_files))
    return files


def _check_anchor(
    source_rel: str,
    full_link: str,
    fragment: str,
    target_path: Path,
    target_content: str | None,
    repo_root: Path,
    cache: dict[Path, set[str]],
) -> BrokenLink | None:
    """Return a ``BrokenLink`` when ``#fragment`` is absent from ``target_path``.

    ``target_content`` lets the caller skip a file read when it already has
    the text (in-page links).  GitHub line anchors (``#L42``) are accepted
    without a heading lookup.  ``None`` means the anchor is valid or exempt.
    """
    if _LINE_ANCHOR_RE.match(fragment):
        return None

    resolved = target_path.resolve()
    anchors = cache.get(resolved)
    if anchors is None:
        if target_content is None:
            try:
                target_content = target_path.read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                # Unreadable target — the file-existence check owns that
                # failure; don't also report a phantom broken anchor.
                return None
        anchors = _extract_anchors(target_content)
        cache[resolved] = anchors

    if fragment in anchors:
        return None

    try:
        target_display = resolved.relative_to(repo_root).as_posix()
    except ValueError:
        target_display = target_path.name
    return BrokenLink(
        file=source_rel,
        link=full_link,
        target=target_display,
        reason=f"Anchor not found in {target_display}: #{fragment}",
    )


def check_doc_links(repo_root: Path, verbose: bool = False) -> list[BrokenLink]:
    """Scan markdown files and return a list of broken internal links."""
    md_files = _collect_md_files(repo_root)
    failures: list[BrokenLink] = []
    checked = 0
    # Cache each target document's anchor set (resolved path -> slugs) so a
    # file linked from many places is parsed once.
    anchor_cache: dict[Path, set[str]] = {}

    print("[SCAN] Checking documentation links...")

    for md_file in md_files:
        rel_path = md_file.relative_to(repo_root).as_posix()

        if verbose:
            print(f"  Checking: {rel_path}")

        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if not content.strip():
            continue

        # Strip code blocks and inline code to avoid false positives
        # from regex patterns like [a-z0-9] being parsed as links.
        # Only strip backtick content OUTSIDE of markdown link text brackets.
        stripped = _CODE_BLOCK_RE.sub("", content)
        stripped = _strip_inline_code_outside_links(stripped)

        file_dir = md_file.parent

        for match in _LINK_RE.finditer(stripped):
            link_path = match.group(2)
            anchor = match.group(3) or ""
            full_link = link_path + anchor
            fragment = anchor[1:] if anchor else ""

            if re.match(r"^https?://", link_path) or link_path.startswith("mailto:"):
                continue
            if re.match(r"^[a-z]+://", link_path):
                continue
            # Skip regex-like targets that leaked through backtick stripping.
            if re.search(r"[*+?^$|\\]", link_path):
                continue

            # Pure in-page link: [text](#anchor). There is no file target to
            # resolve — validate the fragment against this document's own
            # headings.  An empty [text]() has nothing to check.
            if not link_path.strip():
                if not anchor:
                    continue
                checked += 1
                broken = _check_anchor(
                    rel_path, full_link, fragment, md_file, content,
                    repo_root, anchor_cache,
                )
                if broken is not None:
                    failures.append(broken)
                continue

            checked += 1

            if link_path.startswith("/"):
                target = repo_root / link_path.lstrip("/")
            else:
                target = file_dir / link_path

            try:
                target = target.resolve()
            except (OSError, ValueError):
                failures.append(BrokenLink(
                    file=rel_path,
                    link=full_link,
                    target=link_path,
                    reason="Invalid path characters in link target",
                ))
                continue

            if not target.is_file() and not target.is_dir():
                failures.append(BrokenLink(
                    file=rel_path,
                    link=full_link,
                    target=link_path,
                    reason=f"Target not found: {target}",
                ))
                continue

            # Cross-file #anchor: validate against the target document's
            # headings, but only for markdown files — a #fragment on a
            # non-markdown target (source line anchors, images) is not a
            # heading slug.
            if anchor and target.is_file() and target.suffix.lower() == ".md":
                broken = _check_anchor(
                    rel_path, full_link, fragment, target, None,
                    repo_root, anchor_cache,
                )
                if broken is not None:
                    failures.append(broken)

    _n = len(md_files)
    print(f"[OK] Checked {checked} links in {_n} markdown file{'s' if _n != 1 else ''}")
    return failures


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="Check for broken documentation links.")
    parser.add_argument("--verbose", action="store_true", help="Show each file being checked")
    args = parser.parse_args(argv)

    failures = check_doc_links(REPO_ROOT, verbose=args.verbose)

    if failures:
        print()
        print(f"[FAIL] Found {len(failures)} broken link(s):")
        print()
        for f in failures:
            print(f"  File:   {f.file}")
            print(f"  Link:   {f.link}")
            print(f"  Reason: {f.reason}")
            print()
        return 1

    print("[OK] All documentation links are valid!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
