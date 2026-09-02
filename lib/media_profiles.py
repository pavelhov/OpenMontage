"""Internal media profile constants and render-profile helpers.

Defines platform-specific media profiles (resolution, aspect ratio, codec, etc.)
so the composer and publisher agents can format output correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AspectRatio(str, Enum):
    LANDSCAPE_16_9 = "16:9"
    PORTRAIT_9_16 = "9:16"
    SQUARE_1_1 = "1:1"
    CINEMATIC_21_9 = "21:9"
    STANDARD_4_3 = "4:3"


@dataclass(frozen=True)
class MediaProfile:
    """A named render profile for a target platform/format."""
    name: str
    width: int
    height: int
    aspect_ratio: AspectRatio
    fps: int
    codec: str
    audio_codec: str
    crf: int
    pixel_format: str = "yuv420p"
    max_file_size_mb: Optional[float] = None
    max_duration_seconds: Optional[float] = None
    caption_format: str = "srt"
    notes: str = ""


# ---- Platform profiles ----

YOUTUBE_LANDSCAPE = MediaProfile(
    name="youtube_landscape",
    width=1920, height=1080,
    aspect_ratio=AspectRatio.LANDSCAPE_16_9,
    fps=30, codec="libx264", audio_codec="aac", crf=18,
    caption_format="srt",
    notes="YouTube standard HD upload",
)

YOUTUBE_4K = MediaProfile(
    name="youtube_4k",
    width=3840, height=2160,
    aspect_ratio=AspectRatio.LANDSCAPE_16_9,
    fps=30, codec="libx264", audio_codec="aac", crf=18,
    caption_format="srt",
    notes="YouTube 4K upload",
)

YOUTUBE_SHORTS = MediaProfile(
    name="youtube_shorts",
    width=1080, height=1920,
    aspect_ratio=AspectRatio.PORTRAIT_9_16,
    fps=30, codec="libx264", audio_codec="aac", crf=20,
    max_duration_seconds=60,
    caption_format="srt",
    notes="YouTube Shorts (max 60s, vertical)",
)

INSTAGRAM_REELS = MediaProfile(
    name="instagram_reels",
    width=1080, height=1920,
    aspect_ratio=AspectRatio.PORTRAIT_9_16,
    fps=30, codec="libx264", audio_codec="aac", crf=20,
    max_file_size_mb=250,
    max_duration_seconds=90,
    caption_format="srt",
    notes="Instagram Reels (max 90s, vertical)",
)

INSTAGRAM_FEED = MediaProfile(
    name="instagram_feed",
    width=1080, height=1080,
    aspect_ratio=AspectRatio.SQUARE_1_1,
    fps=30, codec="libx264", audio_codec="aac", crf=20,
    max_file_size_mb=250,
    max_duration_seconds=60,
    notes="Instagram feed video (square)",
)

TIKTOK = MediaProfile(
    name="tiktok",
    width=1080, height=1920,
    aspect_ratio=AspectRatio.PORTRAIT_9_16,
    fps=30, codec="libx264", audio_codec="aac", crf=20,
    max_file_size_mb=287,
    max_duration_seconds=600,
    caption_format="srt",
    notes="TikTok (max 10min, vertical preferred)",
)

TIKTOK_720P = MediaProfile(
    name="tiktok_720p",
    width=720, height=1280,
    aspect_ratio=AspectRatio.PORTRAIT_9_16,
    fps=30, codec="libx264", audio_codec="aac", crf=20,
    max_file_size_mb=287,
    max_duration_seconds=600,
    caption_format="srt",
    notes="TikTok-safe 720p vertical master (exact 9:16). Prefer for Grok 720p stitches.",
)

LINKEDIN = MediaProfile(
    name="linkedin",
    width=1920, height=1080,
    aspect_ratio=AspectRatio.LANDSCAPE_16_9,
    fps=30, codec="libx264", audio_codec="aac", crf=20,
    max_file_size_mb=5120,
    max_duration_seconds=600,
    caption_format="srt",
    notes="LinkedIn video (landscape preferred, max 10min)",
)

CINEMATIC = MediaProfile(
    name="cinematic",
    width=2560, height=1080,
    aspect_ratio=AspectRatio.CINEMATIC_21_9,
    fps=24, codec="libx264", audio_codec="aac", crf=16,
    notes="Cinematic ultra-wide format",
)

GENERIC_HD = MediaProfile(
    name="generic_hd",
    width=1920, height=1080,
    aspect_ratio=AspectRatio.LANDSCAPE_16_9,
    fps=30, codec="libx264", audio_codec="aac", crf=23,
    caption_format="srt",
    notes="Generic HD output (no platform-specific constraints)",
)


# ---- Profile registry ----

ALL_PROFILES: dict[str, MediaProfile] = {
    p.name: p for p in [
        YOUTUBE_LANDSCAPE, YOUTUBE_4K, YOUTUBE_SHORTS,
        INSTAGRAM_REELS, INSTAGRAM_FEED,
        TIKTOK, TIKTOK_720P, LINKEDIN, CINEMATIC, GENERIC_HD,
    ]
}


def get_profile(name: str) -> MediaProfile:
    """Get a media profile by name."""
    if name not in ALL_PROFILES:
        available = ", ".join(ALL_PROFILES.keys())
        raise ValueError(f"Unknown profile {name!r}. Available: {available}")
    return ALL_PROFILES[name]


def get_profiles_for_platform(platform: str) -> list[MediaProfile]:
    """Get all profiles matching a platform prefix."""
    return [p for name, p in ALL_PROFILES.items() if name.startswith(platform)]


def ffmpeg_output_args(profile: MediaProfile) -> list[str]:
    """Generate FFmpeg output arguments for a media profile."""
    args = [
        "-c:v", profile.codec,
        "-c:a", profile.audio_codec,
        "-crf", str(profile.crf),
        "-pix_fmt", profile.pixel_format,
        "-r", str(profile.fps),
        "-vf", f"scale={profile.width}:{profile.height}",
    ]
    return args


# ---- Delivery geometry helpers ----

GEOMETRY_PIXEL_TOLERANCE = 8
GEOMETRY_RATIO_TOLERANCE = 0.005
# Near-9:16 auto-snap window. 720x1264 is ~0.71% off exact 9:16, so this
# must be wider than the exact-match ratio tolerance above.
NEAR_9_16_RATIO_TOLERANCE = 0.015
_PORTRAIT_9_16 = 9 / 16
_SOCIAL_VERTICAL_HINTS = {
    "tiktok",
    "instagram",
    "instagram_reels",
    "reels",
    "shorts",
    "youtube_shorts",
    "vertical",
    "9:16",
}


def even_dimension(value: int) -> int:
    """Round down to an even pixel count for yuv420p-safe geometry."""
    return int(value) - (int(value) % 2)


def dimensions_match(
    width: int,
    height: int,
    target_width: int,
    target_height: int,
    *,
    pixel_tolerance: int = GEOMETRY_PIXEL_TOLERANCE,
) -> bool:
    """Return True when observed dims are within pixel tolerance of the target."""
    return (
        abs(int(width) - int(target_width)) <= pixel_tolerance
        and abs(int(height) - int(target_height)) <= pixel_tolerance
    )


def aspect_ratio_delta(width: int, height: int, target_ratio: float = _PORTRAIT_9_16) -> float:
    """Absolute delta between observed width/height and a target ratio."""
    if height <= 0:
        return 1.0
    return abs((float(width) / float(height)) - float(target_ratio))


def is_near_portrait_9_16(
    width: int,
    height: int,
    *,
    ratio_tolerance: float = NEAR_9_16_RATIO_TOLERANCE,
) -> bool:
    """True for portrait frames near exact 9:16 (auto-snap candidate).

    Uses a wider window than exact delivery matching so photo-true Grok
    image_to_video masters like 720x1264 still snap to 720x1280.
    """
    return (
        int(width) > 0
        and int(height) > int(width)
        and aspect_ratio_delta(width, height) <= ratio_tolerance
    )


def snap_portrait_9_16(width: int, height: int) -> tuple[int, int]:
    """Snap a near-vertical frame to exact 9:16 at a video-safe short edge.

    Prefers common delivery sizes used by Grok/TikTok masters:
    - short edge around 720 → 720x1280
    - short edge around 1080 → 1080x1920
    Otherwise snaps to the nearest of those common sizes, then exact 9:16.
    """
    short_edge = even_dimension(min(int(width), int(height)))
    # Prefer the nearest common social short-edge rather than leaving a dead
    # band (e.g. 941px keyframes) that would otherwise keep off-geometry.
    if 640 <= short_edge <= 1200:
        target_short = 720 if abs(short_edge - 720) <= abs(short_edge - 1080) else 1080
        return target_short, int(target_short * 16 / 9)
    target_h = even_dimension(int(round(short_edge * 16 / 9)))
    return short_edge, max(target_h, short_edge + 2)


def default_fit_for_aspect(aspect_ratio: AspectRatio | str | None) -> str:
    """Vertical social defaults to cover/crop; everything else pads."""
    value = aspect_ratio.value if isinstance(aspect_ratio, AspectRatio) else aspect_ratio
    if value == AspectRatio.PORTRAIT_9_16.value or value == "9:16":
        return "cover"
    return "pad"


def ffmpeg_geometry_filter(
    width: int,
    height: int,
    *,
    fit: str = "pad",
) -> str:
    """Build an ffmpeg scale+pad/crop filter for exact delivery geometry."""
    if fit == "cover":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def resolve_delivery_geometry(
    *,
    profile_name: str | None = None,
    target_resolution: str | None = None,
    compose_target: dict | None = None,
    platform_hint: str | None = None,
    observed_width: int | None = None,
    observed_height: int | None = None,
    auto_snap_near_9_16: bool = False,
) -> dict | None:
    """Resolve the exact delivery geometry OpenMontage should enforce.

    Priority:
      1. named media profile
      2. explicit WxH target / compose_target
      3. social platform hint → tiktok_720p when observed short edge ~720, else tiktok
      4. optional auto-snap of near-9:16 portrait observations (stitch safety net)

    Returns None when no delivery geometry should be forced.
    """
    if profile_name:
        try:
            profile = get_profile(profile_name)
        except ValueError:
            profile = None
        if profile is not None:
            return {
                "width": profile.width,
                "height": profile.height,
                "fit": default_fit_for_aspect(profile.aspect_ratio),
                "fps": profile.fps,
                "codec": profile.codec,
                "audio_codec": profile.audio_codec,
                "source": f"profile:{profile.name}",
                "profile": profile.name,
            }

    if isinstance(compose_target, dict):
        try:
            width = int(compose_target["width"])
            height = int(compose_target["height"])
        except (KeyError, TypeError, ValueError):
            width = height = 0
        if width > 0 and height > 0:
            fit = compose_target.get("fit")
            if fit not in ("pad", "cover"):
                fit = default_fit_for_aspect(
                    "9:16" if height > width else "16:9"
                )
            return {
                "width": width,
                "height": height,
                "fit": fit,
                "source": "compose_target",
            }

    if target_resolution:
        parts = str(target_resolution).lower().split("x")
        if len(parts) == 2:
            try:
                width = int(parts[0])
                height = int(parts[1])
            except ValueError:
                width = height = 0
            if width > 0 and height > 0:
                return {
                    "width": width,
                    "height": height,
                    "fit": default_fit_for_aspect(
                        "9:16" if height > width else "16:9"
                    ),
                    "source": "target_resolution",
                }

    hint = str(platform_hint or "").strip().lower().replace(" ", "_")
    if hint in _SOCIAL_VERTICAL_HINTS or any(token in hint for token in _SOCIAL_VERTICAL_HINTS):
        if observed_width and observed_height and min(observed_width, observed_height) <= 800:
            profile = get_profile("tiktok_720p")
        else:
            profile = get_profile("tiktok")
        return {
            "width": profile.width,
            "height": profile.height,
            "fit": default_fit_for_aspect(profile.aspect_ratio),
            "fps": profile.fps,
            "codec": profile.codec,
            "audio_codec": profile.audio_codec,
            "source": f"platform_hint:{hint or 'vertical'}",
            "profile": profile.name,
        }

    if (
        auto_snap_near_9_16
        and observed_width
        and observed_height
        and is_near_portrait_9_16(observed_width, observed_height)
        and not dimensions_match(
            observed_width,
            observed_height,
            *snap_portrait_9_16(observed_width, observed_height),
        )
    ):
        width, height = snap_portrait_9_16(observed_width, observed_height)
        return {
            "width": width,
            "height": height,
            "fit": "cover",
            "source": "auto_snap_near_9_16",
        }

    return None


def delivery_geometry_issue(
    width: int,
    height: int,
    target: dict | None,
) -> str | None:
    """Return a blocking issue string when output misses the delivery geometry."""
    if not target:
        return None
    target_w = int(target["width"])
    target_h = int(target["height"])
    if dimensions_match(width, height, target_w, target_h):
        return None
    source = target.get("source", "delivery_geometry")
    return (
        f"Delivery geometry mismatch: rendered {int(width)}x{int(height)} "
        f"but target is {target_w}x{target_h} ({source}). "
        "Normalize/re-encode before shipping; do not concat-copy off-geometry clips."
    )
