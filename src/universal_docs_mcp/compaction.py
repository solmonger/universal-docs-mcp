"""LLM-oriented compaction of fetched documentation.

The upstream docs cannot be changed (they update in real time), but the bytes
we hand to a model can be shaped. This module turns raw registry/README
markdown into:

  1. a *section map* (slug, title, level, size) so an agent can request exactly
     the section it needs instead of re-reading the whole page, and
  2. a *budgeted compact view*: noise stripped (badges, HTML comments,
     image-only lines, horizontal rules), highest-value sections kept in full,
     lower-priority sections reduced to their headings once the token budget
     is spent.

Token estimates are deliberately cheap heuristics (chars / 4); the goal is
budget shaping, not billing accuracy.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

# Headings that carry the most action-per-token for a coding agent.
_PRIORITY_PATTERNS = [
    re.compile(r"^(intro|overview|about|what is)", re.I),
    re.compile(r"^(install(ation)?|setup|quick ?start|getting started)", re.I),
    re.compile(r"^(usage|example|basic usage|how to use)", re.I),
    re.compile(r"^(api|reference|configuration|options|cli)", re.I),
]

_NOISE_LINE = re.compile(
    r"^("
    r"\[!\[.*\]\(.*\)\]\(.*\)"  # badge: [![alt](img)](link)
    r"|!\[.*\]\(.*\)"  # bare image
    r"|<!--.*?-->"  # html comment (single line)
    r"|-{3,}|_{3,}|\*{3,}"  # horizontal rules
    r"|<br\s*/?>"  # stray breaks
    r")\s*$"
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def estimate_tokens(text: str) -> int:
    """Cheap token budget estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _fenced_lines(markdown: str):
    """Yield lines with fenced code protected from prose transformations."""
    fence = None
    for line in markdown.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        protected = fence is not None
        if marker:
            run, rest = marker.groups()
            if fence is None:
                fence = run
                protected = True
            elif run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                fence = None
        yield line, protected


def strip_noise(markdown: str) -> str:
    """Remove badge/image/html noise and collapse blank runs."""
    out = []
    for line, protected in _fenced_lines(markdown):
        if not protected and _NOISE_LINE.match(line.strip()):
            continue
        out.append(line)
    text = "\n".join(out)
    return text.strip()


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(slug) > 100:
        slug = slug[:100] + "-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return slug or "section"


@dataclass
class Section:
    slug: str
    title: str
    level: int
    body: str

    @property
    def chars(self) -> int:
        return len(self.body)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title[:256],
            "title_truncated": len(self.title) > 256,
            "level": self.level,
            "chars": self.chars,
            "tokens": estimate_tokens(self.body),
        }


def parse_sections(markdown: str) -> list[Section]:
    """Split markdown into an intro chunk plus ATX-heading sections."""
    clean = strip_noise(markdown)
    sections: list[Section] = []
    intro_lines: list[str] = []
    current: Optional[dict] = None
    seen_slugs: set[str] = set()

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        body = "\n".join(current["lines"]).strip()
        slug = current["slug"]
        if slug in seen_slugs:
            n = 2
            while f"{slug}-{n}" in seen_slugs:
                n += 1
            slug = f"{slug}-{n}"
        seen_slugs.add(slug)
        sections.append(
            Section(
                slug=slug, title=current["title"], level=current["level"], body=body
            )
        )
        current = None

    for line, protected in _fenced_lines(clean):
        m = None if protected else _HEADING.match(line)
        if m:
            flush()
            title = m.group(2).strip()
            slug = slugify(title)
            current = {
                "title": title,
                "level": len(m.group(1)),
                "slug": slug,
                "lines": [],
            }
        elif current is None:
            intro_lines.append(line)
        else:
            current["lines"].append(line)
    flush()

    intro = "\n".join(intro_lines).strip()
    if intro:
        sections.insert(0, Section(slug="intro", title="(intro)", level=0, body=intro))
    return sections


def _priority(title: str) -> int:
    for i, pat in enumerate(_PRIORITY_PATTERNS):
        if pat.search(title):
            return i
    return len(_PRIORITY_PATTERNS)


def section_map(
    sections: list[Section], offset: int = 0, limit: int = 100
) -> list[dict]:
    return [s.to_dict() for s in sections[offset : offset + min(limit, 100)]]


def get_section(sections: list[Section], key: str) -> Optional[Section]:
    """Find a section by slug, or by case-insensitive title substring."""
    key_l = key.strip().lower()
    for s in sections:
        if s.slug == key_l:
            return s
    for s in sections:
        if key_l and key_l in s.title.lower():
            return s
    return None


def compact(
    markdown: str,
    budget_tokens: int = 1500,
    header: str = "",
) -> dict:
    """Return a budgeted compact view plus the full section map.

    The final serialized content is capped as well as the section selection.
    This matters when a header or join separators consume more than the cheap
    per-section estimate.
    """
    budget_tokens = max(1, int(budget_tokens))
    sections = parse_sections(markdown)
    ordered = sorted(
        enumerate(sections),
        key=lambda pair: (_priority(pair[1].title), pair[0]),
    )

    # Keep the header, but do not let metadata consume more than the caller's
    # entire budget. The server supplies a short header; this also makes the
    # standalone helper safe for arbitrary callers.
    header = header.strip()
    header_limit = budget_tokens * 4
    if len(header) > header_limit:
        header = header[:header_limit]
    used = estimate_tokens(header) if header else 0

    included: list[Section] = []
    omitted: list[Section] = []
    for _, sec in ordered:
        cost = estimate_tokens(f"## {sec.title}\n\n{sec.body}") + 4
        if used + cost <= budget_tokens:
            included.append(sec)
            used += cost
        else:
            omitted.append(sec)

    included.sort(key=lambda s: sections.index(s))
    parts = []
    if header:
        parts.append(header)
    if included:
        parts.append("\n\n".join(f"## {sec.title}\n\n{sec.body}" for sec in included))
    body = "\n\n".join(p for p in parts if p)

    # The accounting above is intentionally approximate. Apply a final hard
    # character cap so the returned estimate cannot exceed the requested budget.
    was_clipped = len(body) > header_limit
    body = body[:header_limit]

    return {
        "content": body,
        "tokens_included": estimate_tokens(body),
        "truncated": was_clipped or bool(omitted),
        "budget_scope": "content only; character-based estimate, not model tokens or JSON metadata",
        "budget_tokens": budget_tokens,
        "sections_included": [s.slug for s in included],
        "sections_omitted": [s.slug for s in omitted[:100]],
        "sections_omitted_total": len(omitted),
        "section_map": section_map(sections),
        "section_map_total": len(sections),
        "section_map_truncated": len(sections) > 100,
    }
