# ResumeAI — AI-Powered Resume Screener

Two things, sharing one evidence-based scoring engine:

1. **Check** — paste a job description, get a scored breakdown with a quote from your
   CV behind every judgement.
2. **Discover** — upload only your CV and get back the best matches from thousands of
   live job postings, ranked and scored.

Powered by **Gemini** (or Claude — one config line switches providers), with structured
logs shipped to an ELK stack. Runs locally with no Docker required; Kubernetes manifests
are included for when you deploy it.

The distinguishing feature is not the model — it is the **evaluation harness**. The
project keeps a keyword-matching baseline alongside the AI engine and measures both
against human-labelled data, so "the AI is better" is a measured number in this
repository rather than a claim in a README.

---

## Contents

- [What it does](#what-it-does)
- [Job discovery](#job-discovery)
- [Does the AI actually help?](#does-the-ai-actually-help)
- [Architecture](#architecture)
- [Quick start (no Docker)](#quick-start-no-docker)
- [Running without Docker](#running-without-docker)
- [Running the full stack with Docker](#running-the-full-stack-with-docker)
- [Deploying to Kubernetes](#deploying-to-kubernetes)
- [Observability](#observability)
- [Configuration](#configuration)
- [Testing](#testing)
- [Cost](#cost)
- [Design decisions](#design-decisions)
- [Limitations](#limitations)

---

## What it does

Upload a resume (PDF, DOCX, TXT), paste a job description, and get back:

| Output | Detail |
|---|---|
| **Overall score and verdict** | 0–100 plus one of four bands, from `strong_match` to `weak_match` |
| **Four dimension scores** | Skills, experience, education, domain relevance — each with written reasoning |
| **Per-requirement evidence** | For every skill the JD asks for: present or not, a quote from the resume, and whether it was merely *mentioned*, actually *applied*, or is genuine *expertise* |
| **Transferable skills** | Adjacent capability the resume has that the JD did not name |
| **Red flags** | Timeline gaps, contradictions, keyword stuffing — often empty by design |
| **Suggestions** | Specific, actionable, addressed to the candidate |

The `strength` field is what separates this from term counting. A resume listing
"Kubernetes" in a skills blob and a resume describing a cluster migration both contain
the word; only one demonstrates the skill.

---

## Does the AI actually help?

`eval/` holds 14 human-labelled resume/JD pairs across three roles, each with a score
and an advance/reject decision. Four are **adversarial by construction**:

- **Keyword stuffers** (`b5`, `d4`) — list every requirement verbatim, have done none of it.
- **Synonym-strong candidates** (`b6`, `f4`) — genuinely excellent, but write "k8s" instead
  of "Kubernetes" and describe capability through the work rather than a technology list.

Those four are exactly where term overlap breaks. Measured results — reproduce with
`python -m eval.evaluate --engine both`:

```
METRIC                          BASELINE          GEMINI
==============================================================================
pairwise accuracy                  0.667             1.0     0.5 = chance
spearman rho                       0.343             1.0     rank correlation vs. human
decision accuracy                  0.545             1.0     advance/reject agreement
mean abs. error                     21.9             9.0     points, lower is better
median latency (ms)                   12           13293
```

*(Gemini column: `gemini-3.5-flash`, 14/14 pairs, run 2026-08-30.)*

The adversarial cases are where the gap opens:

| Case | Human | Baseline | Gemini |
|---|---|---|---|
| `b5` keyword stuffer, no real experience | 28 | **58.7** ❌ | **10.0** ✅ |
| `b6` excellent, writes "k8s" not "Kubernetes" | 86 | **34.8** ❌ | **96.0** ✅ |
| `d4` support technician claiming the whole DevOps stack | 32 | **48.2** ❌ | **15.0** ✅ |
| `f4` strong React work described through products built | 78 | **29.8** ❌ | **92.0** ✅ |

The baseline ranks the stuffer `b5` **above** the excellent `b6` — 58.7 to 34.8 — and
its advance/reject accuracy of 0.545 is barely distinguishable from a coin flip. Gemini
orders all 27 comparable pairs correctly and flags the stuffing explicitly in
`red_flags`.

### Read those 1.0s with suspicion

Perfect scores mean the **test set is saturated, not that the model is perfect**. 14
pairs, labelled by one person, with adversarial cases built around a failure mode I
already knew about, is a demonstration — not a benchmark. A harder set would separate
these engines further.

One real weakness the numbers do show: an **MAE of 9.0 with perfect ranking** means
Gemini is systematically optimistic on strong candidates (98 vs. 88, 96 vs. 86, 94 vs.
85). Ordering is right, calibration runs hot. If you use absolute score thresholds
rather than ranking, calibrate them against your own labels first.

---

## Job discovery

Upload a CV, get the roles it matches best from a live corpus of real postings.

### Where the jobs come from

Ingested from public, keyless company boards — **no scraping, no ToS violations**:

| Source | Postings fetched | Notes |
|---|---|---|
| Greenhouse | 3,788 | ~20 company boards, full JD text |
| Lever | 89 | Same model, different companies |
| Remotive | 19 | Remote tech roles |

Edit [`companies.json`](resumescreener/jobs/companies.json) to change coverage. A dead
board is logged and skipped, never fatal.

```bash
python -m resumescreener.jobs.ingest          # fetch + embed
python -m resumescreener.jobs.ingest --stats  # what is indexed
```

### The funnel

Scoring 30 jobs with an LLM would take seven minutes and exhaust a free-tier day on a
single upload. So the expensive engine is spent only where the cheap stages already
agree:

| Stage | Method | Narrows | Cost |
|---|---|---|---|
| 1. Retrieve | SQLite query + filters | thousands → ~2,000 | free |
| 2. Rank | Hybrid: embeddings + skill overlap | ~2,000 → 30 | 1 API call |
| 3. Judge | The same LLM screener as `/analyze` | 30 → top 10 | ~10 calls, concurrent |
| 4. Tail | The baseline keyword engine | the rest | free |

Every job embedding is **cached in SQLite forever**, so a posting is embedded once, not
once per user who searches. That is what makes the cost amortize to nothing.

Results are labelled by how they were scored — `Scored in detail` versus
`Keyword ranked` — because a reasoned judgement and a keyword tally should never look
identical to someone deciding where to apply.

### Retrieval evaluation

```bash
python -m eval.evaluate_retrieval --distractors 300
```

Measures whether the ranker surfaces the right job, using **hard negatives**: the real
postings most semantically similar to each target, not an arbitrary sample. Reports
recall@k, MRR, and a forced-choice accuracy (rank the three target JDs against each
other only — chance is 0.333).

**Current result: hybrid and TF-IDF tie at 1.0 on every metric.** That is a statement
about the benchmark, not a victory. Distinguishing backend from frontend from devops is
coarse enough that term overlap already solves it. The embedding ranker would need finer
distinctions — backend-payments versus backend-infrastructure — to show its value, and
building that test set is the honest next step. Until then the hybrid ranker is not
demonstrated to beat free TF-IDF, and the README should not pretend otherwise.

---

## Architecture

```
                       ┌──────────────────────────────┐
  browser ──upload──▶  │  Flask (gunicorn, gthread)   │
                       │  routes.py → screener.py     │
                       └───────────┬──────────────────┘
                                   │  LLM_PROVIDER
                    ┌──────────────┴───────────────┐
                    ▼                              ▼
        ┌───────────────────────┐        ┌────────────────────┐
        │  gemini.py │ llm.py   │  fail  │  baseline.py       │
        │  shared rubric from   │───────▶│  TF-IDF + skill    │
        │  prompts.py           │  over  │  dictionary        │
        └───────────────────────┘        └────────────────────┘
                    │                              │
                    └──────────────┬───────────────┘
                                   ▼
                        ScreeningResponse (Pydantic)
                                   │
                       stdout / file, one JSON object per line
                                   ▼
              Filebeat ─▶ Logstash ─▶ Elasticsearch ─▶ Kibana
```

| Module | Responsibility |
|---|---|
| [`app.py`](resumescreener/app.py) | Application factory, error handlers, request correlation ids |
| [`routes.py`](resumescreener/routes.py) | `/analyze`, `/healthz`, `/readyz` |
| [`screener.py`](resumescreener/screener.py) | Provider routing and graceful degradation |
| [`gemini.py`](resumescreener/gemini.py) | Gemini engine: structured outputs, backoff on 429/503 |
| [`llm.py`](resumescreener/llm.py) | Claude engine: structured outputs, prompt caching |
| [`prompts.py`](resumescreener/prompts.py) | The scoring rubric — **shared by both engines** |
| [`baseline.py`](resumescreener/baseline.py) | Deterministic fallback and evaluation control arm |
| [`schemas.py`](resumescreener/schemas.py) | Pydantic models — also the JSON Schema sent to the model |
| [`parsing.py`](resumescreener/parsing.py) | PDF/DOCX/TXT extraction with user-safe errors |
| [`logging_config.py`](resumescreener/logging_config.py) | ECS-shaped JSON logging |

Both engines send the **identical rubric** from `prompts.py` and return the **identical
schema**. That is what makes the eval comparison measure models rather than prompts.

---

## Quick start (no Docker)

```powershell
# Windows PowerShell
.\scripts\run-local.ps1 -Setup     # creates .venv, installs dependencies
.\scripts\run-local.ps1            # http://localhost:5000
```

```bash
# macOS / Linux
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env               # add your GEMINI_API_KEY
python wsgi.py                     # http://localhost:5000
```

Get a free Gemini key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
and put it in `.env` as `GEMINI_API_KEY`. Without a key the app still runs — it serves
baseline keyword scores and marks every response `degraded: true`, and the UI says so
rather than passing keyword overlap off as an AI judgement.

---

## Running without Docker

**Docker is not required for anything except the local ELK stack.** The application,
the tests, and the evaluation all run natively:

| Task | Command |
|---|---|
| Run the app | `.\scripts\run-local.ps1` or `python wsgi.py` |
| Run the tests | `.\scripts\run-local.ps1 -Test` or `pytest` |
| Run the evaluation | `.\scripts\run-local.ps1 -Eval` |
| Production-style server | `gunicorn wsgi:app --bind 0.0.0.0:8000 --threads 8` (Linux/macOS; on Windows use `waitress-serve --port=8000 wsgi:app`) |

### Observability without Docker

Elasticsearch, Logstash, and Kibana genuinely need containers or a hosted cluster — you
cannot run them from a Python venv. Three options, in order of effort:

1. **Log to a file and read it.** Set `LOG_FILE=logs/app.jsonl` in `.env`. Every event
   is written as one JSON object per line — the exact format Filebeat expects. Inspect
   it directly:

   ```powershell
   Get-Content logs\app.jsonl -Wait | ConvertFrom-Json | Format-Table log.level, message, engine, overall_score
   ```

2. **Elastic Cloud free trial.** Point Logstash (or Filebeat directly) at a hosted
   cluster and you get the real Kibana dashboards with no local containers. The
   pipeline config in `deploy/elk/` works unchanged apart from the `hosts` line.

3. **Install Elasticsearch and Kibana natively.** They ship as standalone archives and
   run on the JVM without Docker. Heavier, but fully local.

The Docker and Kubernetes files are kept and current — the stack is ready the moment
Docker is available on your machine.

---

## Running the full stack with Docker

*Requires Docker; see above if you cannot install it.*

```bash
export GEMINI_API_KEY=...
docker compose up -d --build

# app     http://localhost:8000
# kibana  http://localhost:5601
```

In Kibana, create data views for `screenings-*` and `app-logs-*`. Every screening lands
in `screenings-*` with `overall_score`, `verdict`, `score_bucket`, `engine`, `degraded`,
and `event.duration_ms` already parsed into fields — no grok, because the app emits JSON
and Filebeat decodes it at the edge.

---

## Deploying to Kubernetes

```bash
kubectl apply -f deploy/k8s/
```

See [`deploy/k8s/README.md`](deploy/k8s/README.md) for secret setup and the reasoning
behind probes and resource limits. The short version: liveness never touches the LLM
API, `/readyz` reports degradation without failing, and there is no CPU limit because
requests are blocked on network I/O rather than compute.

---

## Observability

Every log line is one JSON object shaped for the Elastic Common Schema:

```json
{
  "@timestamp": "2026-08-30T09:21:30.310Z",
  "log.level": "INFO",
  "message": "screening completed",
  "service.name": "resume-screener",
  "trace.id": "a3f19c02b7d4e881",
  "engine": "gemini",
  "degraded": false,
  "overall_score": 15.0,
  "verdict": "weak_match",
  "event.duration_ms": 9153
}
```

`trace.id` ties every line for one request together and is returned to the client in the
`X-Request-ID` header, so a user-reported problem can be found in Kibana directly.

Logstash routes screening events to `screenings-*` and everything else to `app-logs-*`,
and tags `degraded` and `alertable` for alerting. Dashboard panels worth building:

| Panel | Query |
|---|---|
| Degradation rate | `tags: degraded` over total — the LLM-outage alert |
| Score distribution | terms on `score_bucket` |
| Screening latency | percentiles of `event.duration_ms` where `tags: screening` |
| Error rate | `tags: alertable` |

---

## Configuration

Environment-driven, loaded from `.env` when present — see [`.env.example`](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `claude`; an unknown value fails loudly |
| `GEMINI_API_KEY` | — | Absent means degraded (baseline) mode |
| `GEMINI_MODEL` | `gemini-3.5-flash` | `gemini-2.5-flash`, `gemini-3.6-flash`, `gemini-3.7-flash` also work on the free tier; pro models are usually quota-blocked without billing |
| `GEMINI_TEMPERATURE` | `0.2` | Low, so the rubric drives the score rather than sampling variety |
| `ANTHROPIC_API_KEY` | — | Only when `LLM_PROVIDER=claude` |
| `FALLBACK_TO_BASELINE` | `true` | `false` makes an LLM outage a 503 instead |
| `LOG_FILE` | — | Also write JSON logs to a rotating file |
| `JSON_LOGS` | `true` | `false` for readable local development logs |

---

## Testing

```bash
pytest                      # 73 tests
pytest --cov=resumescreener # ~89% coverage
ruff check .
```

The suite stubs both provider SDKs, so it needs no API key and costs nothing — a test
that reaches the network is a bug. Coverage includes the degradation path, Gemini's
retry/backoff on 429 and 503, the prompt-cache ordering in the Claude engine,
error-message leakage, and a contract test that fails if the frontend drifts from the
API shape.

---

## Cost

**Gemini (default).** Roughly 850 input and 900 output tokens per screening. Free within
quota; median latency measured over the eval set is **13.3 seconds**.

> **Free-tier quota is small and per-model.** `gemini-3.5-flash` allowed **20 requests per
> day** on the key used here — one full `--engine both` eval run (14 screenings) plus a few
> manual tests exhausts it, after which every call returns `429 RESOURCE_EXHAUSTED` with
> `retryDelay: 0s` (retrying does not help; the window is daily). Quotas count **per
> model**, so switching `GEMINI_MODEL` to `gemini-2.5-flash` or `gemini-3.6-flash` gives a
> fresh budget. This is exactly why `gemini.py` retries 429/503 with backoff and why the
> baseline fallback exists — when the quota runs out the app keeps serving, flagged
> `degraded: true`.

**Claude.** About 2,000 input and 800 output tokens, roughly **$0.03 per resume** at
Opus 5 rates. The Claude engine places the job description *before* the resume with a
`cache_control` breakpoint, so screening N candidates for one role pays for the JD once
instead of N times — [pinned by a test](tests/test_llm.py), because a regression there
silently restores full price on every resume. Gemini's caching is implicit and not
controllable that way, so the same trick does not apply.

---

## Design decisions

**Degrade, don't fail.** When the LLM is unreachable the baseline engine still returns a
result, flagged `degraded: true` and labelled in the UI. A screening tool that returns
nothing during a provider outage is worse than one that returns a weaker answer and says
so.

**One rubric, two providers.** `prompts.py` is shared. Comparing engines that were given
different instructions would measure prompt differences, not model differences.

**The baseline is kept deliberately.** Not legacy code — it is the fallback *and* the
control arm. Without it there is no way to demonstrate the LLM adds value.

**Structured outputs, not JSON parsing.** Both engines constrain decoding to the Pydantic
schema. A response that does not validate raises instead of silently scoring zero — and
a safety block must never be read as "this candidate scored 0".

**Model output is untrusted input.** Every model-authored string reaches the DOM via
`textContent`, enforced repository-wide by a test.

**No demographic inference.** The rubric explicitly forbids inferring anything from
names, schools, or locations. Capability and experience are the only permitted signals.

---

## Limitations

Stated plainly, because a screening tool that oversells itself is a liability:

- **The evaluation set is 14 pairs labelled by one person**, and the AI engine now scores
  1.0 on it — meaning the set is saturated and no longer discriminating. Growing it with
  real, consented, multi-annotator data is the single highest-value improvement available.
- **Scores run optimistic.** Ranking is reliable; absolute values are not calibrated.
- **Scanned/image-only PDFs are rejected**, not OCR'd.
- **No batch endpoint.** One resume per request. Ranking 200 candidates against one JD is
  the obvious next feature.
- **No persistence.** Results are returned and logged, never stored.
- **~13s per screening.** Fine for one resume, too slow for a synchronous batch UI — that
  work belongs on a queue.
- **LLM scores are not perfectly reproducible.** Temperature is 0.2, not 0. This is why
  the evaluation measures ranking rather than exact agreement.
- **This tool must not make hiring decisions on its own.** It ranks and explains to
  support a human reviewer. Automated screening carries real legal and ethical exposure;
  the evidence and `red_flags` fields exist to make a human's review faster, not to
  replace it.

---

## License

MIT
