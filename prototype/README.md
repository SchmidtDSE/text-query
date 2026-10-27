# Prototype server

A single-file MCP server with two tools, built to demo codebook questions over a literature
corpus in the High Seas app. It stands in for the design in the top-level README until the
library and batch front end exist.

| Tool | Purpose |
|---|---|
| `decide(sql, questions, ...)` | Asks typed questions (noul, choice, score) about the text column of up to `MAX_ROWS` rows that a DuckDB query returns, and returns a summary plus the rows with the highest probabilities |
| `read_rows(sql, max_rows)` | Returns up to 50 rows with long strings truncated |

The decision model, S3 credentials, caller token and corpus description are all configuration;
see the docstring of `server.py`. Answers are cached in memory by model, question and text.

```bash
pip install -r requirements.txt
DECISION_URL=https://openrouter.ai/api/v1/systemone DECISION_TOKEN=$JEV_KEY \
DECISION_MODEL='~typesafe/jev-latest' python server.py   # serves /mcp on port 8000
```

The High Seas deployment is `k8s/text-query.yaml` in
[`bosl-high-seas`](https://github.com/boettiger-lab/bosl-high-seas).
