"""Delivery-geometry gate for TikTok-safe package/stitch paths.

Regression for C13v3: photo-true Grok image_to_video clips at 720x1264 were
concat-copied into final.mp4. OpenMontage must normalize to exact 9:16
(720x1280 / 1080x1920) before shipping social masters.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.media_profiles import (
    ALL_PROFILES,
    delivery_geometry_issue,
    delivery_timing_issues,
    get_profile,
    is_near_portrait_9_16,
    resolve_delivery_geometry,
    snap_portrait_9_16,
)
from tools.video.video_compose import VideoCompose
from tools.video.video_stitch import VideoStitch

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


def _make_clip(path: Path, w: int, h: int, d: float = 1.0, color: str = "teal") -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={d}:r=24",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={d}",
            "-c:v", "libx264", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-shortest", str(path),
        ],
        capture_output=True,
        check=True,
    )


def _dims(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
        ]
    ).decode().strip()
    w, h = out.split(",")
    return int(w), int(h)


def _probe_json(path: Path) -> dict:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
    )
    return json.loads(out.decode())


def _make_bframe_clip(path: Path, w: int, h: int, d: float = 1.0, color: str = "teal") -> None:
    """Simulate Grok-style H.264 with B-frames and non-zero start_time."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={d}:r=30",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={d}",
            "-c:v", "libx264", "-crf", "28", "-pix_fmt", "yuv420p",
            "-bf", "2", "-g", "30",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-shortest", str(path),
        ],
        capture_output=True,
        check=True,
    )


def _timing(path: Path) -> dict[str, str | int | float]:
    probe = _probe_json(path)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    fmt = probe.get("format", {})
    return {
        "has_b_frames": int(video.get("has_b_frames", 0)),
        "start_time": float(fmt.get("start_time", 0) or 0),
        "r_frame_rate": str(video.get("r_frame_rate", "")),
        "avg_frame_rate": str(video.get("avg_frame_rate", "")),
    }


def _first_video_packet(path: Path) -> dict[str, str]:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_packets", "-read_intervals", "%+#1",
            "-show_entries", "packet=pts_time,flags", "-of", "json", str(path),
        ]
    )
    packets = json.loads(out.decode()).get("packets", [])
    assert packets, f"No video packets found in {path}"
    return packets[0]


def test_tiktok_720p_profile_exists():
    assert "tiktok_720p" in ALL_PROFILES
    p = get_profile("tiktok_720p")
    assert (p.width, p.height) == (720, 1280)


def test_snap_and_near_9_16_helpers():
    assert is_near_portrait_9_16(720, 1264)
    assert snap_portrait_9_16(720, 1264) == (720, 1280)
    assert snap_portrait_9_16(941, 1672) == (1080, 1920)
    issue = delivery_geometry_issue(
        720, 1264, {"width": 720, "height": 1280, "source": "profile:tiktok_720p"}
    )
    assert issue is not None
    assert "720x1264" in issue
    assert "720x1280" in issue


def test_resolve_delivery_geometry_auto_snaps_off_geometry_vertical():
    target = resolve_delivery_geometry(
        observed_width=720,
        observed_height=1264,
        auto_snap_near_9_16=True,
    )
    assert target == {
        "width": 720,
        "height": 1280,
        "fit": "cover",
        "source": "auto_snap_near_9_16",
        "fps": 30,
        "codec": "libx264",
        "audio_codec": "aac",
        "profile": "tiktok_720p",
    }


