import os

import pytest

from orchestrator.tools.files import (
    WorkspaceEscape,
    list_files,
    read_file,
    resolve_in_workspace,
    write_file,
)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world")
    return str(tmp_path)


def test_read_within_workspace(ws):
    assert read_file(ws, "a.txt") == "hello"
    assert read_file(ws, "sub/b.txt") == "world"


def test_dotdot_escape_rejected(ws):
    out = read_file(ws, "../outside.txt")
    assert out.startswith("ERROR")


def test_absolute_path_escape_rejected(ws):
    out = read_file(ws, "/etc/passwd")
    assert out.startswith("ERROR")


def test_symlink_escape_rejected(ws, tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOPSECRET")
    link = os.path.join(ws, "link.txt")
    os.symlink(secret, link)
    out = read_file(ws, "link.txt")
    assert out.startswith("ERROR")
    with pytest.raises(WorkspaceEscape):
        resolve_in_workspace(ws, "link.txt")


def test_size_cap_truncates(ws):
    big = "x" * 1000
    write_file(ws, "big.txt", big)
    out = read_file(ws, "big.txt", max_bytes=100)
    assert "[TRUNCATED" in out


def test_write_then_read_round_trip(ws):
    msg = write_file(ws, "new/nested.txt", "data")
    assert msg.startswith("OK")
    assert read_file(ws, "new/nested.txt") == "data"


def test_write_escape_rejected(ws):
    out = write_file(ws, "../evil.txt", "x")
    assert out.startswith("ERROR")


def test_list_files(ws):
    out = list_files(ws, ".")
    assert "a.txt" in out
    assert "sub/" in out
