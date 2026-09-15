# Technical & IP Diligence Review

A Python application for TEN Capital Network. You give it an investor pitch deck (PDF, PPT or PPTX). It produces a **one-page PDF** that assesses the company's technical architecture, development maturity and intellectual-property position. The assessment is written from the perspective of later-stage venture diligence, strategic investment, acquisition or exit readiness.

It answers one question:

> *Is this company's technology and IP sufficiently mature, defensible, scalable, documented and transferable to withstand rigorous technical and legal diligence?*

It is a decision-support tool, not a deck summarizer. Every finding keeps:
- its source slide;
- the excerpt it rests on;
- a classification of **FACT**, **INFERENCE**, **ASSUMPTION** or **NOT PROVIDED**.

---

## Contents

1. [What you get](#what-you-get)
2. [Quick start](#quick-start)
3. [Running it](#running-it)
4. [Configuration](#configuration)
5. [How it works](#how-it-works)
6. [Scoring methodology](#scoring-methodology)
7. [Reliability safeguards](#reliability-safeguards)
8. [TEN Capital record and consent](#ten-capital-record-and-consent)
9. [Samples](#samples)
10. [Testing](#testing)
11. [Project structure](#project-structure)
12. [Extending](#extending)
13. [Limitations](#limitations)

---

## What you get

Each run writes three files:

| File | Contents |
|---|---|
| `<company>_tech_ip_review_<timestamp>.pdf` | One-page US Letter review with these sections: company snapshot, technical assessment, intellectual property, top risks, exit/deal-closing exposure, five priority diligence questions, investor conclusion. |
| `<company>_tech_ip_review_<timestamp>.json` | The full validated structured analysis: every finding, score, red flag, question, assumption and missing item, with slide references. |
| `<company>_tech_ip_review_<timestamp>_ten_capital_record.json` | The TEN Capital lead-intelligence record. It is always saved locally and transmitted **only** with explicit consent. |

The PDF reports:
- an **Overall Technical & IP Diligence Score** (0–100, shown with an uncertainty band such as `41 ± 12`);
- an **Evidence Confidence** score;
- ten category scores (1–5);
- **Technical Position** and **IP Position**: Strong / Adequate / Mixed / Weak / High Risk;
- **Transaction Readiness**: Ready / Mostly Ready / Material Diligence Required / Significant Remediation Required.

---

## Quick start

Requires Python 3.11 or newer.

```bash
git clone <repo-url> TechDiligenceIPRisk
cd TechDiligenceIPRisk

python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# macOS / Linux:       source .venv/bin/activate

pip install -r requirements.txt        # runtime
pip install -e ".[dev]"                # optional: adds pytest and the `tdip` command

cp .env.example .env                   # Windows: copy .env.example .env
# Optional: set ANTHROPIC_API_KEY in .env to use the Claude engine
```

Try it on a bundled sample. This needs no API key:

```bash
python -m app.main analyze samples/decks/quickmind_ai_seed_extension.pdf --engine rules
```

---

## Running it

### Command line

```bash
python -m app.main analyze path/to/deck.pdf
python -m app.main analyze deck.pptx --engine llm --output-dir reports --user analyst@fund.com
python -m app.main analyze deck.pdf --consent-share-with-ten-capital
```

| Option | Meaning |
|---|---|
| `--engine auto\|llm\|rules` | `auto` (default) uses Claude when credentials exist, otherwise the deterministic rules engine. `llm` requires Claude and fails if it is unavailable. `rules` makes no network calls. |
| `--output-dir DIR` | Where the PDF and JSON files go. Default: `TDIP_OUTPUT_DIR`, or `./output`. |
| `--user ID` | Identifier recorded in the TEN Capital record. |
| `--consent-share-with-ten-capital` | Explicit consent to transmit the record. Without this flag, nothing is sent. |
| `--quiet` | Print only the output paths. |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Success |
| `2` | Deck could not be read (malformed, unsupported or too large) |
| `3` | Analysis or validation failed |
| `4` | The report could not be fitted to one page |

### Web interface

```bash
python -m app.main serve                      # http://127.0.0.1:8040
# or
uvicorn app.web.server:app --host 0.0.0.0 --port 8040 --workers 1
```

The page follows the TEN Capital tool-page template:
- one upload field;
- an optional email;
- a consent checkbox, unchecked by default;
- one button.

It shows progress while the analysis runs, then downloads the PDF automatically. It also links to the JSON analysis and the TEN Capital record. Jobs are held in process memory, so run a **single worker**.

API, if you want to script against the server:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/jobs` | Multipart fields `deck` (file), `user` (optional), `consent` (`granted` only when the user ticked the box). Returns `202 {job_id, status_url}`. |
| `GET` | `/api/jobs/{id}` | Status, current step, result summary, download links. |
| `GET` | `/api/jobs/{id}/report.pdf` | The one-page PDF. |
| `GET` | `/api/jobs/{id}/analysis.json` | The structured analysis. |
| `GET` | `/api/jobs/{id}/record.json` | The TEN Capital record. |
| `GET` | `/healthz` | Liveness check. |

### Deploy to Railway

The repository includes a `Dockerfile` (Python 3.12) and `railway.json`:
- health check on `/healthz`;
- one replica, because jobs are held in memory;
- restart on failure.

`.railwayignore` keeps `.env`, tests and generated files out of uploads.

```bash
railway init --name ten-capital-tech-ip-diligence
railway add --service tech-ip-diligence \
  --variables "ANTHROPIC_API_KEY=sk-ant-..." \
  --variables "TDIP_ENGINE=llm" \
  --variables "TDIP_APP_PASSWORD=<strong password>"
railway domain --service tech-ip-diligence
railway up --service tech-ip-diligence --ci      # build, deploy, stream build logs
```

Notes:
- **Always set `TDIP_APP_PASSWORD` on a public URL.** Every upload spends Anthropic API credits. The browser asks for a username and password: any username works, with this password. `/healthz` stays open for Railway's health check.
- **Secrets live only in Railway service variables**, never in the image or the repository. To rotate the API key, run `railway variable set ANTHROPIC_API_KEY=<new key> --service tech-ip-diligence`, which redeploys automatically.
- **Analyses run as background jobs and the page polls for progress**, so multi-minute Claude runs are not cut off by HTTP timeouts.
- **Railway's filesystem is ephemeral.** Reports are kept for `TDIP_WEB_JOB_TTL_MINUTES` (default 60) and are lost on redeploy. Download them when the job finishes.
- **Logs:** `railway logs --service tech-ip-diligence`.

### From Python

```python
from app.pipeline import run_diligence
from app.integrations.ten_capital import ConsentStatus

run = run_diligence("deck.pdf", user="analyst@fund.com", consent=ConsentStatus.NOT_GRANTED)
print(run.analysis.scores.overall_score, run.pdf_path)
```

---

## Configuration

All settings come from environment variables with the `TDIP_` prefix. A `.env` file at the project root is loaded automatically. See [`.env.example`](.env.example) for the full annotated list.

| Variable | Default | Purpose |
|---|---|---|
| `TDIP_ENGINE` | `auto` | `auto`, `llm` or `rules` |
| `ANTHROPIC_API_KEY` | — | Credentials for the Claude engine |
| `TDIP_LLM_MODEL` | `claude-opus-5` | Model used by the LLM engine |
| `TDIP_LLM_EFFORT` | `high` | `low` … `max` |
| `TDIP_LLM_MAX_TOKENS` | `64000` | Output budget for the analysis call (includes adaptive thinking) |
| `TDIP_LLM_SERVER_FALLBACKS` | `true` | Server-side refusal fallbacks (beta) |
| `TDIP_LLM_ATTACH_PDF` | `true` | Send the PDF itself so Claude can see diagrams |
| `TDIP_MAX_UPLOAD_MB` / `TDIP_MAX_SLIDES` | `50` / `250` | Ingestion limits |
| `TDIP_SOFFICE_PATH` | — | LibreOffice binary, required for legacy `.ppt` |
| `TDIP_UNVERIFIED_SCORE` | `2` | Score for a category with no deck evidence (1–3) |
| `TDIP_OUTPUT_DIR` | `./output` | Output folder |
| `TDIP_FONT_DIR` | — | Folder containing `OpenSans-Regular.ttf`, `OpenSans-Bold.ttf` and `OpenSans-Italic.ttf` |
| `TDIP_LOG_LEVEL` / `TDIP_LOG_FILE` | `INFO` / — | Logging |
| `TDIP_TEN_CAPITAL_ENDPOINT` | — | HTTPS endpoint that receives consented records |
| `TDIP_TEN_CAPITAL_API_KEY` | — | Bearer token for that endpoint |
| `TDIP_WEB_HOST` / `TDIP_WEB_PORT` | `127.0.0.1` / `8040` | Web server |
| `TDIP_WEB_MAX_CONCURRENT_JOBS` | `2` | Upload concurrency limit |

**Fonts.** The house font is Open Sans. Drop the three TTFs into `app/assets/fonts/` or point `TDIP_FONT_DIR` at them. Without them the report falls back to Helvetica, which renders the same layout.

---

## How it works

```
upload ─► ingestion ─► analysis engine ─► finalize (Python) ─► validation ─► PDF + JSON ─► TEN Capital record
           │              │                  │
           │              │                  └─ scores, evidence caps, confidence, ratings, questions
           │              ├─ rules engine: signal catalog, severity escalation, gap detection
           │              └─ LLM engine: Claude structured output ─► grounding ─► safety-net merge
           └─ PDF (pdfplumber → pypdf fallback), PPTX (python-pptx), PPT (LibreOffice → PPTX)
```

### 1. Ingestion (`app/ingestion/`)

- **Validation before parsing.** Each file is checked for existence, size, extension and file signature (`%PDF`, ZIP with `ppt/presentation.xml`, or the OLE header for `.ppt`). A misnamed file is caught before a parser touches it.
- **PDF.** Reads per-page text, detects the slide title from font size, extracts ruled tables and counts images. If pdfplumber fails, pypdf recovers plain text and a warning is logged. Password-protected files get a specific message.
- **PPTX.** Reads titles, text boxes, grouped shapes, tables, chart titles/categories/series, SmartArt diagram text and speaker notes, in reading order.
- **Legacy PPT.** Converted with LibreOffice when it is available. Otherwise the user gets a clear instruction to re-save the file as PPTX or PDF.
- **Snapshot extraction.** Stage, revenue, growth, gross margin, customers, concentration, raise, funding to date and engineering headcount are extracted with slide numbers. Projections ("projected revenue of $50M by 2029") are never reported as revenue.
- **Slide numbers.** Every extracted fact keeps its slide or page number. Image-only slides are flagged, because no OCR is performed.

Every failure becomes a `DeckIngestionError` subclass with a message safe to show a user, and is logged.

### 2. Analysis engines (`app/analysis/`)

Both engines produce the same intermediate draft, which `engine.finalize` turns into a validated `DiligenceAnalysis`.

**Rules engine** (deterministic, offline):
- `signals.py` holds a catalog of about 90 diligence signals across the ten categories. Examples: SOC 2, penetration testing, contractor-built code, copyleft licenses, founder dependency, university licenses, foundation-model APIs, customer-specific code branches, single points of failure. Signals are matched sentence by sentence, with negation detection ("no GPL components") and roadmap detection ("SOC 2 planned for 2027").
- `red_flags.py` maps signals to diligence templates (issue, consequence, question, mitigation, transaction exposure) and escalates severity in context. For example, contractor-built code is CRITICAL when no invention assignments are evidenced and MEDIUM when they are. It also raises NOT PROVIDED flags for expected evidence that is absent.
- `technical_analysis.py` and `ip_analysis.py` compare the deck against a checklist of what later-stage diligence expects. Each unmet expectation becomes an explicit gap.

**LLM engine** (`llm_engine.py`, Claude Opus 5):
- One streamed request reads the slide-tagged deck text, plus the PDF itself so diagrams are visible.
- The response is JSON validated against a Pydantic schema, with one automatic repair round if validation fails. The schema is sent as instructions: the full diligence schema exceeds the API's grammar-size limit for constrained structured outputs.
- It returns snapshot facts, findings, dependencies, red flags, proposed scores, questions and narratives.
- The output is then verified by `grounding.py` (see [Reliability safeguards](#reliability-safeguards)) before any of it is used.
- If the API is unavailable in `auto` mode, the run falls back to the rules engine and says so in the report.

### 3. Finalize, validate, report

- `risk_scoring.py` computes category scores, evidence caps, the weighted overall score, evidence confidence, uncertainty, and the three conclusion ratings. This happens in Python for both engines.
- `utils/validation.py` rejects the analysis before any PDF is produced if any of these checks fail:
  - every FACT has a real slide reference and excerpt;
  - scores reconcile;
  - section references resolve;
  - NOT PROVIDED fields carry no values.
- `reporting/pdf_generator.py` builds the page at successively tighter density levels until it fits, then verifies the rendered file has exactly one page (see [One page, guaranteed](#one-page-guaranteed)).

---

## Scoring methodology

### Category scores (1–5)

| Score | Meaning |
|---|---|
| 5 — Strong | Well-supported evidence, mature practices, limited material risk |
| 4 — Good | Generally strong with manageable gaps |
| 3 — Moderate | Material unanswered questions requiring diligence |
| 2 — Weak | Meaningful technical or IP concerns |
| 1 — High Risk | Potential financing, transaction, scalability or ownership issue |

**Rules engine.** Each category starts at a neutral 3:
- Positive evidence adds its weight; negative evidence subtracts its weight.
- FACTs count in full and INFERENCEs at half. ASSUMPTIONs and planned items count zero.
- A category with no evidence at all receives `TDIP_UNVERIFIED_SCORE` (default 2). Undocumented is not the same as strong.

**Evidence caps**, applied to both engines, including scores proposed by Claude:

| Condition | Maximum score |
|---|---|
| Fewer than two corroborating positive deck facts | 4 |
| No positive deck fact | 3 |
| Any HIGH red flag in the category | 3 |
| A CRITICAL red flag in the category | 2 |
| Two CRITICAL red flags in the category | 1 |
| Exit readiness, when any CRITICAL issue exists anywhere | 2 |

### Overall score (0–100)

The overall score is the weighted average of category scores, mapped linearly so that 1 → 0 and 5 → 100.

| Category | Weight |
|---|---|
| Technical Architecture | 15% |
| Product / Development Maturity | 10% |
| Scalability | 10% |
| Engineering Organization | 10% |
| Technical Documentation | 5% |
| Cybersecurity / Compliance | 10% |
| IP Ownership | 15% |
| IP Defensibility | 10% |
| Third-Party Dependency | 5% |
| Exit / Transaction Readiness | 10% |

### Evidence confidence (0–100)

Evidence confidence adds up four components:

| Component | Points | What it measures |
|---|---|---|
| Category coverage | up to 45 | Categories with at least one deck FACT |
| Depth | up to 20 | Number of FACTs |
| Snapshot completeness | up to 15 | Business snapshot fields provided |
| Extraction quality | up to 20 | Readable slides, parse warnings, grounding rejections |

It is capped at **85**: a pitch deck is self-reported and never equals a data-room review. The overall score is printed with an uncertainty band of ±4 to ±20, derived from confidence.

### Conclusion ratings

- **Technical Position** and **IP Position** come from the weighted average of their categories:
  - Strong ≥ 4.25
  - Adequate ≥ 3.5
  - Mixed ≥ 2.75
  - Weak ≥ 2.0
  - High Risk below 2.0
- A CRITICAL flag in a category group limits that position to Weak or worse.
- **Transaction Readiness:**
  - Any CRITICAL flag → *Significant Remediation Required*.
  - Three or more HIGH flags, or an exit score of 2 or less → *Material Diligence Required*.
  - *Ready* needs an overall score ≥ 75, no HIGH flags and confidence ≥ 60.
  - Confidence below 40 can never be rated Ready or Mostly Ready.

### Red-flag severity

| Severity | Meaning |
|---|---|
| CRITICAL | Could materially prevent investment, acquisition, financing, licensing or closing |
| HIGH | Likely to become a major diligence issue or require remediation |
| MEDIUM | Should be investigated but appears manageable |
| LOW | Minor gap or documentation issue |

Each flag carries: issue, severity, classification, supporting evidence with slide references, business consequence, diligence question, suggested mitigation, and how it would surface in a transaction. That last item is one or more of: diligence question, closing condition, reps & warranties, indemnity, escrow, purchase-price adjustment, delay/termination risk.

---

## Reliability safeguards

| Requirement | How it is enforced |
|---|---|
| Never fabricate patents, certifications, customers, ownership or open-source usage | The LLM engine may only mark a FACT when its quote is found on a slide. Claims about certifications, patents, freedom to operate, open-source licenses or IP ownership are **removed** if the specific term never appears in the deck. The check is term by term: a deck mentioning SOC 2 does not support an ISO 27001 claim. Numeric snapshot values must appear in the deck text. Dependencies must be named in the deck. |
| Never present an assumption as fact | Ungrounded FACTs are downgraded to INFERENCE with no excerpt. Combination flags are INFERENCE; absence flags are NOT PROVIDED. |
| Never treat a roadmap as completed | Positive claims in plan/roadmap/use-of-funds language are recorded as `planned`, carry zero weight, and are listed as "Planned only". Unsupported "production" maturity claims are downgraded. |
| Never treat patents as defensibility | A single fact cannot score 5. The defensibility text always states that patents are not proof without claim-to-product mapping. |
| Never claim freedom to operate | The report states "No freedom-to-operate evidence" unless the deck says an FTO analysis was obtained. |
| Preserve slide references | Every FACT, snapshot field and red flag keeps its slide numbers. Tests confirm each excerpt appears on its cited slide, in both PDF and PPTX formats. |
| Don't lose deterministic findings | Signal-backed CRITICAL/HIGH flags from the rules engine are merged into LLM results if the model missed them. |
| Validate before reporting | `validate_analysis` blocks PDF generation on structural or traceability errors. |
| Handle bad files gracefully | Signature checks, parser fallbacks, typed user-facing errors, logged failures. |

### One page, guaranteed

The generator tries up to six density levels. Each level:
- shortens lower-priority text;
- reduces top risks from five toward three;
- drops the security and AI lines;
- tightens the type.

Diligence questions and the investor conclusion are kept at every level. If content still overflows, a final shrink-to-fit pass runs. The output is then checked with pypdf. A second page raises `OnePageError` instead of being written.

---

## Automated result emails

Every completed analysis, from the web app or the CLI, is emailed through [Resend](https://resend.com) to `TDIP_REPORT_EMAIL_TO`. The default recipient is `Info@tencapital.group`.

Each email contains:
- a branded summary: score, evidence confidence, maturity, positions, readiness, conclusion, top risks, priority diligence questions, company snapshot and run details;
- attachments: the one-page PDF and the structured analysis JSON.

What the uploader's consent checkbox changes:

| | Consent not granted | Consent granted |
|---|---|---|
| Summary, PDF, analysis JSON | Included | Included |
| Uploader's email address | Withheld | Included |
| Reply-To header (answering reaches the uploader) | Not set | Set to the uploader's email |
| TEN Capital lead record JSON | Not attached | Attached |

The upload page states this up front whenever emails are configured.

| Variable | Default | Purpose |
|---|---|---|
| `RESEND_API_KEY` | — | Resend API key. Without it, no email is sent |
| `TDIP_REPORT_EMAIL_TO` | `Info@tencapital.group` | Recipients (comma-separated) |
| `TDIP_REPORT_EMAIL_FROM` | `TEN Capital Tech & IP Diligence <reports@tencapital.group>` | Sender. The domain must be verified in Resend |
| `TDIP_EMAIL_REPORTS` | `true` | Set `false` to switch emails off without removing the key |

Delivery is best-effort:
- A failed send is logged (`railway logs`), and the analysis and downloads still complete.
- Each email uses the record ID as an idempotency key, so a retry cannot deliver the same email twice.
- To skip the email for a single CLI run, use `--no-email`.
- The test suite removes `RESEND_API_KEY` from its environment, so tests never send real email.

## TEN Capital record and consent

The record contains:
- the user, company, sector, stage, raise, revenue, growth and gross margin;
- the overall score, uncertainty and evidence confidence;
- all ten category ratings, the maturity level, and the technical, IP and transaction positions;
- critical and high-severity red flags;
- the top five diligence questions;
- major missing information, summary findings and recommended diligence areas;
- the consent status, transmission status, creation timestamp, source file hash and engine used.

Consent rules:

- The record is **always generated and saved locally**.
- `consent_status` is `NOT_GRANTED` by default.
- Consent becomes `GRANTED` **only** through an explicit per-run action:
  - the CLI flag `--consent-share-with-ten-capital`;
  - the web checkbox, which is unchecked by default and must submit exactly `granted`.
- `resolve_consent()` accepts only the literal boolean `True`. Strings such as `"true"` or `"yes"`, the number `1`, and environment variables never grant consent. There is intentionally no configuration setting for consent.
- `submit_record()` refuses to open a network connection unless consent is `GRANTED`. It also requires HTTPS (localhost excepted), sends an `Idempotency-Key`, and reports `SENT`, `FAILED`, `NOT_CONFIGURED` or `NOT_SENT_NO_CONSENT` in the saved record.

---

## Samples

| Path | What it is |
|---|---|
| `samples/decks/helix_grid_systems_series_b.pptx` / `.pdf` | Mock company with strong technology and clean IP |
| `samples/decks/quickmind_ai_seed_extension.pdf` / `.pptx` | Mock company with contractor-built code, copyleft open source, founder dependency, weak documentation and unresolved university IP |
| `samples/output/*_tech_ip_review.pdf` | Sample one-page reports |
| `samples/output/*_analysis.json` | Sample structured output |
| `samples/output/*_ten_capital_record.json` | Sample TEN Capital records (consent not granted) |

Both companies are fictional. Regenerate everything with:

```bash
python -m samples.generate_samples
```

The committed samples were produced with the rules engine, so they are reproducible without an API key.

---

## Testing

```bash
pip install -e ".[dev]"
python -m pytest
```

The suite covers:
- PDF and PPTX ingestion: titles, tables, grouped shapes, notes, charts, images;
- malformed, truncated, misnamed, oversized and legacy files, including checks that failures are logged;
- missing fields, and missing IP-only or technical-only information;
- source-reference retention in both formats;
- score calculation, weights, evidence caps, confidence and uncertainty;
- risk severity, escalation, negation and roadmap handling;
- the one-page constraint, including very long company names, Unicode names and deliberately overflowing content;
- consent logic, where transports fail the test if a network call is attempted without consent;
- structured TEN Capital record generation;
- the LLM engine with a fake Anthropic client: request shape, grounding of fabricated claims, fallbacks, retries, schema strictness;
- the web upload flow and the CLI;
- the two mock companies, including a check that the high-risk company scores at least 40 points lower with at least three critical flags.

No test calls the Anthropic API.

---

## Project structure

```
app/
  main.py                    CLI entry point (analyze, serve)
  config.py                  Environment-driven settings
  pipeline.py                End-to-end run shared by CLI and web
  ingestion/
    extractor.py             Validation, format dispatch, snapshot extraction
    pdf_parser.py            pdfplumber + pypdf fallback
    ppt_parser.py            python-pptx; LibreOffice conversion for .ppt
    document.py              Format-neutral deck model
    errors.py                User-facing ingestion errors
  analysis/
    signals.py               Diligence signal catalog and detection
    red_flags.py             Flag templates, escalation, gap flags
    technical_analysis.py    Architecture, maturity, engineering, docs, security, scalability
    ip_analysis.py           Patents, trade secrets, ownership, software/data, AI/data
    risk_scoring.py          Scores, caps, confidence, ratings
    diligence_questions.py   Prioritized questions
    llm_engine.py            Claude structured-output engine
    grounding.py             Quote verification and claim scrubbing
    engine.py                Engine selection, rules draft, finalize
    draft.py                 Engine-neutral intermediate result
  models/schemas.py          Pydantic schema for the structured analysis
  reporting/
    pdf_generator.py         One-page ReportLab report
    templates.py             Palette, fonts, density levels
  integrations/ten_capital.py  Record, consent, transmission
  utils/                     Logging, validation, text and context helpers
  web/                       FastAPI server and upload page
templates/
  tech_ip_review_template.md    Document structure template: sections, fields, format rules (no company data)
  tech_ip_review_template.json  Machine-readable version of the same template
samples/                     Mock decks, generator, sample outputs
tests/                       pytest suite
```

---

## Extending

- **Add a diligence signal.** Append a `SignalRule` to `RULES` in `app/analysis/signals.py` with:
  - category, topic and polarity;
  - weight (0.5 minor … 1.5 major);
  - a short label;
  - regex patterns;
  - optionally, a flag code.

  If the signal should raise a red flag, add a `FlagTemplate` to `TEMPLATES` in `red_flags.py`. Add any context escalation in `derive_flags`.
- **Change weights.** Edit `DEFAULT_WEIGHTS` in `app/analysis/risk_scoring.py`. Validation enforces that they sum to 1.
- **Change the model.** Set `TDIP_LLM_MODEL`. Grounding, caps and validation apply regardless of model.

---

## Limitations

- The analysis is limited to the uploaded deck. It is **not** legal advice, a patent-validity or freedom-to-operate opinion, a code audit or a security assessment. Every finding needs verification against the data room.
- No OCR is performed. Image-only slides are flagged and excluded from the rules engine. With the LLM engine and a PDF upload, Claude can read them, but its claims about them are classified as INFERENCE.
- The rules engine recognises English phrasing. Unusual wording may go undetected and appear as NOT PROVIDED, which is the conservative failure mode.
- Legacy `.ppt` needs LibreOffice installed.
