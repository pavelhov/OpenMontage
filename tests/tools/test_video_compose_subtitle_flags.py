"""Disabled captions must not be reintroduced by FFmpeg composition."""

from subprocess import CompletedProcess

import pytest

from tools.video.video_compose import VideoCompose


@pytest.mark.parametrize("via_render", [False, True])
@pytest.mark.parametrize("direct_path", [False, True])
@pytest.mark.parametrize(
    "options,subtitle_settings,expected_burn",
    [
        ({}, {}, True),
        ({"subtitle_burn": True}, {"enabled": True}, True),
        ({"subtitle_burn": False}, {"enabled": True}, False),
        ({"subtitle_burn": True}, {"enabled": False}, False),
    ],
)
def test_subtitle_disable_flags_override_paths(
    tmp_path, monkeypatch, via_render, direct_path, options, subtitle_settings, expected_burn,
):
    source = tmp_path / "source.mp4"
    source.touch()
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    output = tmp_path / "out.mp4"
    cuts = [{"source": str(source), "in_seconds": 0, "out_seconds": 1}]
    decisions = {"cuts": cuts, "subtitles": {"source": str(srt), **subtitle_settings}}
    inputs = {"edit_decisions": decisions, "options": options, "output_path": str(output)}
    if direct_path:
        inputs["subtitle_path"] = str(srt)

    tool = VideoCompose()
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(tool, "run_command", run)
    monkeypatch.setattr(tool, "_has_audio_stream", lambda path: True)
    if via_render:
        result = tool._render_via_ffmpeg(
            inputs=inputs, edit_decisions=decisions, resolved_cuts=cuts,
            output_path=output, profile=None,
        )
    else:
        result = tool._compose(inputs)

    assert result.success, result.error
    final_command = commands[-1]
    assert any("subtitles=" in arg for arg in final_command) is expected_burn
    assert result.data["has_subtitles"] is expected_burn
    if expected_burn:
        style_filter = final_command[final_command.index("-vf") + 1]
        assert ",FontSize=" in style_filter
        assert ";FontSize=" not in style_filter
