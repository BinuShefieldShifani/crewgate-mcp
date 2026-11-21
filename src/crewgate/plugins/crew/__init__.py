"""Crew plugin: airline crew rosters and duty swaps (Wise IT use case).

Tools are registered on Day 4 (Tue). For now build() returns the plugin with no tools,
so the loader and its rules can already be tested.
"""

from crewgate.core.plugin import ToolSpec


class CrewPlugin:
    name = "crew"

    def tools(self) -> list[ToolSpec]:
        return []


def build() -> CrewPlugin:
    return CrewPlugin()
