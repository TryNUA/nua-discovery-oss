# AGENTS.md

Instructions for any AI agent working in this repo (Claude Code, Codex, Cursor or similar). Read this first, then `docs/SPEC.md`.

## What this repo is

A knowledge base of one team's customer discovery calls, built so that an AI can answer questions across **every** call rather than a retrieved handful. What the discovery is about lives in `config/domain.yml`: the company, the team, the market, the problem areas being explored and who gets interviewed. Read it before you answer anything; it gives you the vocabulary and what counts as relevant. The people on the team are in `config/people.yml`, the seats (roles interviewees speak from) in `config/seats.yml`.

When the pipeline is deployed, calls arrive from the call recorder (Granola, or Circleback) through a GCP pipeline that commits the transcript, writes a digest and posts a brief to Slack. The bot commits to `main` all the time, so run `git pull` before you answer anything.

## Setup (local clone)

1. Install with [uv](https://docs.astral.sh/uv/): `uv sync`. It builds `.venv` from `uv.lock`, dev tools included.
2. Copy `.env.example` to `.env` and add the model keys the team uses (`GEMINI_API_KEY` is enough with the default models in `config/pipeline.yml`).
3. Codex: allow network access when asked, or set `sandbox_workspace_write.network_access = true`, since `ask` calls model APIs and prep files and change requests are pushed to GitHub.
4. Check it works: `uv run python -m nua ask "which seats have we talked to?"`

An MCP server (claude.ai connector) exists in `service/mcp.py` but is experimental and off by default. If your team has deployed it, its tools follow the same rules as this file.

## Reading order for any question about calls or beliefs

1. `beliefs.md`: what the team is betting on (Beliefs) and what it is tracking (Watching)
2. `beliefs-status.md`: evidence for and against each, by seat, with quotes, and which are under pressure
3. `decisions.md`: live decisions and the beliefs they rest on
4. `findings.md`: themes from calls that no belief covers yet, plus ungrouped recent findings
5. `open-questions.md`: what is still unanswered and which seat to ask
6. `calls.csv`: one row per call, for filtering by person, firm, seat, date, recorder, status
7. `claims.csv`: one row per finding (its quotes joined by ` | `), for filtering by seat, scope, belief
8. `digests/*.md`: structured per-call summaries
9. `transcripts/*.md`: raw source, to verify a quote or when a digest is thin

## Cross-call questions

In Slack, anyone in a channel the bot is in can mention it: "@<bot_name> <question>", where `bot_name` is set in `config/domain.yml` (default "Nua Discovery"). Filters `since:YYYY-MM-DD` and `seat:"<seat>"` go anywhere in the message. It answers in the thread.

Never answer a question that spans the corpus from a subset of digests. Run:

```
uv run python -m nua ask "<question>" [--since YYYY-MM-DD] [--seat "<seat>"]
```

- It reads every digest and prints a coverage line ("read N/N digests"). Pass the coverage line on to the person asking.
- It can take a few minutes. Run it with a 10-minute timeout or in the background.
- If it fails, say so. Do not read `claims.csv` in full as a substitute. If a partial answer is wanted, filter `claims.csv`, and state the filter and which calls it covered.

## Pre-call prep

When asked to prep for a call:

1. Find prior calls with the person or firm in `calls.csv`, and mentions of them in digests (`mentions`). List any open follow-ups: what we owe them, what they offered.
2. List beliefs under pressure and Watching items this seat can speak to.
3. Pull open questions for this seat from `open-questions.md`.
4. Write at most 5 questions, the most decision-relevant first, each tagged with the B-/W- id it tests if any.
5. Save to `prep/<planned-date>_<firstname-lastname>.md` in exactly this shape, then commit and push:

```markdown
---
planned_date: YYYY-MM-DD
contact: <full name; ask if you only have a first name>
firm: <firm>
attendee_email: <if known>
seat: <from config/seats.yml>
caller: <a person key from config/people.yml>
---

## Context
<prior calls, mentions, open follow-ups>

## Questions
1. <question> (<B-### or W-### if any>)
```

The pipeline reads only the `## Questions` list. It matches the prep file to the call by attendee email, then by contact name, within 7 days of `planned_date`, and reports which questions were answered, dodged or never asked.

## Changing beliefs

- Never edit `beliefs.md` directly. The processor is its only writer.
- When someone on the team asks you to add, watch, promote, reword, split, merge, retire or keep a belief, or to dismiss a verdict as a wrong read, write a change request and push it:

```yaml
# requests/<YYYY-MM-DD>_<who>_<short-slug>.yml
action: add            # add | watch | promote | reword | split | merge | retire | keep | dismiss_verdict
target: null           # B-/W- id for everything except add, watch and merge
targets: []            # merge only: the ids to merge
statements: []         # split only: [{statement, scope}, ...]
statement: <one line>
scope: {line: ..., party: ..., segment: ..., seat: ...}
source: {call_id: <call id>, quote: "<verbatim quote>"}
requested_by: <a person key from config/people.yml>
reason: <optional>
verdict: null          # dismiss_verdict only, optional: supports | contradicts | scope_mismatch
requested_at: <ISO timestamp with offset>   # optional; keeps an identical request filed again distinct
```

- `requested_by` must be a key in `config/people.yml`, or the request is rejected as invalid.
- The deployed processor applies the request: it assigns the id, writes the history line, backchecks every past call, announces it in Slack and deletes the request file. If the finding is already a belief, it says so instead of adding a second one. Without a deployment, nothing applies requests.
- `decisions.md` is edited by hand. Only change it when someone on the team asks, and take the next free `D-###`.

## Hard rules

- **Quotes are verbatim.** Copy from the transcript exactly, including ASR garble. Clean-up belongs in the claim text, never in the quote.
- **No claim without a quote.** A claim that cannot be traced to a transcript span does not go in a digest or an answer.
- **A finding is one point with 1 to 3 quotes.** Digests store findings under `claims`, each with a `quotes` list, every quote verified against the transcript. Cite any one of them as call id plus quote. A finding found by both extraction models has `found_by: 2`.
- **Recorder notes are not evidence.** The recorder's AI overview above the transcript is context only. Quote the transcript.
- **Generated files are never edited by hand:** `beliefs-status.md`, `findings.md`, `open-questions.md` (except its manual section), `calls.csv`, `claims.csv`, `digests/`, and `config/themes.yml`, `config/proposals.yml`, `config/rejected.yml`, `config/verdict_overrides.yml`, `config/backcheck.yml`, `config/batches/`.
- **Never edit a transcript** after ingest.
- **Corrections to a call's contact, firm, seat, role or date** go in `config/overrides.yml`, keyed by call id, when someone on the team asks. The pipeline rewrites the digest from it. The call id itself never changes.
- **Never resolve a contradiction on your own.** Report it with both quotes and a scope diagnosis. The team decides.
- **Use `config/entities.yml` to widen searches and fill structured fields,** never to rewrite a quote. ASR garbles names, so the same firm can show up spelled two ways across transcripts (the shipped example maps "guide wire" to Guidewire).
- **Mind speaker labels.** On calls marked `me_them`, `diarized` or `single_stream`, who said what is inferred. Say so when it matters.
- **Skip stub digests** (`status` of `internal`, `parse_failed` or `failed`) when answering questions.

## Conventions

- Transcripts: `transcripts/granola_<note_id>.md`, `transcripts/circleback_<meeting_id>.md`, or the original name for dropped files. The pipeline reads `transcripts/` flat; files in subfolders are not processed.
- Call id: `YYYY-MM-DD_firstname-lastname` (call date in the configured time zone, `timezone` in `config/pipeline.yml`, default America/Los_Angeles; main external speaker), or `undated_firstname-lastname` when the date is unknown. It is the filename stem in `digests/`. Each digest's `transcript` field points to its transcript file, and its `prep` field to its prep file.
- Prep files: `prep/<planned-date>_<firstname-lastname>.md`, named before the call, so they may not match the call id.
- Ids: `B-###` beliefs, `W-###` watching, `D-###` decisions. Findings are cited as call id plus one of their quotes.
- Every digest records `prompt_version` and models, so a prompt change means re-running affected digests. Prompts are Python modules in `nua/prompts/` (one str constant per part). `prompt_version` in `config/pipeline.yml` covers `extract.py`, `consolidate.py`, `verdict.py` and `arbiter.py` there, plus the model ids.
- Dry run any pipeline change: `uv run python -m nua run <transcript> --dry-run`.

## Style for anything the team reads

Tight and direct. No em dashes. No filler phrases. Per-call briefs lead with what changed, not with a recap.
