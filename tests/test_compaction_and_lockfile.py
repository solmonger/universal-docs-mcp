from pathlib import Path
from tempfile import TemporaryDirectory

from universal_docs_mcp.compaction import compact, get_section, parse_sections
from universal_docs_mcp.lockfile import read_pins


def test_compaction_keeps_structure_and_budget():
    raw = """[![build](badge.svg)](https://example.test)\n\n# Demo\n\nintro text\n\n## Installation\n\npip install demo\n\n## API Reference\n\n""" + ("long api detail " * 300)
    result = compact(raw, budget_tokens=80)

    assert result["content"]
    assert "badge.svg" not in result["content"]
    assert "installation" in result["sections_included"]
    assert result["section_map"]
    assert result["tokens_included"] <= 80


def test_section_lookup_by_slug_and_title():
    sections = parse_sections("# Demo\n\n## Quick Start\n\nrun it")
    quick_start = get_section(sections, "quick-start")
    assert quick_start is not None
    assert quick_start.body == "run it"
    by_title = get_section(sections, "Quick")
    assert by_title is not None
    assert by_title.slug == "quick-start"


def test_requirements_are_pinned_when_exact():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "requirements.txt"
        path.write_text("httpx==0.27.2\nrequests>=2.0\n# comment\n")
        pins = read_pins(path)
    assert pins[0].pinned == "0.27.2"
    assert pins[1].pinned is None
    assert pins[0].ecosystem == "python"
