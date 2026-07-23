"""
Pure functions for decomposing LSP hover ("quick info") text into signature and documentation parts.

The parsing is deliberately language-agnostic and conservative: the raw text is always preserved,
and when the split into signature/documentation cannot be determined, no content is fabricated.
"""

import re
from dataclasses import dataclass

_FENCE_RE = re.compile(r"^```[^\n`]*\n(.*?)\n?```", re.DOTALL | re.MULTILINE)
_DECLARATION_KEYWORDS = (
    "class ",
    "interface ",
    "struct ",
    "enum ",
    "def ",
    "fn ",
    "func ",
    "function ",
    "void ",
    "public ",
    "private ",
    "protected ",
    "static ",
)


@dataclass(frozen=True)
class QuickInfoParts:
    raw: str
    signature: str | None
    documentation: str | None


def _looks_like_declaration(line: str, symbol_name: str) -> bool:
    if symbol_name and symbol_name in line:
        return True
    if "(" in line:
        return True
    return any(keyword in line for keyword in _DECLARATION_KEYWORDS)


def parse_quick_info(raw: str | None, symbol_name: str) -> QuickInfoParts | None:
    """
    Splits sanitized hover text into a signature part and a documentation part.

    Rules (in order):

    1. Line endings are normalized to LF and surrounding whitespace is stripped.
    2. If the text contains a fenced Markdown code block, the content of the *first* block is the signature
       and everything outside fenced code blocks is the documentation.
    3. Without a code block, the first non-empty line is used as the signature only if it plausibly
       is a declaration (contains the symbol name, a parenthesis, or a declaration keyword).
    4. Otherwise the signature is None and the whole text is the documentation.

    The raw (normalized) text is always preserved in the result.

    :param raw: the sanitized hover text, or None if no hover information is available
    :param symbol_name: the name of the symbol the hover text belongs to (used for the declaration heuristic)
    :return: the decomposed parts, or None if raw is None or effectively empty
    """
    if raw is None:
        return None
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None

    fence_matches = list(_FENCE_RE.finditer(normalized))
    if fence_matches:
        signature = fence_matches[0].group(1).strip() or None
        # documentation is everything outside fenced code blocks
        doc_parts = []
        last_end = 0
        for match in fence_matches:
            doc_parts.append(normalized[last_end : match.start()])
            last_end = match.end()
        doc_parts.append(normalized[last_end:])
        documentation = "\n".join(part.strip() for part in doc_parts if part.strip()) or None
        return QuickInfoParts(raw=normalized, signature=signature, documentation=documentation)

    lines = normalized.split("\n")
    first_line_idx = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_line_idx is not None and _looks_like_declaration(lines[first_line_idx], symbol_name):
        signature = lines[first_line_idx].strip()
        documentation = "\n".join(lines[first_line_idx + 1 :]).strip() or None
        return QuickInfoParts(raw=normalized, signature=signature, documentation=documentation)

    return QuickInfoParts(raw=normalized, signature=None, documentation=normalized)
