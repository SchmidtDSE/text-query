"""Prototype text-query MCP server: codebook questions over the rows a SQL query returns.

A stand-in for the design in ../README.md, built for a demo in the High Seas app. Nothing is
hard-wired to one corpus or one model:

  DECISION_URL, DECISION_TOKEN   any Jev-compatible endpoint, e.g. https://openrouter.ai/api/v1/systemone
  DECISION_MODEL                 optional model name, e.g. ~typesafe/jev-latest
  S3_ENDPOINT, S3_KEY, S3_SECRET optional; without keys DuckDB reads public buckets only
  MCP_AUTH_TOKEN                 optional bearer token for callers
  INSTRUCTIONS_FILE              optional markdown describing the corpora, sent to clients
  MAX_ROWS, WORKERS              rows per call and parallel requests

Two tools: `decide` runs typed questions (noul, choice, score) over the text column of a
query's rows; `read_rows` returns a few rows, with long text truncated, so an agent can look
at passages and check its SQL.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import anyio
import duckdb
import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

DECISION_URL = os.environ["DECISION_URL"].rstrip("/")
DECISION_TOKEN = os.environ.get("DECISION_TOKEN", "")
DECISION_MODEL = os.environ.get("DECISION_MODEL")
MAX_ROWS = int(os.environ.get("MAX_ROWS", "400"))
WORKERS = int(os.environ.get("WORKERS", "12"))
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")

INSTRUCTIONS = """Runs codebook questions over text with a decision model, and reads rows of text tables.
Write DuckDB SQL that returns one row per passage, then call `decide` with typed questions."""
if os.environ.get("INSTRUCTIONS_FILE"):
    INSTRUCTIONS = open(os.environ["INSTRUCTIONS_FILE"]).read()

mcp = FastMCP("text-query", instructions=INSTRUCTIONS, stateless_http=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

# Answers are cached by model, question and text, so repeated questions cost nothing and
# return the same answer.
log = logging.getLogger("text-query")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def logged(tool, sql, fn):
    """Run a tool body, logging its SQL and any error so failed calls can be diagnosed."""
    log.info("%s sql=%s", tool, " ".join(sql.split())[:1000])
    try:
        out = fn()
    except Exception as e:
        log.warning("%s failed: %s: %s", tool, type(e).__name__, e)
        raise
    if out.startswith('{"error"'):
        log.warning("%s refused: %s", tool, out[:300])
    return out


CACHE: OrderedDict = OrderedDict()
CACHE_SIZE = 200_000


def connect():
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # S3_ENDPOINT may carry a scheme: an in-cluster http:// service turns TLS off.
    endpoint = os.environ.get("S3_ENDPOINT", "s3-west.nrp-nautilus.io")
    use_ssl = not endpoint.startswith("http://")
    endpoint = endpoint.split("://")[-1].rstrip("/")
    if os.environ.get("S3_KEY"):
        con.execute("CREATE SECRET (TYPE s3, KEY_ID ?, SECRET ?, ENDPOINT ?, USE_SSL ?, URL_STYLE 'path')",
                    [os.environ["S3_KEY"], os.environ["S3_SECRET"], endpoint, use_ssl])
    else:
        con.execute("CREATE SECRET (TYPE s3, PROVIDER config, ENDPOINT ?, USE_SSL ?, URL_STYLE 'path')",
                    [endpoint, use_ssl])
    return con


def run_sql(sql, limit):
    con = connect()
    try:
        rel = con.sql(sql)
        cols = rel.columns
        rows = rel.limit(limit + 1).fetchall()
    finally:
        con.close()
    return cols, rows


def check_questions(questions):
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be an object of named questions")
    for name, q in questions.items():
        kind = q.get("type")
        if kind not in ("noul", "choice", "score"):
            raise ValueError(f"{name}: type must be noul, choice or score")
        if not q.get("instructions"):
            raise ValueError(f"{name}: instructions are required")
        if kind == "choice" and not isinstance(q.get("criteria"), dict):
            raise ValueError(f"{name}: a choice question needs criteria as an object of label: description")
        if kind == "score" and not isinstance(q.get("criteria"), list):
            raise ValueError(f"{name}: a score question needs criteria as a list of levels, lowest first")


def ask_one(state, questions):
    """All questions for one text, from the cache or the endpoint. Returns answers and cost."""
    key = hashlib.sha256(json.dumps([DECISION_MODEL, questions, state], sort_keys=True).encode()).hexdigest()
    if key in CACHE:
        CACHE.move_to_end(key)
        return CACHE[key], 0.0, True
    body = {"state": state, "questions": questions}
    if DECISION_MODEL:
        body["model"] = DECISION_MODEL
    for attempt in range(4):
        try:
            r = requests.post(DECISION_URL, json=body, timeout=120,
                              headers={"Authorization": f"Bearer {DECISION_TOKEN}"})
            r.raise_for_status()
            out = r.json()
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    CACHE[key] = out["answers"]
    if len(CACHE) > CACHE_SIZE:
        CACHE.popitem(last=False)
    return out["answers"], out.get("usage", {}).get("cost", 0) or 0, False


def flatten(answers, questions):
    row = {}
    for name, q in questions.items():
        a = answers[name]
        if q["type"] == "noul":
            row[name] = round(a["noul"], 3)
        elif q["type"] == "score":
            row[name] = round(a["score"], 2)
        else:
            row[name] = a["choice"]
            row[name + "_p"] = round(a["probabilities"][a["choice"]], 3)
    return row


def summarise(coded, questions):
    out = {}
    for name, q in questions.items():
        if q["type"] == "noul":
            vals = [r[name] for r in coded]
            out[name] = {"yes_at_0.5": sum(v > 0.5 for v in vals), "yes_at_0.7": sum(v > 0.7 for v in vals),
                         "mean_p": round(sum(vals) / len(vals), 3)}
        elif q["type"] == "score":
            vals = [r[name] for r in coded]
            out[name] = {"mean": round(sum(vals) / len(vals), 2)}
        else:
            counts = {}
            for r in coded:
                counts[r[name]] = counts.get(r[name], 0) + 1
            out[name] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return out


def _decide(sql, questions, text_column, context_column, return_rows, max_return, include_text):
    check_questions(questions)
    cols, rows = run_sql(sql, MAX_ROWS)
    if len(rows) > MAX_ROWS:
        return json.dumps({"error": f"the query returns more than {MAX_ROWS} rows; narrow it, "
                           "for example with a WHERE clause on folder, title or a keyword"})
    if text_column not in cols:
        return json.dumps({"error": f"no column {text_column!r}; columns are {cols}"})
    if not rows:
        return json.dumps({"n": 0, "summary": {}, "rows": []})
    recs = [dict(zip(cols, r)) for r in rows]
    states = [(f"{rec[context_column]}\n\n" if context_column else "") + str(rec[text_column]) for rec in recs]

    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(lambda s: ask_one(s, questions), states))
    cost = sum(c for _, c, _ in results)

    coded = []
    for rec, (answers, _, _) in zip(recs, results):
        out = {k: v for k, v in rec.items() if k != text_column}
        out = {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v)) for k, v in out.items()}
        if include_text:
            text = str(rec[text_column])
            out[text_column] = text if len(text) <= 600 else text[:600] + "…"
        out.update(flatten(answers, questions))
        coded.append(out)

    nouls = [n for n, q in questions.items() if q["type"] == "noul"]
    if nouls:
        coded.sort(key=lambda r: -max(r[n] for n in nouls))
    if return_rows == "positive":
        shown = [r for r in coded if nouls and max(r[n] for n in nouls) > 0.5]
    elif return_rows == "all":
        shown = coded
    else:
        shown = []
    return json.dumps({
        "n": len(coded), "model": DECISION_MODEL or DECISION_URL.split("//")[-1].split("/")[0],
        "cost_usd": round(cost, 4), "cached": sum(c for _, _, c in results), "seconds": round(time.time() - t0, 1),
        "summary": summarise(coded, questions),
        "rows_returned": min(len(shown), max_return), "rows_matching": len(shown),
        "rows": shown[:max_return],
    }, default=str)


@mcp.tool()
async def decide(sql: str, questions: dict, text_column: str = "text", context_column: str | None = None,
                 return_rows: str = "positive", max_return: int = 40, include_text: bool = False) -> str:
    """Answer typed questions about each row's text with a decision model.

    sql: DuckDB SQL returning one row per text, with at most MAX_ROWS rows (default 400). Keep
        id columns such as passage_id, title and page so answers can be cited.
    questions: named questions in the Jev format, for example
        {"vents": {"type": "noul", "instructions": "Does this paragraph describe hydrothermal vent fauna?"},
         "depth": {"type": "choice", "instructions": "Which depth zone?",
                   "criteria": {"shallow": "above 200 m", "deep": "below 200 m", "none": "not stated"}},
         "strength": {"type": "score", "instructions": "How strong is the evidence?",
                      "criteria": ["anecdote", "single survey", "several surveys", "time series"]}}
        noul returns P(yes); choice returns a label and its probability; score returns the
        expected level, counting from 0. Phrase questions about one paragraph.
    text_column: the column the questions are asked about.
    context_column: optional column prefixed to each text, such as a paper title, which helps
        the model place a paragraph.
    return_rows: "positive" (rows where any yes/no question is above 0.5), "all" or "none".
        Rows are sorted by their highest yes/no probability. The summary always covers all rows.
    max_return: the most rows to return.
    include_text: return each row's text, truncated to 600 characters, to quote evidence.
    """
    return await anyio.to_thread.run_sync(lambda: logged("decide", sql, lambda:
        _decide(sql, questions, text_column, context_column, return_rows, max_return, include_text)))


@mcp.tool()
async def read_rows(sql: str, max_rows: int = 20) -> str:
    """Run DuckDB SQL and return up to max_rows rows (at most 50), with strings over 500
    characters truncated. Use it to inspect tables, count passages or read evidence."""
    def run():
        cols, rows = run_sql(sql, min(max_rows, 50))
        out = [{c: (v[:500] + "…" if isinstance(v, str) and len(v) > 500 else v) for c, v in zip(cols, r)}
               for r in rows[:min(max_rows, 50)]]
        return json.dumps({"columns": cols, "rows": out, "truncated": len(rows) > min(max_rows, 50)}, default=str)
    return await anyio.to_thread.run_sync(lambda: logged("read_rows", sql, run))


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return JSONResponse({"status": "ok"})
        if AUTH_TOKEN:
            got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(got, AUTH_TOKEN):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuth)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
