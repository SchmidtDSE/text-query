# Prototype server

An MCP server with two tools, built to demo codebook questions over a literature
corpus in the High Seas app. It stands in for the design in the top-level README until the
library and batch front end exist.

| Tool | Purpose |
|---|---|
| `decide(sql, questions, ...)` | Asks typed questions (noul, choice, score) about the text column of up to `MAX_ROWS` rows that a DuckDB query returns, and returns a summary plus the rows with the highest probabilities |
| `read_rows(sql, max_rows)` | Returns up to 50 rows with long strings truncated |

The decision model, S3 credentials, caller tokens and corpus description are all configuration;
see the docstring of `server.py`. Answers are cached in memory by model, question and text.

## Hardening

The server runs SQL that a language model writes, so it follows geo-query's lockdown after the
NRP incident of 2026-11-06, plus one rule geo-query lacks.

- **Locked DuckDB.** Each call gets a fresh connection that reads only `s3://` and `https://`
  plus its own spill directory, with extension autoload off and the configuration locked, so
  SQL cannot read local files such as a service-account token, or change any setting
  (`dbconfig.py`).
- **One SELECT statement.** The lock does not cover secrets, so caller SQL could otherwise
  `CREATE SECRET` to send `s3://` paths to another host, or `COPY ... TO 's3://...'` with the
  deployment's keys. Only a single SELECT (including `WITH`, `DESCRIBE`, `SUMMARIZE` and
  FROM-first forms) runs.
- **No credentials in output.** `duckdb_secrets()` and `which_secret()` are refused, since the
  first shows the key ID, and the S3 key and secret and the decision token are redacted from
  results and errors.
- **Bounded memory and spill.** `POD_MEMORY_LIMIT` and `POD_EPHEMERAL_LIMIT` (Downward API) cap
  DuckDB at about 80% of the pod's memory and each connection's spill at half its ephemeral
  storage, so a large query fails alone instead of taking down the pod.
- **Named caller tokens.** `MCP_AUTH_TOKENS_DIR` holds one file per caller, named for it (a
  mounted Secret). The matched name, never the token, goes on each log line, and one caller
  can be revoked alone. A set but empty directory stops the server from starting.
  `MCP_AUTH_TOKEN` still works as a token named `default`.
- **Unprivileged image.** The `Dockerfile` bakes in the code and the httpfs extension and runs
  as uid 10001, so the pod can run non-root, with no capabilities and a read-only root
  filesystem. CI builds `ghcr.io/schmidtdse/text-query` on every push to `main`.

The deployment adds the rest: no service-account token, no Ingress, and a NetworkPolicy that
admits only the app's pods and lets the server reach only DNS and public https. The
top-level README's Deployment section lists what a deployment needs.

```bash
pip install -r requirements.txt pytest httpx && python -m pytest tests
```

```bash
pip install -r requirements.txt
DECISION_URL=https://openrouter.ai/api/v1/systemone DECISION_TOKEN=$JEV_KEY \
DECISION_MODEL='~typesafe/jev-latest' python server.py   # serves /mcp on port 8000
```

The High Seas deployment is `k8s/text-query.yaml` and `k8s/text-query-deployment.yaml` in
[`bosl-high-seas`](https://github.com/boettiger-lab/bosl-high-seas).
