"""Optional MCP installation must not prevent unrelated tool discovery."""
import importlib

import pytest

from tools.base_tool import ToolStatus
from tools.openart_mcp_account import OpenArtMCPAccount
from tools.video.openart_mcp_video import OpenArtMCPVideo


@pytest.mark.parametrize('missing', ['lib.openart_mcp', 'lib.openart_mcp_dispatch'])
def test_missing_optional_module_preserves_discovery(monkeypatch, missing):
    original = importlib.import_module

    def load(name, *args, **kwargs):
        if name == missing:
            raise ModuleNotFoundError(name, name=name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, 'import_module', load)
    from tools.tool_registry import ToolRegistry
    registry = ToolRegistry()
    registry.discover()
    assert registry.get('grok_cli_video') is not None
    assert registry.get('frame_sampler') is not None
    for tool in (OpenArtMCPAccount(), OpenArtMCPVideo()):
        assert tool.get_status() == ToolStatus.UNAVAILABLE
        result = tool.execute({})
        assert not result.success
        assert result.cost_usd is None
        assert result.data['error']['kind'] == 'dependency_unavailable'
    info = OpenArtMCPVideo().get_info()
    assert info['generation_enabled'] is False
    assert info['model_catalog'] == {}
    assert info['observed_model_mode_count'] == 0


@pytest.mark.parametrize('tool_class', [OpenArtMCPAccount, OpenArtMCPVideo])
def test_installed_dependency_import_failure_is_not_hidden(monkeypatch, tool_class):
    def broken(name):
        raise ModuleNotFoundError('broken internal dependency', name='unrelated_internal_module')
    monkeypatch.setattr(importlib, 'import_module', broken)
    for call in (tool_class().get_status, lambda: tool_class().execute({})):
        with pytest.raises(ModuleNotFoundError, match='broken internal dependency'):
            call()
