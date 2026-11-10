"""V2 typed transfer tests: fake keys/transport/authority and local synthetic media."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import wave
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from lib import spicy_jobs as jobs, spicy_media as media, spicy_uploads as uploads

HOST = 'typed-fixture.r2.cloudflarestorage.com'
KEY = 'synthetic-v2-only-not-real'


def png():
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\0\0\0')) + chunk(b'IEND', b'')


def wav():
    out = io.BytesIO()
    with wave.open(out, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 800)
    return out.getvalue()


def expiry(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path.resolve()))
    monkeypatch.delenv('SPICY_API_KEY', raising=False)
    monkeypatch.setenv('SPICY_TYPED_FIXTURE', KEY)


def setup_upload(tmp_path, monkeypatch, *, media_kind='image', mime='image/png', payload=None,
                 failure=None, bad_commit=None, authority_change=None, duplicate=False):
    root = tmp_path.resolve()
    payload = payload or png()
    path = root / 'approved-source.bin'
    path.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    binding = jobs.credential_binding('env:SPICY_TYPED_FIXTURE')
    row = {'role': 'first_frame' if media_kind == 'image' else 'reference_' + media_kind,
           'pointer': '/image_url', 'field': 'image_url', 'media_kind': media_kind, 'mime': mime,
           'source_path': str(path), 'source_sha256': sha, 'bytes': len(payload), 'uri': None}
    rows = [row]
    if duplicate:
        rows.append(dict(row, role='last_frame', pointer='/last_image_url', field='last_image_url'))
    native = {'transport': 'spicy_native_jobs_v2', 'sources': rows}
    inputs = {'project_dir': str(root), 'model': 'fixture/native-video', 'native_input': {},
              'credential_ref': 'env:SPICY_TYPED_FIXTURE', 'account_label': 'fixture-account'}
    calls, reservations, put_bytes = [], [], []
    monkeypatch.setattr(jobs, 'build_native_request', lambda value: native)
    monkeypatch.setattr(media, 'probe_bytes', lambda data: {'mime': mime, 'media_kind': media_kind,
        'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'duration_seconds': None if media_kind == 'image' else 1,
        'width': 1 if media_kind != 'audio' else None, 'height': 1 if media_kind != 'audio' else None})
    def reserve(project, request):
        reservations.append(request)
        files = [{key: source[key] for key in ('role', 'pointer', 'media_kind', 'mime')} |
                 {'path': source['source_path'], 'sha256': source['source_sha256'], 'bytes': source['bytes']}
                 for source in rows if source.get('uri') is None]
        if authority_change:
            authority_change(files)
        return {'batch_id': 'typed-fixture-batch', 'account_label': 'fixture-account',
                'credential_ref': binding['credential_ref'], 'credential_digest': binding['credential_digest'],
                'qualified_storage_hosts': [HOST], 'files': files}
    monkeypatch.setattr('lib.production_autonomy.reserve_spicy_source_transfer', reserve)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))])
    def transport(method, url, headers, body, timeout=None):
        calls.append((method, url, dict(headers), body))
        if failure == 'ticket' and url.endswith('/upload-url'):
            raise TimeoutError('SYNTHETIC PRIVATE SIGNED MATERIAL')
        if url.endswith('/upload-url'):
            return 200, {}, json.dumps({'code': 200, 'data': {'fileId': 'typed-file', 'key': 'typed/key',
                'method': 'PUT', 'headers': {'Content-Type': mime}, 'maxBytes': len(payload),
                'uploadUrl': 'https://' + HOST + '/put?signature=SIGNED_SECRET_FIXTURE', 'expiresAt': expiry()}}).encode()
        if method == 'PUT':
            assert not any(key.lower() in ('authorization', 'cookie', 'proxy-authorization') for key in headers)
            put_bytes.append(body)
            if failure == 'put':
                raise TimeoutError('SYNTHETIC PRIVATE SIGNED MATERIAL')
            return 200, {}, b''
        if url.endswith('/commit'):
            if failure == 'commit':
                raise TimeoutError('SYNTHETIC PRIVATE SIGNED MATERIAL')
            data = {'fileId': 'typed-file', 'status': 'ready', 'bytes': len(payload), 'contentType': mime,
                    'sha256': sha, 'uri': 'spicy://f/typed-file', 'expiresAt': expiry(60)}
            data.update(bad_commit or {})
            return 200, {}, json.dumps({'code': 200, 'data': data}).encode()
        raise AssertionError('unexpected typed source fixture endpoint')
    client = jobs.SpicyClient(binding['credential_ref'], transport=transport, storage_transport=transport, qualified_storage_hosts=[HOST])
    return root, path, inputs, native, client, calls, reservations, put_bytes


@pytest.mark.parametrize('kind,mime,payload', [('image', 'image/png', png()), ('video', 'video/mp4', b'\0\0\0\x18ftypisom00000000'), ('audio', 'audio/wav', wav())], ids=['png','mp4','wav'])
def test_typed_upload_exact_bytes_private_journals_and_separate_v1_namespace(tmp_path, monkeypatch, kind, mime, payload):
    root, path, inputs, native, client, calls, reservations, puts = setup_upload(tmp_path, monkeypatch, media_kind=kind, mime=mime, payload=payload)
    receipts = uploads.upload_sources_v2(root, inputs, client=client)
    assert len(receipts) == 1 and receipts[0]['version'] == '2.0'
    assert receipts[0]['media_kind'] == kind and receipts[0]['content_type'] == mime
    assert receipts[0]['transfer_binding']['qualified_storage_hosts'] == [HOST]
    assert puts == [payload] and len(reservations) == 1
    assert len(calls) == 3
    files = list((root / 'artifacts').glob('*.json'))
    assert all(file.name.startswith('spicy_upload_v2') for file in files)
    text = '\n'.join(file.read_text() for file in files)
    assert KEY not in text and 'SIGNED_SECRET_FIXTURE' not in text and 'uploadUrl' not in text
    before = len(calls)
    assert uploads.upload_sources_v2(root, inputs, client=client) == receipts
    assert len(calls) == before and len(reservations) == 1


def test_cached_transfer_retains_original_hosts_and_rejects_malformed_history(tmp_path, monkeypatch):
    from lib.spicy_catalog import digest
    root, _, inputs, _, client, calls, _, _ = setup_upload(tmp_path, monkeypatch)
    receipt = uploads.upload_sources_v2(root, inputs, client=client)[0]
    client.qualified_storage_hosts = frozenset({'new-reviewed.r2.cloudflarestorage.com'})
    cached = uploads.upload_sources_v2(root, inputs, client=client)[0]
    assert cached['transfer_binding']['qualified_storage_hosts'] == [HOST]
    assert len(calls) == 3
    _, journal_path = uploads._v2_paths(root, receipt['source_sha256'], receipt['credential_digest'], 'image')
    journal = json.loads(journal_path.read_text())
    journal['binding']['qualified_storage_hosts'] = ['unqualified.invalid']
    journal['receipt']['transfer_binding']['qualified_storage_hosts'] = ['unqualified.invalid']
    journal['receipt']['transfer_binding_sha256'] = digest(journal['receipt']['transfer_binding'])
    uploads.atomic_write_json(journal_path, journal)
    with pytest.raises(jobs.SpicyError) as caught:
        uploads.usable_receipt_v2(root, receipt['source_sha256'], receipt['credential_digest'], 'image', 'image/png')
    assert caught.value.kind == 'upload_receipt_invalid'


def test_committed_journal_lookup_is_readonly_when_receipt_copy_is_missing(tmp_path, monkeypatch):
    root, _, inputs, _, client, calls, _, _ = setup_upload(tmp_path, monkeypatch)
    receipt = uploads.upload_sources_v2(root, inputs, client=client)[0]
    path, _ = uploads._v2_paths(root, receipt['source_sha256'], receipt['credential_digest'], 'image')
    path.unlink()
    before = {file: file.read_bytes() for file in root.rglob('*') if file.is_file()}
    assert uploads.usable_receipt_v2(root, receipt['source_sha256'], receipt['credential_digest'], 'image', 'image/png') == receipt
    assert {file: file.read_bytes() for file in root.rglob('*') if file.is_file()} == before
    assert len(calls) == 3


@pytest.mark.parametrize('failure', ['ticket', 'put', 'commit'])
def test_uncertain_v2_transfer_blocks_sibling_reticket(tmp_path, monkeypatch, failure):
    root, _, inputs, _, client, calls, _, _ = setup_upload(tmp_path, monkeypatch, failure=failure)
    with pytest.raises(jobs.SpicyError):
        uploads.upload_sources_v2(root, inputs, client=client)
    count = len(calls)
    with pytest.raises(jobs.SpicyError) as caught:
        uploads.upload_sources_v2(root, dict(inputs, compiled_request_id='different-preparation'), client=client)
    assert caught.value.kind == 'upload_original_unresolved'
    assert len(calls) == count
    assert 'SIGNED' not in str(caught.value)


@pytest.mark.parametrize('damage', [{'sha256': 'f' * 64}, {'bytes': True}, {'contentType': 'video/mp4'}])
def test_commit_proves_original_hash_mime_and_size(tmp_path, monkeypatch, damage):
    root, _, inputs, _, client, calls, _, _ = setup_upload(tmp_path, monkeypatch, bad_commit=damage)
    with pytest.raises(jobs.SpicyError) as caught:
        uploads.upload_sources_v2(root, inputs, client=client)
    assert caught.value.kind == 'upload_commit_invalid'
    count = len(calls)
    with pytest.raises(jobs.SpicyError):
        uploads.upload_sources_v2(root, inputs, client=client)
    assert len(calls) == count


def test_wrong_authority_pointer_and_source_drift_fail_before_ticket(tmp_path, monkeypatch):
    root, path, inputs, native, client, calls, _, _ = setup_upload(tmp_path, monkeypatch,
        authority_change=lambda rows: rows[0].update(pointer='/different_source'))
    with pytest.raises(jobs.SpicyError):
        uploads.upload_sources_v2(root, inputs, client=client)
    assert not calls
    path.write_bytes(png() + b'changed')
    with pytest.raises(jobs.SpicyError):
        uploads.upload_sources_v2(root, inputs, client=client)
    assert not calls


def test_duplicate_typed_roles_share_one_physical_transfer(tmp_path, monkeypatch):
    root, _, inputs, _, client, calls, reservations, _ = setup_upload(tmp_path, monkeypatch, duplicate=True)
    receipts = uploads.upload_sources_v2(root, inputs, client=client)
    assert len(receipts) == 2 and receipts[0] == receipts[1]
    assert len(calls) == 3 and len(reservations) == 1


def test_expired_v2_commit_and_symlink_receipt_never_reticket(tmp_path, monkeypatch):
    root, _, inputs, native, client, calls, _, _ = setup_upload(tmp_path, monkeypatch)
    receipt = uploads.upload_sources_v2(root, inputs, client=client)[0]
    monkeypatch.setattr(uploads, '_now', lambda: datetime.now(timezone.utc) + timedelta(hours=3))
    assert uploads.usable_receipt_v2(root, receipt['source_sha256'], receipt['credential_digest'], 'image', 'image/png') is None
    count = len(calls)
    with pytest.raises(jobs.SpicyError) as caught:
        uploads.upload_sources_v2(root, inputs, client=client)
    assert caught.value.kind == 'upload_expired_needs_fresh_authority' and len(calls) == count
    path, _ = uploads._v2_paths(root, receipt['source_sha256'], receipt['credential_digest'], 'image')
    saved = path.with_suffix('.original')
    path.rename(saved)
    path.symlink_to(saved)
    with pytest.raises(jobs.SpicyError) as caught:
        uploads.usable_receipt_v2(root, receipt['source_sha256'], receipt['credential_digest'], 'image', 'image/png')
    assert caught.value.kind == 'upload_receipt_invalid'


def test_probe_checks_container_format_not_only_sync_bytes(monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda name: '/fixture/ffprobe')
    def run(argv, **kwargs):
        assert argv[argv.index('-protocol_whitelist') + 1] == 'pipe'
        assert argv[argv.index('-i') + 1] == 'pipe:0'
        assert kwargs['input'].startswith(b'\xff\xf1')  # ADTS can pass the broad MPEG sync sniff.
        kwargs['stdout'].write(json.dumps({'streams': [{'codec_type': 'audio'}],
            'format': {'format_name': 'aac', 'duration': '1.0'}}).encode())
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(media.SpicyMediaError):
        media.probe_bytes(b'\xff\xf1' + b'fixture-aac')


def test_mov_conversion_and_unsafe_source_files_are_refused(tmp_path):
    with pytest.raises(media.SpicyMediaError) as caught:
        media.sniff(b'\0\0\0\x18ftypqt  00000000')
    assert caught.value.kind == 'source_conversion_unsupported'
    path = tmp_path / 'fifo'
    import os
    os.mkfifo(path, 0o600)
    with pytest.raises(media.SpicyMediaError):
        media.probe(path)
    path.unlink()
    real = tmp_path / 'real.png'
    real.write_bytes(png())
    path.symlink_to(real)
    with pytest.raises(media.SpicyMediaError):
        media.probe(path)


@pytest.mark.skipif(shutil.which('ffprobe') is None, reason='local ffprobe unavailable')
@pytest.mark.parametrize('payload,mime', [(png(), 'image/png'), (wav(), 'audio/wav')], ids=['png','wav'])
def test_real_local_synthetic_media_probe(payload, mime):
    proof = media.probe_bytes(payload)
    assert proof['mime'] == mime and proof['sha256'] == hashlib.sha256(payload).hexdigest()
    if mime == 'audio/wav':
        assert proof['duration_seconds'] == pytest.approx(0.1, abs=0.001)


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None, reason='local media tools unavailable')
@pytest.mark.parametrize('container,mime', [('mp3', 'audio/mpeg'), ('mp4', 'video/mp4'), ('webm', 'video/webm')])
def test_real_local_synthetic_mp3_mp4_webm_probe(container, mime):
    executable = shutil.which('ffmpeg')
    if container == 'mp3':
        arguments = ['-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '0.2', '-c:a', 'libmp3lame', '-f', 'mp3']
    else:
        arguments = ['-f', 'lavfi', '-i', 'color=c=black:s=16x16:d=0.2', '-an', '-c:v',
                     'mpeg4' if container == 'mp4' else 'libvpx-vp9']
        if container == 'mp4':
            arguments += ['-movflags', 'frag_keyframe+empty_moov']
        arguments += ['-f', container]
    process = subprocess.run([executable, '-v', 'error', *arguments, 'pipe:1'], capture_output=True, timeout=20)
    if process.returncode:
        pytest.skip('local synthetic encoder unavailable')
    proof = media.probe_bytes(process.stdout)
    assert proof['mime'] == mime
    assert 0 < proof['duration_seconds'] < 1


def test_media_size_duration_and_invalid_probe_output_are_sanitized(monkeypatch):
    monkeypatch.setattr(media, 'IMAGE_MAX_BYTES', len(png()) - 1)
    with pytest.raises(media.SpicyMediaError) as caught:
        media.probe_bytes(png())
    assert caught.value.kind == 'source_size_invalid'
    monkeypatch.setattr(shutil, 'which', lambda name: '/fixture/ffprobe')
    def oversized_duration(argv, **kwargs):
        kwargs['stdout'].write(json.dumps({'streams': [{'codec_type': 'audio'}],
            'format': {'format_name': 'mp3', 'duration': '601'}}).encode())
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, 'run', oversized_duration)
    with pytest.raises(media.SpicyMediaError) as caught:
        media.probe_bytes(b'ID3fixture-only')
    assert caught.value.kind == 'source_probe_invalid'
