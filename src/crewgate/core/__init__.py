"""Shared secure core: auth, entitlements, partner HTTP, logging, audit, metrics.

Nothing in core knows about crew rosters or flights. Plugins depend on core, never the reverse.
"""
