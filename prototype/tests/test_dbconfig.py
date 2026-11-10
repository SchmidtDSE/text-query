"""The locked DuckDB connection: what caller SQL can and cannot do."""
import duckdb
import pytest

import dbconfig
from dbconfig import check_select, lock_down, redact, setup_sql


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in ("DUCKDB_MEMORY_LIMIT", "POD_MEMORY_LIMIT", "DUCKDB_MAX_TEMP_DIRECTORY_SIZE",
                "POD_EPHEMERAL_LIMIT", "DUCKDB_TEMP_ROOT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_TEMP_ROOT", str(tmp_path))
    c = duckdb.connect()
    for stmt in setup_sql():
        c.execute(stmt)
    c.execute("LOAD httpfs")
    c.execute("LOAD json")
    # An s3 secret on an unreachable http endpoint, as the in-cluster rustfs secret is.
    c.execute("CREATE SECRET (TYPE s3, KEY_ID 'AKIAEXAMPLEKEY', SECRET 'examplesecretvalue', "
              "ENDPOINT '127.0.0.1:9', USE_SSL false, URL_STYLE 'path')")
    lock_down(c)
    yield c
    c.close()


def test_memory_and_spill_follow_pod_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("POD_MEMORY_LIMIT", str(1024 * 2**20))
    monkeypatch.setenv("POD_EPHEMERAL_LIMIT", "10Gi")
    monkeypatch.setenv("DUCKDB_TEMP_ROOT", str(tmp_path))
    stmts = setup_sql()
    assert stmts[0] == "SET allow_persistent_secrets=false"
    assert "SET memory_limit='819MiB'" in stmts
    assert "SET max_temp_directory_size='5120MiB'" in stmts
    assert any(s.startswith(f"SET temp_directory='{tmp_path}/duckdb-") for s in stmts)


def test_each_connection_spills_to_its_own_directory():
    a = [s for s in setup_sql() if "temp_directory=" in s and "max_" not in s]
    b = [s for s in setup_sql() if "temp_directory=" in s and "max_" not in s]
    assert a != b


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_text('/etc/passwd')",
    "SELECT * FROM read_text('/var/run/secrets/kubernetes.io/serviceaccount/token')",
    "SELECT * FROM read_text('http://127.0.0.1:9/x')",
    "SELECT * FROM glob('/*')",
])
def test_local_files_and_plain_http_are_refused(con, sql):
    check_select(con, sql)  # a SELECT, so it passes the statement check...
    with pytest.raises(duckdb.PermissionException):
        con.sql(sql).fetchall()  # ...and DuckDB refuses the read


def test_s3_reaches_the_deployment_endpoint(con):
    # Allowed through to the (unreachable) endpoint the server configured.
    with pytest.raises(duckdb.IOException, match="127.0.0.1:9"):
        con.sql("SELECT count(*) FROM 's3://bucket/x.parquet'").fetchall()


@pytest.mark.parametrize("sql", ["SET memory_limit='100GB'", "RESET memory_limit",
                                 "SET enable_external_access=true"])
def test_settings_cannot_change(con, sql):
    with pytest.raises(duckdb.Error, match="locked"):
        con.sql(sql)


@pytest.mark.parametrize("sql", [
    "CREATE SECRET evil (TYPE s3, ENDPOINT 'evil.example.org')",
    "COPY (SELECT 1) TO 's3://bucket/out.parquet'",
    "ATTACH 's3://bucket/x.db'",
    "INSTALL spatial",
    "LOAD spatial",
    "PRAGMA version",
    "SET memory_limit='1GB'",
    "CREATE TABLE t AS SELECT 1",
    "SELECT 1; SELECT 2",
    "SELECT 1; CREATE SECRET evil (TYPE s3, ENDPOINT 'evil.example.org')",
])
def test_only_one_select_runs(con, sql):
    with pytest.raises(ValueError):
        check_select(con, sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "WITH a AS (SELECT 1 AS x) SELECT * FROM a",
    "FROM (SELECT 1 AS x)",
    "SELECT 'SET memory_limit' AS s",
    "DESCRIBE SELECT 1 AS x",
])
def test_selects_pass(con, sql):
    check_select(con, sql)


def test_secret_functions_are_refused(con):
    # duckdb_secrets() redacts the secret but shows the key ID.
    assert "AKIAEXAMPLEKEY" in str(con.sql("SELECT * FROM duckdb_secrets()").fetchall())
    for sql in ("SELECT * FROM duckdb_secrets()", "SELECT which_secret('s3://b/x', 's3')"):
        with pytest.raises(ValueError, match="deployment"):
            check_select(con, sql)


def test_redact():
    assert redact("key AKIAEXAMPLEKEY bad", ["AKIAEXAMPLEKEY", "", None, "ab"]) == "key [REDACTED] bad"


def test_local_table_dirs_are_admitted(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_TEMP_ROOT", str(tmp_path / "spill"))
    data = tmp_path / "data"
    data.mkdir()
    c = duckdb.connect()
    c.execute(f"COPY (SELECT 1 AS x) TO '{data}/t.parquet'")
    for stmt in setup_sql():
        c.execute(stmt)
    c.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{data}/t.parquet')")
    lock_down(c, [str(data)])
    assert c.sql("SELECT x FROM t").fetchall() == [(1,)]
    with pytest.raises(duckdb.PermissionException):
        c.sql("SELECT * FROM read_text('/etc/passwd')").fetchall()
