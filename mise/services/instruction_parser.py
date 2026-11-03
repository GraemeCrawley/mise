"""Instruction blob → structured StepSections.

Pure-Python, no LLM. The legacy xlsx stores cooking instructions as a
single text blob (column K per legacy/apps-script/README.md). This
service splits the blob into sections and individual numbered steps.

Section detection mirrors the ingredient parser — lines ending in ":"
with ≤5 words are section headers. Step numbering ("1.", "1)",
"Step 1:") is stripped from each step's text since we'll auto-number
on the read side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mise.services.ingredient_parser import is_section_header, section_name

# Strip leading "1.", "1)", "Step 1:", "Step 1.", etc.
_LEADING_NUMBER = re.compile(
    r"""
    ^\s*                                # leading whitespace
    (?:                                 # one of:
        (?:Step\s+)?                    #   optional "Step "
        \d+                             #   number
        [.\):]\s*                       #   . ) or : then ws
        |
        [-•*]\s+                        #   bullet character
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ParsedStepSection:
    """One section of instructions. `name` is None for the default
    (unsectioned) bucket. `steps` is the ordered list of step texts
    after leading-numbering strip."""
    name: str | None
    steps: list[str] = field(default_factory=list)


def parse_instructions(blob: str) -> list[ParsedStepSection]:
    """Split a freeform instructions blob into section + step structure.

    Returns at least one section. Empty input returns [] — caller decides
    whether to flag.
    """
    if not blob or not blob.strip():
        return []

    sections: list[ParsedStepSection] = [ParsedStepSection(name=None)]
    for raw in blob.splitlines():
        line = raw.strip()
        if not line:
            continue
        if is_section_header(line):
            sections.append(ParsedStepSection(name=section_name(line)))
            continue
        cleaned = _LEADING_NUMBER.sub("", line).strip()
        if cleaned:
            sections[-1].steps.append(cleaned)

    # Drop the leading default section if empty (blob started with a header)
    if not sections[0].steps and sections[0].name is None:
        sections.pop(0)

    return sections
