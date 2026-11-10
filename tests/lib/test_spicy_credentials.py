"""Offline local-file credentials and GET-only diagnostics; never real secrets."""
import copy
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from lib import spicy_catalog as catalog
from lib import spicy_credentials as credentials
from lib import spicy_jobs as jobs

SECRET = "synthetic-offline-first"
SECOND = "synthetic-offline-second"


@pytest.fixture(autouse=True)
def isolated_home_and_network(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SPICY_API_KEY", raising=False)
    monkeypatch.delenv("SPICY_CUSTOM_FIXTURE", raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("Credential tests must not use a real network")
    monkeypatch.setattr(socket, "socket", forbidden)


def config_file(tmp_path, content=None):
    directory = tmp_path / ".config" / "spicyapi"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / "credentials.env"
    path.write_bytes(content if content is not None else f"SPICY_API_KEY={SECRET}\n".encode())
    path.chmod(0o600)
    return path


def test_default_file_keeps_existing_reference_and_digest_without_mutating_env(tmp_path):
    config_file(tmp_path)
    before = dict(os.environ)
    binding = jobs.credential_binding("env:SPICY_API_KEY")
    assert binding == {"credential_ref": "env:SPICY_API_KEY", "credential_env": "SPICY_API_KEY",
        "credential_digest": hashlib.sha256(jobs.CREDENTIAL_DOMAIN + SECRET.encode()).hexdigest()}
    assert dict(os.environ) == before
    assert SECRET not in repr(jobs.SpicyClient("env:SPICY_API_KEY"))


def test_environment_present_wins_and_preserves_engine_strip_normalization(tmp_path, monkeypatch):
    config_file(tmp_path, b"invalid file must not be opened")
    monkeypatch.setenv("SPICY_API_KEY", "  " + SECOND + "  ")
    assert credentials.resolve_credential("SPICY_API_KEY") == SECOND


@pytest.mark.parametrize("value", ["", " \t "])
def test_present_empty_environment_never_falls_back(tmp_path, monkeypatch, value):
    config_file(tmp_path)
    monkeypatch.setenv("SPICY_API_KEY", value)
    with pytest.raises(jobs.SpicyError) as error:
        jobs.credential_binding("env:SPICY_API_KEY")
    assert error.value.kind == "credential_missing"


def test_custom_environment_reference_never_falls_back(tmp_path, monkeypatch):
    config_file(tmp_path)
    with pytest.raises(jobs.SpicyError) as error:
        jobs.credential_binding("env:SPICY_CUSTOM_FIXTURE")
    assert error.value.kind == "credential_missing"
    monkeypatch.setenv("SPICY_CUSTOM_FIXTURE", SECOND)
    assert credentials.resolve_credential("SPICY_CUSTOM_FIXTURE") == SECOND


@pytest.mark.parametrize("text", [
    f"SPICY_API_KEY={SECRET}", f"  SPICY_API_KEY = '{SECRET}'  \n",
    f'\t# local comment\n\n SPICY_API_KEY\t=\t"{SECRET}"\n',
])
def test_literal_file_parser_accepts_formatting_and_matching_quotes(text):
    assert credentials.parse_credential_file(text.encode()) == SECRET


@pytest.mark.parametrize("text", [
    "", "# no assignment", "OTHER_KEY=fixture", "export SPICY_API_KEY=fixture",
    "SPICY_API_KEY=", "SPICY_API_KEY='a\nb'", "SPICY_API_KEY='mismatched\"",
    "SPICY_API_KEY=a b", "SPICY_API_KEY=foo # comment", "SPICY_API_KEY=${HOME}",
    "SPICY_API_KEY=$(id)", "SPICY_API_KEY=`id`", "SPICY_API_KEY=foo;id",
    "SPICY_API_KEY=foo\\bar", "SPICY_API_KEY=foo\r\n", "SPICY_API_KEY=foo\x00",
    "SPICY_API_KEY=foo\x85", "# \x85\nSPICY_API_KEY=foo",
    "SPICY_API_KEY=foo\nSPICY_API_KEY=bar", "SPICY_API_KEY=foo\nOTHER_KEY=bar",
])
def test_literal_file_parser_rejects_shell_ambiguous_or_control_values_without_leaks(text):
    with pytest.raises(credentials.SpicyCredentialError) as error:
        credentials.parse_credential_file(text.encode())
    assert str(error.value) == "Spicy credential file is invalid"
    assert error.value.__cause__ is None


def test_shell_input_never_executes(tmp_path):
    marker = tmp_path / "must-not-exist"
    config_file(tmp_path, f"SPICY_API_KEY=$(touch {marker})\n".encode())
    with pytest.raises(jobs.SpicyError):
        jobs.credential_binding("env:SPICY_API_KEY")
    assert not marker.exists()


@pytest.mark.parametrize("content", [b"\xff", b"#" * (credentials.MAX_FILE_BYTES + 1)])
def test_file_encoding_and_size_fail_without_secret_output(tmp_path, content):
    config_file(tmp_path, content)
    with pytest.raises(jobs.SpicyError) as error:
        jobs.credential_binding("env:SPICY_API_KEY")
    assert SECRET not in str(error.value)
    assert content.decode(errors="replace") not in str(error.value)


def test_invalid_utf8_traceback_cannot_expose_raw_credential():
    import traceback
    try:
        credentials.parse_credential_file(("SPICY_API_KEY=" + SECRET).encode() + b"\xff")
    except credentials.SpicyCredentialError as error:
        rendered = "".join(traceback.format_exception(error))
        assert SECRET not in rendered
        assert "UnicodeDecodeError" not in rendered


@pytest.mark.parametrize("target,mode", [("file", 0o644), ("file", 0o620), ("dir", 0o755), ("dir", 0o710)])
def test_owner_only_file_and_directory_required(tmp_path, target, mode):
    path = config_file(tmp_path)
    (path if target == "file" else path.parent).chmod(mode)
    with pytest.raises(jobs.SpicyError):
        jobs.credential_binding("env:SPICY_API_KEY")


def test_foreign_owner_is_refused(tmp_path, monkeypatch):
    config_file(tmp_path)
    actual_uid = os.getuid()
    monkeypatch.setattr(credentials.os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(jobs.SpicyError):
        jobs.credential_binding("env:SPICY_API_KEY")


@pytest.mark.parametrize("target", ["file", "dir"])
def test_credential_leaf_and_immediate_directory_symlinks_refused(tmp_path, target):
    path = config_file(tmp_path)
    original = path if target == "file" else path.parent
    moved = original.with_name(original.name + "-original")
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=target == "dir")
    with pytest.raises(jobs.SpicyError):
        jobs.credential_binding("env:SPICY_API_KEY")


def test_fifo_refused_without_waiting_for_writer(tmp_path):
    path = config_file(tmp_path)
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(jobs.SpicyError):
        jobs.credential_binding("env:SPICY_API_KEY")


def test_directory_in_place_of_file_is_refused(tmp_path):
    path = config_file(tmp_path)
    path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(jobs.SpicyError):
        jobs.credential_binding("env:SPICY_API_KEY")


def test_missing_config_is_secret_free(tmp_path):
    with pytest.raises(jobs.SpicyError) as error:
        jobs.credential_binding("env:SPICY_API_KEY")
    assert error.value.kind == "credential_missing"
    assert str(tmp_path) not in str(error.value)


def test_file_change_during_read_is_refused(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    original_read = os.read
    changed = False
    def mutate(fd, count):
        nonlocal changed
        result = original_read(fd, count)
        if not changed:
            changed = True
            path.write_text(f"SPICY_API_KEY={SECOND}\n")
        return result
    monkeypatch.setattr(credentials.os, "read", mutate)
    with pytest.raises(jobs.SpicyError, match="changed while being read"):
        jobs.credential_binding("env:SPICY_API_KEY")


def test_directory_fd_pins_file_if_directory_name_changes(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    real_open = os.open
    moved = path.parent.with_name("spicyapi-original")
    def replace_name(name, flags, **kwargs):
        if name == "credentials.env":
            path.parent.rename(moved)
            path.parent.symlink_to(moved, target_is_directory=True)
        return real_open(name, flags, **kwargs)
    monkeypatch.setattr(credentials.os, "open", replace_name)
    assert credentials.resolve_credential("SPICY_API_KEY") == SECRET


def test_file_rotation_is_seen_before_next_request_and_existing_binding_cannot_change(tmp_path):
    path = config_file(tmp_path)
    calls = []
    client = jobs.SpicyClient("env:SPICY_API_KEY", transport=lambda *args: calls.append(args))
    original = dict(client.binding)
    path.write_text(f"SPICY_API_KEY={SECOND}\n")
    assert jobs.credential_binding("env:SPICY_API_KEY") != original
    with pytest.raises(jobs.SpicyError) as error:
        client.list_models()
    assert error.value.kind == "credential_changed"
    assert not calls


def model_item():
    route = catalog.VIDEO_ROUTES[catalog.VIDEO_MODEL]
    return {"model": catalog.VIDEO_MODEL, "modality": "video", "enabled": True, "available": True,
        "inputSchema": {"type": "object", "properties": copy.deepcopy(route["properties"]),
                        "required": list(route["required"]), "additionalProperties": False}}


def diagnostic_client(items, credit=None):
    calls = []
    def transport(method, url, headers, body, timeout):
        assert method == "GET" and body is None
        assert headers["Authorization"] == "Bearer " + SECRET
        calls.append(url)
        if url.endswith("/chat/credit"):
            data = credit if credit is not None else {"available": "10.00", "held": "0.25", "total": "10.25",
                "provider_private": SECRET}
        else:
            assert url == jobs.API_BASE + "/models?includeSchema=1"
            data = {"items": items, "provider_private": SECRET}
        return 200, {}, json.dumps({"code": 200, "msg": SECRET, "data": data}).encode()
    return jobs.SpicyClient("env:SPICY_API_KEY", transport=transport), calls


def test_diagnostic_is_two_gets_only_and_writes_no_authority_or_records(tmp_path):
    config_file(tmp_path)
    item = model_item()
    item["provider_private"] = SECRET
    client, calls = diagnostic_client([item, {"model": SECRET, "enabled": False, "available": True}])
    before = set(tmp_path.rglob("*"))
    result = jobs.connection_check(client=client)
    assert calls == [jobs.API_BASE + "/chat/credit", jobs.API_BASE + "/models?includeSchema=1"]
    assert result["balance_usd"] == {"available": "10.00", "held": "0.25", "total": "10.25"}
    assert result["catalogue"] == {"total": 2, "enabled": 1, "available": 2, "video_enabled_available": 1}
    assert result["video_route"]["callable"] is True
    assert result["authority"] == "none" and result["production_observation_written"] is False
    assert SECRET not in json.dumps(result)
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("damage", ["absent", "disabled", "unavailable", "schema", "truthy"])
def test_diagnostic_does_not_claim_exact_route_callability_without_live_qualification(tmp_path, damage):
    config_file(tmp_path)
    item = model_item()
    if damage == "disabled": item["enabled"] = False
    if damage == "unavailable": item["available"] = False
    if damage == "schema": item["inputSchema"] = {}
    if damage == "truthy": item["enabled"] = "true"
    client, _ = diagnostic_client([] if damage == "absent" else [item])
    assert jobs.connection_check(client=client)["video_route"]["callable"] is False


@pytest.mark.parametrize("credit", [{"available": SECRET}, {"available": "NaN", "held": "0", "total": "0"}])
def test_diagnostic_balance_rejection_is_sanitized(tmp_path, credit):
    config_file(tmp_path)
    client, _ = diagnostic_client([model_item()], credit)
    with pytest.raises(jobs.SpicyError) as error:
        jobs.connection_check(client=client)
    assert SECRET not in str(error.value)


def test_diagnostic_duplicate_or_malformed_models_fail_closed(tmp_path):
    config_file(tmp_path)
    for items in ([model_item(), model_item()], [SECRET], {}):
        client, _ = diagnostic_client(items)
        with pytest.raises(jobs.SpicyError) as error:
            jobs.connection_check(client=client)
        assert error.value.kind == "invalid_catalog"


def test_diagnostic_signed_credit_balance_is_observation_only(tmp_path):
    config_file(tmp_path)
    credit = {"available": "-0.25", "held": "1.00", "total": "-0.10"}
    client, _ = diagnostic_client([model_item()], credit)
    result = jobs.connection_check(client=client)
    assert result["balance_usd"] == credit
    assert result["authority"] == "none"
    client, _ = diagnostic_client([model_item()], dict(credit, held="-0.01"))
    with pytest.raises(jobs.SpicyError) as error:
        jobs.connection_check(client=client)
    assert error.value.kind == "invalid_balance"