def test_stitch_normalizes_720x1264_to_720x1280(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    out = tmp_path / "final.mp4"
    _make_clip(a, 720, 1264, color="darkblue")
    _make_clip(b, 720, 1264, color="darkgreen")

    result = VideoStitch().execute(
        {
            "operation": "stitch",
            "clips": [str(a), str(b)],
            "profile": "tiktok_720p",
            "output_path": str(out),
        }
    )
    assert result.success, result.error
    assert _dims(out) == (720, 1280)
    assert result.data["auto_normalized"] is True
    assert result.data["delivery_geometry"]["width"] == 720
    assert result.data["delivery_geometry"]["height"] == 1280


def test_stitch_auto_snaps_near_9_16_without_explicit_profile(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    out = tmp_path / "final.mp4"
    _make_clip(a, 720, 1264, color="purple")
    _make_clip(b, 720, 1264, color="orange")

    result = VideoStitch().execute(
        {
            "operation": "stitch",
            "clips": [str(a), str(b)],
            "output_path": str(out),
        }
    )
    assert result.success, result.error
    assert _dims(out) == (720, 1280)


def test_stitch_passes_through_exact_720x1280(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    out = tmp_path / "final.mp4"
    _make_clip(a, 720, 1280, color="navy")
    _make_clip(b, 720, 1280, color="maroon")

    result = VideoStitch().execute(
        {
            "operation": "stitch",
            "clips": [str(a), str(b)],
            "profile": "tiktok_720p",
            "output_path": str(out),
        }
    )
    assert result.success, result.error
    assert _dims(out) == (720, 1280)
    timing = _timing(out)
    assert timing["has_b_frames"] == 0
    assert timing["r_frame_rate"] == "30/1"
    assert timing["avg_frame_rate"] == "30/1"
    assert abs(float(timing["start_time"])) < 0.001
    assert result.data["method"] == "filter_concat_delivery_safe"
    first_packet = _first_video_packet(out)
    assert float(first_packet.get("pts_time", -1)) == 0.0
    assert str(first_packet.get("flags", "")).startswith("K")


def test_stitch_tiktok_720p_never_stream_copies_exact_geometry(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    out = tmp_path / "final.mp4"
    _make_bframe_clip(a, 720, 1280, color="navy")
    _make_bframe_clip(b, 720, 1280, color="maroon")

    result = VideoStitch().execute(
        {
            "operation": "stitch",
            "clips": [str(a), str(b)],
            "profile": "tiktok_720p",
            "output_path": str(out),
        }
    )
    assert result.success, result.error
    assert _dims(out) == (720, 1280)
    timing = _timing(out)
    assert timing["has_b_frames"] == 0
    assert timing["r_frame_rate"] == "30/1"
    assert abs(float(timing["start_time"])) < 0.001
    assert result.data["method"] == "filter_concat_delivery_safe"
    assert result.data["delivery_timing_gate"] is True


def test_delivery_timing_issues_flags_bframes_and_start_time(tmp_path):
    clip = tmp_path / "bad.mp4"
    _make_bframe_clip(clip, 720, 1280)
    issues = delivery_timing_issues(_probe_json(clip), target_fps=30)
    assert any("B-frames" in issue for issue in issues)


def test_final_review_flags_720x1264_for_tiktok_profile(tmp_path):
    bad = tmp_path / "bad.mp4"
    _make_clip(bad, 720, 1264, color="black")
    review = VideoCompose()._run_final_review(
        bad,
        edit_decisions={"metadata": {"platform": "tiktok"}},
        profile_name="tiktok_720p",
    )
    issues = "\n".join(review["checks"]["technical_probe"].get("issues", []))
    assert "Delivery geometry mismatch" in issues
    assert "720x1264" in issues
    assert "720x1280" in issues
    assert review["status"] in {"revise", "fail"}


def test_final_review_flags_bframe_tiktok_master(tmp_path):
    bad = tmp_path / "bad.mp4"
    _make_bframe_clip(bad, 720, 1280)
    review = VideoCompose()._run_final_review(
        bad,
        edit_decisions={"metadata": {"platform": "tiktok"}},
        profile_name="tiktok_720p",
    )
    issues = "\n".join(review["checks"]["technical_probe"].get("issues", []))
    assert "B-frames" in issues
    assert review["status"] in {"revise", "fail"}


def test_compose_target_720x1280_cover(tmp_path):
    src = tmp_path / "in.mp4"
    out = tmp_path / "out.mp4"
    _make_clip(src, 720, 1264, d=2.0, color="teal")
    ed = {
        "version": "1.0",
        "render_runtime": "ffmpeg",
        "cuts": [{"id": "c1", "source": str(src), "in_seconds": 0, "out_seconds": 1}],
        "metadata": {"compose_target": {"width": 720, "height": 1280, "fit": "cover"}},
    }
    result = VideoCompose().execute(
        {"operation": "compose", "edit_decisions": ed, "output_path": str(out)}
    )
    assert result.success, result.error
    assert _dims(out) == (720, 1280)
