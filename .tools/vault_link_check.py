#!/usr/bin/env python3
"""Resolve every [[wiki-link]] in docs/ and CLAUDE.md.

Reports broken links. Exit 0 = clean, nonzero = count of broken.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# Build index of all .md files (basename and full relative path).
md_files = {p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md")}
md_basenames = {p.stem: p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md")}

# Also accept db/schema (without .md) — pseudo-target
allowed_paths = set(md_files)
allowed_paths.update(p[:-3] for p in md_files)  # without .md ext


def resolve(target: str) -> bool:
    """Return True if a wiki-link target resolves to a known file."""
    if not target:
        return True  # empty link — skip
    # Strip Obsidian anchors / aliases
    t = target.split("|", 1)[0]
    t = t.split("#", 1)[0]
    t = t.strip()
    if not t:
        return True
    # try direct: with/without .md
    if t in allowed_paths:
        return True
    if t.endswith(".md") and t[:-3] in allowed_paths:
        return True
    if (t + ".md") in md_files:
        return True
    # try basename match
    if t in md_basenames:
        return True
    # special-case: db/schema (db/schema.sql exists outside .md set)
    if t.startswith("db/schema"):
        return (DOCS / "db" / "schema.sql").exists()
    return False


WIKI_RE = re.compile(r"\[\[([^\]]+)\]\]")

broken = []
scanned_files = 0
total_links = 0
scan_roots = [DOCS, ROOT / "CLAUDE.md"]
for root in scan_roots:
    paths = [root] if root.is_file() else list(root.rglob("*.md"))
    for path in paths:
        scanned_files += 1
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in WIKI_RE.finditer(text):
            total_links += 1
            target = m.group(1)
            if not resolve(target):
                broken.append((path.relative_to(ROOT).as_posix(), target))

print(f"scanned: {scanned_files} files, {total_links} wiki-links")
if broken:
    print(f"\nBROKEN ({len(broken)}):")
    for f, t in broken:
        print(f"  {f}: [[{t}]]")
    sys.exit(min(len(broken), 255))
else:
    print("all wiki-links resolve — clean")
    sys.exit(0)
