"""Tool-description variants: the same tools, described three ways, chosen per gateway.

The text a model reads decides which tool it picks. Step 6 measures that instead of guessing:
every plugin writes each of its tools three ways, the gateway serves one of them
(CREWGATE_TOOL_DESCRIPTIONS), and evals/selection.py scores how often a model picks the right
tool under each.

  minimal   one short line, the way descriptions generated from an API spec often look
  guided    what the tool does, when to use it, what to call before or after (Step 5's text)
  contract  fixed sections: Use when / Not for / Needs / Returns / Next, naming the other tools

Descriptions name other tools by their MCP names (booking_get_offer, not booking.get_offer):
that is the only name the model ever sees.
"""

from collections.abc import Mapping
from typing import Literal, get_args

Variant = Literal["minimal", "guided", "contract"]
VARIANTS: tuple[Variant, ...] = get_args(Variant)
DEFAULT_VARIANT: Variant = "guided"

DescriptionTable = Mapping[str, Mapping[str, str]]  # variant -> tool name -> text


class DescriptionError(Exception):
    pass


def describe(table: DescriptionTable, tool: str, variant: str) -> str:
    if variant not in VARIANTS:
        raise DescriptionError(f"unknown description variant {variant!r}, expected one of {VARIANTS}")
    text = table.get(variant, {}).get(tool)
    if not text:
        raise DescriptionError(f"tool {tool} has no {variant!r} description")
    return text


def check_table(table: DescriptionTable, tools: list[str]) -> None:
    """Every tool needs every variant, and no variant may describe a tool that doesn't exist."""
    for variant in VARIANTS:
        described = set(table.get(variant, {}))
        if missing := sorted(set(tools) - described):
            raise DescriptionError(f"{variant!r} descriptions missing for {missing}")
        if extra := sorted(described - set(tools)):
            raise DescriptionError(f"{variant!r} describes unknown tools {extra}")
