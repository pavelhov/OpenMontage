"""Bounded local source-byte qualification for native Spicy v2 transfers.

No conversion, URL resolution or provider calls. ffprobe reads the frozen byte
snapshot through a pipe-only protocol whitelist; returned errors contain no paths,
media metadata blobs, credentials or subprocess output.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

IMAGE_MAX_BYTES = 10 * 1024 * 1024
MEDIA_MAX_BYTES = 90 * 1024 * 1024
MAX_DURATION_SECONDS = 600
MIMES = {'image/png': 'image', 'image/jpeg': 'image', 'image/webp': 'image', 'image/gif': 'image',
         'video/mp4': 'video', 'video/webm': 'video', 'audio/mpeg': 'audio', 'audio/wav': 'audio'}
FORMATS = {'image/png': {'png_pipe'}, 'image/jpeg': {'jpeg_pipe'}, 'image/webp': {'webp_pipe'},
           'image/gif': {'gif'}, 'video/mp4': {'mp4'}, 'video/webm': {'webm'},
           'audio/mpeg': {'mp3'}, 'audio/wav': {'wav'}}


class SpicyMediaError(ValueError):
    def __init__(self, message, *, kind='source_media_invalid'):
        super().__init__(message)
        self.kind = kind


def _fail(message='Spicy source media is unsupported or invalid', kind='source_media_invalid'):
    raise SpicyMediaError(message, kind=kind) from None


def sniff(data: bytes) -> str:
    if not isinstance(data, bytes):
        _fail()
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF':
        if data[8:12] == b'WEBP':
            return 'image/webp'
        if data[8:12] == b'WAVE':
            return 'audio/wav'
    if data.startswith(b'ID3') or (len(data) >= 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0):
        return 'audio/mpeg'
    if len(data) >= 16 and data[4:8] == b'ftyp':
        if b'qt  ' in data[8:64]:
            _fail('MOV source conversion is unsupported; original-byte proof is required', 'source_conversion_unsupported')
        return 'video/mp4'
    if data.startswith(b'\x1aE\xdf\xa3') and b'webm' in data[:4096]:
        return 'video/webm'
    _fail('Spicy source MIME signature is unsupported', 'source_mime_invalid')


def frozen_bytes(path) -> bytes:
    """Read a regular no-follow local file once, refusing concurrent in-place edits."""
    fd = None
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MEDIA_MAX_BYTES:
            _fail('Spicy source is not a bounded regular media file', 'source_size_invalid')
        chunks, remaining = [], MEDIA_MAX_BYTES + 1
        while remaining:
            block = os.read(fd, min(remaining, 1024 * 1024))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, key) != getattr(after, key) for key in fields):
            _fail('Spicy source changed during its byte snapshot', 'source_changed')
        data = b''.join(chunks)
        if len(data) != before.st_size or len(data) > MEDIA_MAX_BYTES:
            _fail('Spicy source exceeds its byte snapshot limit', 'source_size_invalid')
        return data
    except OSError:
        _fail('Spicy source could not be read as a safe local file', 'source_file_invalid')
    finally:
        if fd is not None:
            os.close(fd)


def probe_bytes(data: bytes) -> dict:
    mime = sniff(data)
    kind = MIMES[mime]
    if not data or len(data) > (IMAGE_MAX_BYTES if kind == 'image' else MEDIA_MAX_BYTES):
        _fail('Spicy source exceeds the documented media byte limit', 'source_size_invalid')
    executable = shutil.which('ffprobe')
    if not executable:
        _fail('ffprobe is required for native Spicy typed sources', 'source_probe_unavailable')
    argv = [executable, '-v', 'error', '-protocol_whitelist', 'pipe', '-i', 'pipe:0',
            '-show_packets', '-show_entries',
            'packet=stream_index,duration_time:stream=index,codec_type,width,height,duration:format=format_name,duration', '-of', 'json']
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.run(argv, input=data, stdout=output, stderr=subprocess.DEVNULL,
                                     timeout=30, check=False)
            if process.returncode != 0 or output.tell() > 8 * 1024 * 1024:
                _fail('Spicy source probing failed', 'source_probe_invalid')
            output.seek(0)
            raw_metadata = output.read(8 * 1024 * 1024 + 1)
    except (OSError, subprocess.SubprocessError):
        _fail('Spicy source probing failed', 'source_probe_invalid')
    if len(raw_metadata) > 8 * 1024 * 1024:
        _fail('Spicy source probing failed', 'source_probe_invalid')
    try:
        metadata = json.loads(raw_metadata)
        format_name = metadata.get('format', {}).get('format_name')
        if not isinstance(format_name, str) or not FORMATS[mime].intersection(format_name.split(',')):
            raise ValueError()
        streams = metadata['streams']
        if not isinstance(streams, list) or not streams or any(not isinstance(stream, dict) for stream in streams):
            raise ValueError()
        videos = [stream for stream in streams if stream.get('codec_type') == 'video']
        audios = [stream for stream in streams if stream.get('codec_type') == 'audio']
        width = height = duration = None
        if kind in ('video', 'image'):
            if len(videos) != 1:
                raise ValueError()
            width, height = videos[0]['width'], videos[0]['height']
            if (type(width) is not int or type(height) is not int or not 0 < width <= 16384
                    or not 0 < height <= 16384 or width * height > 80_000_000):
                raise ValueError()
        if kind == 'audio' and (not audios or videos):
            raise ValueError()
        if kind in ('video', 'audio'):
            durations = []
            raw_durations = [metadata.get('format', {}).get('duration')] + [stream.get('duration') for stream in streams]
            for raw in raw_durations:
                if raw not in (None, 'N/A'):
                    if isinstance(raw, bool):
                        raise ValueError()
                    durations.append(float(raw))
            packet_durations = {}
            for packet in metadata.get('packets', []):
                raw = packet.get('duration_time')
                if raw not in (None, 'N/A'):
                    value, index = float(raw), packet['stream_index']
                    if type(index) is not int or not math.isfinite(value) or value < 0:
                        raise ValueError()
                    packet_durations[index] = packet_durations.get(index, 0) + value
            durations += list(packet_durations.values())
            if not durations or any(not math.isfinite(value) or value < 0 for value in durations):
                raise ValueError()
            duration = max(durations)
            if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS:
                raise ValueError()
    except (ValueError, KeyError, TypeError, OverflowError):
        _fail('Spicy source has invalid dimensions, streams or duration', 'source_probe_invalid')
    return {'mime': mime, 'media_kind': kind, 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'duration_seconds': duration,
            'width': width, 'height': height}


def probe(path) -> dict:
    return probe_bytes(frozen_bytes(path))
