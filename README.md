# text-query

Runs codebooks and structured extraction over any text column that a SQL query returns. It is
to text what [`geo-query`](https://github.com/SchmidtDSE/geo-query) is to tables: generic, open by default, and usable by agents in any app.
A codebook is a set of typed questions (yes/no, choice from labels, or a level on an ordered
scale), and the answers are probabilities that become columns of a new dataset.

**Status:** prototype. `prototype/` is an MCP server with `decide` and `read_rows` that the
private High Seas app runs today; see its README. A pilot version of the batch coder
is in [`text-workflows/pilot`](https://github.com/SchmidtDSE/text-workflows/tree/main/pilot).
The design is in
[`text-workflows/PLAN.md`](https://github.com/SchmidtDSE/text-workflows/blob/main/PLAN.md).

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
  (`https://openrouter.ai/api/v1/systemone`), Kev and Laya are all of this kind. Jev is the
  default; Kev is a free option where speed does not matter (see the pilot results in
  `PLAN.md`).
- **Extraction model:** any OpenAI-compatible chat endpoint with structured output.
- **Embedding model, if used:** any OpenAI-compatible embeddings endpoint.

Coded outputs record the endpoint, model, question text and hash, and run date in their
parquet metadata. Answers are cached by model, question and text, so repeated questions cost
nothing and return the same answer.

## Authentication

Open is the default; private is configuration, as in geo-query. Credentials belong to the
deployment and never pass through a tool call, because the model writes every tool argument
and would carry a key into the conversation it sends to its language model.

| Concern | Open default | Private option |
|---|---|---|
| Who can call the server | anyone | named bearer tokens, one per calling app (`MCP_AUTH_TOKENS_DIR`) |
| Which buckets it reads | public buckets | the deployment's own read-only keys (`S3_KEY`, `S3_SECRET`) |
| Which models it calls, and whose credits | the deployment's endpoint and token (`DECISION_URL`, `DECISION_TOKEN`) | the same; there is no spending cap yet, only `MAX_ROWS` per call |
| Which tables it sees | whatever the SQL names | named views over the deployment's files (`TABLES`) |

## Hardening

The server runs SQL that a language model writes, so it is locked down as geo-query has been
since the NRP incident of 2026-11-06, when a public query tool was used to read a pod's
Kubernetes service-account token. The server enforces these itself
([`prototype/README.md`](prototype/README.md#hardening) has the details):

- Each call gets a fresh DuckDB connection that reads only `s3://` and `https://` and its own
  spill directory, with extension loading and every setting locked.
- Only a single SELECT statement runs. DuckDB's lock does not stop `CREATE SECRET` or
  `COPY ... TO`, so this check closes them.
- `duckdb_secrets()` is refused, and keys and tokens are removed from results and errors.
- Memory and spill are capped at about 80% and 50% of the pod's limits.
- Every path but `/health` needs a named token; the caller's name, never the token, is logged.

## Deployment

A deployment adds the rest. The High Seas app's manifests are the working example:
[`k8s/text-query-deployment.yaml`](https://github.com/boettiger-lab/bosl-high-seas/blob/main/k8s/text-query-deployment.yaml)
and [`k8s/text-query.yaml`](https://github.com/boettiger-lab/bosl-high-seas/blob/main/k8s/text-query.yaml)
in boettiger-lab/bosl-high-seas.

- **The image, pinned by digest.** CI builds `ghcr.io/schmidtdse/text-query:<sha>` on every push
  to `main`. Nothing is cloned or installed when the pod starts.
- **An unprivileged pod.** `runAsNonRoot` with uid and gid 10001, `readOnlyRootFilesystem`, all
  capabilities dropped, the `RuntimeDefault` seccomp profile, and
  `automountServiceAccountToken: false`. Mount an `emptyDir` at `/tmp` for DuckDB's spill
  files.
- **Pod limits passed to the server.** Set `POD_MEMORY_LIMIT` and `POD_EPHEMERAL_LIMIT` from
  `limits.memory` and `limits.ephemeral-storage` with the Downward API, so DuckDB spills
  within them.
- **Tokens from a Secret.** Mount a Secret at `MCP_AUTH_TOKENS_DIR` with one key per calling
  app, named for it, mode `0440`. Each app's nginx adds its token server-side, so browsers
  never hold it. The server reads tokens at start-up, so adding or revoking one needs a
  restart.
- **No Ingress.** Apps reach the server through their own nginx at
  `http://<service>.<namespace>.svc.cluster.local:8000/mcp`, behind the app's sign-in.
- **A NetworkPolicy in both directions.** Ingress only from the apps' pods on port 8000.
  Egress only to cluster DNS and to https on public addresses, which covers the object store
  and the decision model, so SQL cannot reach services inside the cluster.
- **Object stores through their public https endpoints.** NRP drops pod traffic between
  namespaces, and the egress policy blocks in-cluster addresses anyway. Use
  `S3_ENDPOINT=https://rustfs.nrp-nautilus.io` for rustfs, never its internal Service.
- **Small requests.** At most 1 CPU and 2 GiB requested, which keeps an idle server out of
  NRP's utilization checks; limits may be higher.

## Licence

BSD-3-Clause. See [DISCLOSURE.md](DISCLOSURE.md) for the use of software tools.
