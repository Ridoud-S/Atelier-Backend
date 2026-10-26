"""The documentation must describe the schema that actually exists."""
from __future__ import annotations

import re
from pathlib import Path

from app.db.base import Base

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _doc_tables() -> set[str]:
    text = (DOCS / "data-model.md").read_text(encoding="utf-8")
    return set(re.findall(r"^#### `([a-z_]+)`", text, flags=re.MULTILINE))


def _erd_entities() -> set[str]:
    text = (DOCS / "erd.mmd").read_text(encoding="utf-8")
    return set(re.findall(r"^    ([a-z_]+) \{$", text, flags=re.MULTILINE))


def test_data_dictionary_lists_exactly_the_real_tables():
    assert _doc_tables() == set(Base.metadata.tables)


def test_erd_declares_exactly_the_real_tables():
    assert _erd_entities() == set(Base.metadata.tables)


def test_erd_relationships_only_mention_real_tables():
    text = (DOCS / "erd.mmd").read_text(encoding="utf-8")
    rels = re.findall(r"^    ([a-z_]+) [|o}{]+--[|o}{]+ ([a-z_]+) :", text, flags=re.MULTILINE)
    assert rels, "no relationships parsed"
    names = set(Base.metadata.tables)
    assert all(a in names and b in names for a, b in rels)


def test_every_documented_column_exists():
    """Each column named in a table's dictionary section is a real column of that table."""
    text = (DOCS / "data-model.md").read_text(encoding="utf-8")
    sections = re.split(r"^#### `([a-z_]+)`", text, flags=re.MULTILINE)[1:]
    problems = []
    for table, body in zip(sections[0::2], sections[1::2]):
        body = body.split("\n### ")[0].split("\n## ")[0]
        columns = {c.name for c in Base.metadata.tables[table].columns}
        for row in re.findall(r"^\| `([a-z_, `]+)` \|", body, flags=re.MULTILINE):
            for name in re.findall(r"[a-z_]+", row):
                if name not in columns:
                    problems.append((table, name))
    assert problems == []
