"""Keyword search over the policy document, one result per section."""

from __future__ import annotations

import re
from pathlib import Path

_WORD = re.compile(r"[a-z]+")
_STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "can",
    "i",
    "my",
    "do",
    "how",
    "what",
    "of",
    "to",
    "for",
    "and",
    "it",
    "on",
    "in",
    "be",
    "long",
    "does",
    "you",
    "your",
    "me",
    "get",
}


def _terms(text: str) -> set[str]:
    return {w[:6] for w in _WORD.findall(text.lower()) if w not in _STOP}


class PolicyBase:
    def __init__(self, path: str | Path) -> None:
        self.sections: list[tuple[str, str]] = []
        heading, lines = None, []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if heading:
                    self.sections.append((heading, " ".join(lines).strip()))
                heading, lines = line[3:].strip(), []
            elif heading and line.strip():
                lines.append(line.strip())
        if heading:
            self.sections.append((heading, " ".join(lines).strip()))

    def search(self, query: str, top: int = 2) -> list[tuple[str, str]]:
        wanted = _terms(query)
        scored = [(len(wanted & _terms(f"{h} {h} {b}")), h, b) for h, b in self.sections]
        return [(h, b) for score, h, b in sorted(scored, key=lambda s: -s[0]) if score > 0][:top]
