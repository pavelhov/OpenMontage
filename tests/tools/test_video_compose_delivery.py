"""Profile-driven compose/encode must retain safe social-master timing and audio."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.media_profiles import delivery_timing_issues
from tools.video.video_compose import VideoCompose


def _inputs(operation, source, output, **extra):
    inputs = {"operation": operation, "output_path": str(output), **extra}
    if operation == "encode":
        inputs["input_path"] = str(source)
    else:
        inputs["edit_decisions"] = {
            "cuts": [{"source": str(source), "in_seconds": 0, "out_seconds": 1}],
        }
    return inputs


@pytest.mark.parametrize("operation", ["encode", "compose"])
@pytest.mark.parametrize("profile", [None, "unknown", "tiktok_720p"])
@pytest.mark.parametrize("replace_audio", [False, True])
def test_profile_flags_preserve_caller_settings(tmp_path, monkeypatch, operation, profile, replace_audio):
    source = tmp_path / "source.mp4"
    source.touch()
    audio = tmp_path / "mix.wav"
    audio.touch()
    commands = []
    tool = VideoCompose()
    monkeypatch.setattr(tool, "run_command", lambda cmd: commands.append(cmd))
    monkeypatch.setattr(tool, "_has_audio_stream", lambda path: True)
    inputs = _inputs(
        operation, source, tmp_path / "out.mp4",
        profile=profile, codec="libx265", crf=17, preset="fast",
    )
    if replace_audio and operation == "compose":
        inputs["audio_path"] = str(audio)
    result = tool.execute(inputs)
    assert result.success, result.error
    command = commands[-1]
    encoded = operation == "encode" or profile == "tiktok_720p"
    assert command[command.index("-c:v") + 1] == ("libx265" if encoded else "copy")
    if encoded:
        assert command[command.index("-crf") + 1] == "17"
        assert command[command.index("-preset") + 1] == "fast"
        assert command.count("-c:v") == 1
    assert "-ar" not in command and "-ac" not in command
    assert "-profile:v" not in command  # Explicit encoder overrides keep their own profile semantics.
    assert command[command.index("-c:a") + 1] == (
        "aac" if operation == "encode" or replace_audio else "copy"
    )
    if operation == "encode":
        assert command[command.index("-b:a") + 1] == "192k"
    if operation == "compose" and replace_audio:
        assert command[command.index("-map") + 1] == "0:v"
        assert "1:a" in command and "-shortest" in command
    if profile == "tiktok_720p":
        for option, value in (
            ("-s", "720x1280"), ("-r", "30"), ("-bf", "0"),
            ("-force_key_frames", "0"), ("-movflags", "+faststart"),
        ):
            assert command[command.index(option) + 1] == value
    else:
        assert "-bf" not in command and "-movflags" not in command


def test_compose_fails_when_audio_probe_fails_instead_of_synthesizing_silence(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.touch()
    commands = []
    tool = VideoCompose()
    monkeypatch.setattr(tool, "run_command", lambda cmd: commands.append(cmd))

    def failed_probe(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="probe could not read source")

    monkeypatch.setattr(subprocess, "check_output", failed_probe)
    result = tool.execute(_inputs("compose", source, tmp_path / "out.mp4"))

    assert not result.success
    assert "ffprobe" in result.error.lower()
    assert "probe could not read source" in result.error
    assert commands == []


def _run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def _atom_types(path: Path) -> list[bytes]:
    types = []
    with path.open("rb") as media:
        while header := media.read(8):
            size = int.from_bytes(header[:4], "big")
            types.append(header[4:])
            header_size = 8
            if size == 1:
                size = int.from_bytes(media.read(8), "big")
                header_size = 16
            if not size:
                break
            media.seek(size - header_size, 1)
    return types


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe unavailable",
)
@pytest.mark.parametrize("case", ["encode_audio", "encode_silent", "compose_audio", "compose_silent", "compose_mix"])
def test_real_tiktok_delivery_timing_and_audio(tmp_path, case):
    source = tmp_path / "source.mp4"
    has_audio = case not in {"encode_silent", "compose_silent"}
    command = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24:d=1",
    ]
    if has_audio:
        command.extend(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000:duration=1"])
    command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)])
    _run(command)
    operation = case.split("_")[0]
    output = tmp_path / "delivery.mp4"
    inputs = _inputs(operation, source, output, profile="tiktok_720p", preset="medium", crf=28)
    if case == "compose_mix":
        mix = tmp_path / "mix.wav"
        _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=32000:duration=1", str(mix)])
        inputs["audio_path"] = str(mix)
    result = VideoCompose().execute(inputs)
    assert result.success, result.error
    probe = json.loads(_run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output),
    ]))
    frames = json.loads(_run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time,key_frame,pict_type", "-of", "json", str(output),
    ]))["frames"]
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio = next((stream for stream in probe["streams"] if stream["codec_type"] == "audio"), None)
    assert (video["width"], video["height"]) == (720, 1280)
    assert delivery_timing_issues(probe, target_fps=30) == []
    assert video["r_frame_rate"] == video["avg_frame_rate"] == "30/1"
    assert frames[0]["key_frame"] == 1
    assert float(frames[0]["best_effort_timestamp_time"]) == 0
    assert all(frame["pict_type"] != "B" for frame in frames)
    timestamps = [float(frame["best_effort_timestamp_time"]) for frame in frames]
    assert all(abs(b - a - 1 / 30) < 0.000002 for a, b in zip(timestamps, timestamps[1:]))
    atoms = _atom_types(output)
    assert atoms.index(b"moov") < atoms.index(b"mdat")
    if case == "encode_silent":
        assert audio is None
    else:
        assert audio and audio["codec_name"] == "aac"
        # Compose's existing segment normalization uses stereo 48kHz; encode
        # and a replacement mix retain the source/mix channel count and rate.
        expected = ("48000", 2) if case in {"compose_audio", "compose_silent"} else ("32000", 1)
        assert (audio["sample_rate"], audio["channels"]) == expected
    print(json.dumps({
        "case": case, "size": [video["width"], video["height"]],
        "fps": video["avg_frame_rate"], "has_b_frames": video["has_b_frames"],
        "container_start": probe["format"]["start_time"], "video_start": video["start_time"],
        "first_frame": frames[0], "faststart": True,
        "audio": {key: audio[key] for key in ("codec_name", "sample_rate", "channels", "start_time")} if audio else None,
    }))
