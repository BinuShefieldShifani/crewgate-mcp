"""Booking plugin: flight search and booking through the Duffel test-mode API (Etraveli use case).

Built Wed 25 to Thu 26.
"""

from crewgate.core.plugin import PluginContext, ToolSpec


class BookingPlugin:
    name = "booking"

    def tools(self) -> list[ToolSpec]:
        return []


def build(ctx: PluginContext | None = None) -> BookingPlugin:
    return BookingPlugin()
