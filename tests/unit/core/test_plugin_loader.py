import sys
import types

import pytest
from pydantic import SecretStr

from crewgate.core.plugin import PluginLoadError, ToolSpec, load_plugins
from crewgate.plugins.crew.config import CrewSettings


async def _h():
    return None


def _register_fake(name, plugin_name, tool_names):
    mod = types.ModuleType(f"crewgate.plugins.{name}")

    class P:
        def __init__(self):
            self.name = plugin_name

        def tools(self):
            return [ToolSpec(t, "d", "x.read", _h) for t in tool_names]

    mod.build = lambda ctx=None: P()
    sys.modules[f"crewgate.plugins.{name}"] = mod


def test_loads_crew_and_booking():
    names = [p.name for p in load_plugins(["crew", "booking"])]
    assert names == ["crew", "booking"]


def test_unknown_plugin_rejected():
    with pytest.raises(PluginLoadError):
        load_plugins(["does_not_exist"])


def test_unprefixed_tool_rejected():
    _register_fake("fake_bad", "fake_bad", ["get_thing"])
    with pytest.raises(PluginLoadError, match="prefixed"):
        load_plugins(["fake_bad"])


def test_duplicate_tool_rejected():
    _register_fake("fake_a", "fake", ["fake.t"])
    _register_fake("fake_b", "fake", ["fake.t"])
    with pytest.raises(PluginLoadError, match="duplicate"):
        load_plugins(["fake_a", "fake_b"])


def test_write_tool_without_summary_rejected():
    mod = types.ModuleType("crewgate.plugins.fake_write")

    class P:
        name = "fake_write"

        def tools(self):
            return [ToolSpec("fake_write.do", "d", "x.write", _h, writes=True)]

    mod.build = lambda ctx=None: P()
    sys.modules["crewgate.plugins.fake_write"] = mod
    with pytest.raises(PluginLoadError, match="summarize"):
        load_plugins(["fake_write"])


def test_secrets_never_leak_through_settings_repr():
    s = CrewSettings(client_secret=SecretStr("super-secret-value"))
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s.model_dump())
