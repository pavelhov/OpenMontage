"""Regression coverage for caption paging and Remotion public asset paths."""

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tools.video.remotion_caption_burn import RemotionCaptionBurn


@pytest.mark.parametrize("word_timings", [True, False])
def test_transcript_segments_end_caption_pages(word_timings):
    segments = [
        {"start": 0, "end": 1, "text": "First line"},
        {"start": 1, "end": 2, "text": "Second line"},
    ]
    if word_timings:
        for seg in segments:
            seg["words"] = [
                {"word": word, "start": seg["start"] + i * 0.5,
                 "end": seg["start"] + (i + 1) * 0.5}
                for i, word in enumerate(seg["text"].split())
            ]
    segments.insert(1, {"start": 1, "end": 1, "text": ""})

    captions = RemotionCaptionBurn()._segments_to_word_captions(segments)

    assert [c["word"] for c in captions] == ["First", "line", "Second", "line"]
    assert [c.get("pageBreakAfter", False) for c in captions] == [False, True, False, True]
    assert [(c["startMs"], c["endMs"]) for c in captions] == [
        (0, 500), (500, 1000), (1000, 1500), (1500, 2000),
    ]


def test_srt_cues_end_caption_pages(tmp_path):
    srt = tmp_path / "captions.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nFirst line\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nSecond\nline\n",
        encoding="utf-8",
    )
    captions = RemotionCaptionBurn()._srt_to_word_captions(str(srt))
    assert [c["word"] for c in captions] == ["First", "line", "Second", "line"]
    assert [c.get("pageBreakAfter", False) for c in captions] == [False, True, False, True]


def test_caption_render_passes_public_relative_video_path(tmp_path, monkeypatch):
    tool = RemotionCaptionBurn()
    root = tmp_path / "composer"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video fixture")
    output = tmp_path / "out.mp4"
    monkeypatch.setattr(tool, "_find_remotion_root", lambda: root)

    def run(cmd, **kwargs):
        if "format=duration" in cmd:
            return CompletedProcess(cmd, 0, stdout="1.0\n")
        if "stream=width,height" in cmd:
            return CompletedProcess(cmd, 0, stdout="1080x1920\n")
        props_arg = next(arg for arg in cmd if arg.startswith("--props="))
        props = json.loads((Path(kwargs["cwd"]) / props_arg.split("=", 1)[1]).read_text())
        assert props["videoSrc"] == "talking-head/source.mp4"
        assert (root / "public" / props["videoSrc"]).read_bytes() == source.read_bytes()
        output.write_bytes(b"render fixture")
        return CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(tool, "run_command", run)
    result = tool._render_remotion(str(source), str(output), [], 4, 52, "#ffffff")
    assert result.success, result.error
