# text-query

Runs codebooks and structured extraction over any text column that a SQL query returns. It is
to text what [`mcp-data-server`](https://github.com/boettiger-lab/mcp-data-server) (to be
renamed `geo-query`) is to tables: generic, open by default, and usable by agents in any app.
A codebook is a set of typed questions (yes/no, choice from labels, or a level on an ordered
scale), and the answers are probabilities that become columns of a new dataset.

**Status:** design. A pilot version of the batch coder is in
[`corpus-workflows/pilot`](https://github.com/SchmidtDSE/corpus-workflows/tree/main/pilot). The
design is in
[`corpus-workflows/PLAN.md`](https://github.com/SchmidtDSE/corpus-workflows/blob/main/PLAN.md).

## Front ends

One core library has two front ends.

- **Command line and cluster jobs, for batch.** Coding a whole corpus is a long, resumable job
  that writes a coded parquet to S3, where SQL tools can join it like any other dataset.
- **MCP server, for interactive use.** An agent asks a new question of a few hundred passages
  during a conversation and reads the answers in seconds.

The library handles the SQL read, batching, retries, the answer cache, provenance and quote
checking, and can be called directly from notebooks.

## Tools

| Tool | Purpose |
|---|---|
| `decide(sql, id_column, text_column, questions)` | Runs a codebook over the rows a query returns, up to an interactive limit, and returns ids with probabilities |
| `submit_decide(...)`, `job_status(job_id)` | The same for a whole corpus, writing results to an S3 parquet path |
| `extract(sql, id_column, text_column, schema)` | Structured extraction with a JSON schema, on rows a codebook has already flagged |
| `check_quotes(...)` | Confirms that each extracted quote appears in its source text |

## Pluggable models

No model is hard-wired. Each is an endpoint, a model name and a token, set by the deployment or
supplied per request.

- **Decision model:** any service that accepts the Jev request body (`state` plus named
  `noul`, `choice` and `score` questions) and returns probabilities. Jev on OpenRouter
  (`https://openrouter.ai/api/v1/systemone`) and Laya are both of this kind. In the pilot Jev
  was much more accurate; Laya is free and useful as a comparison.
- **Extraction model:** any OpenAI-compatible chat endpoint with structured output.
- **Embedding model, if used:** any OpenAI-compatible embeddings endpoint.

Coded outputs record the endpoint, model, question text and hash, and run date in their
parquet metadata. Answers are cached by model, question and text, so repeated questions cost
nothing and return the same answer.

## Authentication

Open is the default; private is configuration, as in `mcp-data-server`.

| Concern | Open default | Private option |
|---|---|---|
| Who can call the server | anyone | `MCP_AUTH_TOKEN` bearer token |
| Which buckets it reads | public buckets | `s3_key`, `s3_secret`, `s3_endpoint` and `s3_scope` per request, held for that request only |
| Which models it calls, and whose credits | deployment defaults with a spending cap | the client's own endpoint and token per request |
| Which catalog it sees | the public catalog | a private catalog by deployment (`STAC_CATALOG_TOKEN`) |

## Licence

BSD-3-Clause. See [DISCLOSURE.md](DISCLOSURE.md) for the use of software tools.
