"""DuckDB connection hardening, adapted from geo-query's dbconfig.py.

Every connection runs caller-supplied SQL, so each one is set up the way
geo-query sets up its own after the NRP incident of 2026-11-06, when someone
read a pod's Kubernetes service-account token through a public query tool:

- Memory is bounded to the pod (POD_MEMORY_LIMIT, ~80%), so a large query
  spills instead of OOM-killing the replica (geo-query#270).
- Each connection spills to a private directory, capped at half the pod's
  ephemeral-storage limit (POD_EPHEMERAL_LIMIT), so concurrent spills do not
  collide and a runaway spill fails its query instead of evicting the pod
  (geo-query#423).
- Reads are limited to s3:// and https:// plus the spill directory, extension
  autoload and community extensions are off, and the configuration is locked
  (geo-query#453, #460).

text-query adds one rule geo-query's server lacks: caller SQL must be a single
SELECT statement. The configuration lock does not cover secrets, so without
this check caller SQL could CREATE SECRET to send s3:// paths to another host,
or COPY ... TO 's3://...' with the deployment's own keys. DuckDB serializes only
SELECT statements with json_serialize_sql(), which gives the check (as
geo-query-wasm does).

Env is read at call time, so tests can monkeypatch.
"""
import json
import os
import re
import sys
import uuid

_MEMORY_LIMIT_FRACTION = 0.8
_TEMP_DIRECTORY_FRACTION = 0.5

_BYTE_UNITS = {
    "": 1, "b": 1,
    "k": 10**3, "kb": 10**3, "ki": 2**10, "kib": 2**10,
    "m": 10**6, "mb": 10**6, "mi": 2**20, "mib": 2**20,
    "g": 10**9, "gb": 10**9, "gi": 2**30, "gib": 2**30,
    "t": 10**12, "tb": 10**12, "ti": 2**40, "tib": 2**40,
}

# Remote prefixes caller SQL may read. Plain http:// is absent: it would reach
# unencrypted in-cluster services. An s3:// path still goes to an http://
# endpoint when the deployment's own secret names one (S3_ENDPOINT); that
# endpoint is fixed by the server, not the caller.
REMOTE_PREFIXES = ("s3://", "https://")

SECRET_FUNCTIONS = re.compile(r"\b(duckdb_secrets|which_secret)\b", re.IGNORECASE)

LOCKED_HINT = (
    "This server configures every connection itself (settings, extensions, S3 "
    "secrets). Send one SELECT statement, without SET, RESET, PRAGMA, INSTALL, "
    "CREATE, COPY or ATTACH."
)


def sql_quote(value: str) -> str:
    """Escape a value for a single-quoted SQL string literal."""
    return (value or "").replace("'", "''")


def _parse_bytes(text: str) -> int:
    """A plain byte count (the Downward API's form) or a k8s/SI quantity."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", text or "")
    if not m:
        raise ValueError(f"unparseable byte quantity: {text!r}")
    num, unit = m.group(1), m.group(2).lower()
    if unit not in _BYTE_UNITS:
        raise ValueError(f"unknown byte unit {unit!r} in {text!r}")
    return int(float(num) * _BYTE_UNITS[unit])


def _fraction_of_pod_limit(var: str, fraction: float) -> str | None:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    try:
        total = _parse_bytes(raw)
    except ValueError as e:
        print(f"⚠️ {var} ignored ({e})", file=sys.stderr)
        return None
    mib = int(total * fraction) // (1024 * 1024)
    return f"{mib}MiB" if mib > 0 else None


def duckdb_memory_limit() -> str | None:
    """DUCKDB_MEMORY_LIMIT verbatim, else ~80% of POD_MEMORY_LIMIT, else None."""
    explicit = os.environ.get("DUCKDB_MEMORY_LIMIT", "").strip()
    return explicit or _fraction_of_pod_limit("POD_MEMORY_LIMIT", _MEMORY_LIMIT_FRACTION)


def duckdb_max_temp_directory_size() -> str | None:
    """DUCKDB_MAX_TEMP_DIRECTORY_SIZE verbatim, else half of POD_EPHEMERAL_LIMIT."""
    explicit = os.environ.get("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "").strip()
    return explicit or _fraction_of_pod_limit("POD_EPHEMERAL_LIMIT", _TEMP_DIRECTORY_FRACTION)


def setup_sql() -> list[str]:
    """SET statements for a fresh connection, before any secret or view."""
    stmts = ["SET allow_persistent_secrets=false"]
    mem = duckdb_memory_limit()
    if mem:
        stmts.append(f"SET memory_limit='{sql_quote(mem)}'")
    root = os.environ.get("DUCKDB_TEMP_ROOT", "").strip() or "/tmp"
    stmts.append(f"SET temp_directory='{sql_quote(os.path.join(root, f'duckdb-{uuid.uuid4().hex}'))}'")
    cap = duckdb_max_temp_directory_size()
    if cap:
        stmts.append(f"SET max_temp_directory_size='{sql_quote(cap)}'")
    return stmts


def lock_down(conn, extra_dirs=()) -> None:
    """Limit `conn` to remote storage, its spill directory and `extra_dirs` (local
    TABLES files in development), then freeze its configuration. Call last, after
    every SET, LOAD, secret and view."""
    temp_dir = conn.sql("SELECT current_setting('temp_directory')").fetchone()[0]
    allowed = list(REMOTE_PREFIXES)
    for d in (temp_dir, *extra_dirs):
        if d and "://" not in d:
            allowed.append(d.rstrip("/") + "/")
    conn.sql("SET allowed_directories=[" + ", ".join(f"'{sql_quote(a)}'" for a in allowed) + "]")
    conn.sql("SET autoinstall_known_extensions=false")
    conn.sql("SET autoload_known_extensions=false")
    conn.sql("SET allow_community_extensions=false")
    conn.sql("SET enable_external_access=false")
    conn.sql("SET lock_configuration=true")


def check_select(conn, sql: str) -> None:
    """Raise ValueError unless `sql` is one SELECT statement (DESCRIBE,
    SUMMARIZE, WITH and FROM-first forms count) that does not read secrets."""
    if SECRET_FUNCTIONS.search(sql):
        raise ValueError("duckdb_secrets() and which_secret() are not available here: "
                         "the S3 secrets belong to the deployment.")
    parsed = json.loads(conn.execute("SELECT json_serialize_sql(?::VARCHAR)", [sql]).fetchone()[0])
    if parsed.get("error"):
        if parsed.get("error_type") == "not implemented":
            raise ValueError(f"only SELECT queries run here. {LOCKED_HINT}")
        raise ValueError(parsed.get("error_message", "the SQL could not be parsed"))
    if len(parsed.get("statements", [])) != 1:
        raise ValueError("send one SELECT statement at a time")


def redact(text: str, values) -> str:
    """Replace each credential value in text with [REDACTED]."""
    for v in values:
        if v and len(v) >= 4:
            text = text.replace(v, "[REDACTED]")
    return text
