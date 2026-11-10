"""The server's auth, SQL path and error handling, with no network."""
import json

import pytest
from starlette.testclient import TestClient

import server


@pytest.fixture(scope="module")
def client():
    # The MCP session manager runs once per process, so one app serves every test here.
    mp = pytest.MonkeyPatch()
    mp.setattr(server, "TOKENS", {"high-seas": "tok-hs", "other": "tok-o"})
    with TestClient(server.app()) as c:
        yield c
    mp.undo()


def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/healthz").status_code == 200


def test_mcp_needs_a_known_token(client):
    assert client.post("/mcp", json={}).status_code == 401
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.post("/mcp", headers={"Authorization": "Bearer tok-hs", "Accept": "application/json, text/event-stream"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert ok.status_code == 200
    assert "decide" in ok.text and "read_rows" in ok.text


def test_refuses_to_start_on_an_empty_token_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "TOKENS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DIR_TOKENS", {})
    with pytest.raises(SystemExit):
        server.app()


def test_read_rows_runs_locked_sql():
    out = json.loads(server.logged("read_rows", "x", lambda: json.dumps(
        dict(zip(["columns", "rows"], server.run_sql("SELECT 42 AS answer", 5))))))
    assert out == {"columns": ["answer"], "rows": [[42]]}


@pytest.mark.parametrize("sql,match", [
    ("SELECT * FROM read_text('/etc/passwd')", "Permission Error"),
    ("SET memory_limit='100GB'", "only SELECT"),
    ("CREATE SECRET e (TYPE s3, ENDPOINT 'evil.example.org')", "only SELECT"),
    ("SELECT * FROM duckdb_secrets()", "deployment"),
])
def test_unsafe_sql_is_refused(sql, match):
    with pytest.raises(RuntimeError, match=match):
        server.logged("read_rows", sql, lambda: server.run_sql(sql, 5))


def test_errors_are_redacted(monkeypatch):
    monkeypatch.setattr(server, "CREDENTIALS", ["AKIAEXAMPLEKEY"])
    def boom():
        raise OSError("InvalidAccessKeyId: AKIAEXAMPLEKEY")
    with pytest.raises(RuntimeError) as e:
        server.logged("read_rows", "x", boom)
    assert "AKIAEXAMPLEKEY" not in str(e.value) and "[REDACTED]" in str(e.value)
    assert server.logged("read_rows", "x", lambda: "found AKIAEXAMPLEKEY") == "found [REDACTED]"
