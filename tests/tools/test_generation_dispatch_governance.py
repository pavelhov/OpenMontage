"""The common generation boundary must fail closed before provider execution."""
import json
import pytest
from tools.base_tool import BaseTool, ToolResult, ToolTier

class FakeVideo(BaseTool):
    name = 'fake_video'
    provider = 'fake'
    capability = 'video_generation'
    tier = ToolTier.GENERATE
    calls = 0
    def execute(self, inputs):
        self.calls += 1
        return ToolResult(success=True)

def test_direct_generation_in_strict_custom_root_cannot_bypass_gate(tmp_path):
    (tmp_path / 'project.json').write_text(json.dumps({'project_id':'p','governance':{'mode':'strict','version':'1.0'},'story_revision':'r1'}))
    tool = FakeVideo()
    with pytest.raises(ValueError, match='governance'):
        tool.execute({'prompt':'unapproved', 'output_path':str(tmp_path / 'assets' / 'out.mp4')})
    assert tool.calls == 0


def test_strict_gate_survives_optional_event_import_failure(tmp_path,monkeypatch):
    import builtins
    original = builtins.__import__
    def failed_events(name,*args,**kwargs):
        if name == 'lib.events': raise ImportError('optional events unavailable')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',failed_events)
    (tmp_path/'project.json').write_text(json.dumps({'governance':{'mode':'strict','version':'1.0'}}))
    tool = FakeVideo()
    with pytest.raises(ValueError,match='governance'):
        tool.execute({'output_path':str(tmp_path/'out.mp4')})
    assert tool.calls == 0


def test_legacy_diagnostics_stock_and_editing_remain_ungoverned(tmp_path):
    (tmp_path/'project.json').write_text(json.dumps({'governance':{'mode':'strict','version':'1.0'}}))
    for capability in ('video_analysis','video_editing','image_search','music_search'):
        tool = FakeVideo()
        tool.capability = capability
        assert tool.execute({'output_path':str(tmp_path/'out.mp4')}).success
        assert tool.calls == 1
    legacy = FakeVideo()
    assert legacy.execute({'prompt':'legacy no project'}).success


def test_missing_or_unenrolled_explicit_production_project_fails(tmp_path):
    tool = FakeVideo()
    with pytest.raises(ValueError,match='unknown'):
        tool.execute({'project_dir':str(tmp_path),'governance':{}})
    (tmp_path/'project.json').write_text('{"project_id":"legacy"}')
    with pytest.raises(ValueError,match='not enrolled'):
        tool.execute({'project_dir':str(tmp_path),'governance':{}})
    assert tool.calls == 0


def test_conflicting_context_and_escaping_output_fail(tmp_path):
    root = tmp_path/'a';root.mkdir()
    other = tmp_path/'b';other.mkdir()
    for path in (root,other): (path/'project.json').write_text('{"project_id":"p"}')
    tool = FakeVideo()
    with pytest.raises(ValueError,match='conflicting'):
        tool.execute({'project_dir':str(root),'output_path':str(other/'out.mp4')})
    with pytest.raises(ValueError,match='escapes'):
        tool.execute({'project_dir':str(root),'output_path':'../outside.mp4'})
    assert tool.calls == 0


def test_output_symlink_cannot_erase_strict_project_context(tmp_path):
    root = tmp_path/'p';root.mkdir()
    (root/'project.json').write_text(json.dumps({'governance':{'mode':'strict','version':'1.0'}}))
    (root/'escape').symlink_to(tmp_path,target_is_directory=True)
    tool = FakeVideo()
    with pytest.raises(ValueError,match='escapes'):
        tool.execute({'output_path':str(root/'escape'/'outside.mp4')})
    assert tool.calls == 0


def test_real_stock_taxonomy_is_ungoverned_despite_generation_capability(tmp_path):
    (tmp_path/'project.json').write_text(json.dumps({'governance':{'mode':'strict','version':'1.0'}}))
    for capability in ('video_generation','image_generation'):
        stock = FakeVideo();stock.tier = ToolTier.SOURCE;stock.capability = capability
        assert stock.execute({'output_path':str(tmp_path/'stock.mp4')}).success
        assert stock.calls == 1
