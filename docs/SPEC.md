# Nua Discovery: spec

How the pipeline behaves. Code docstrings point into this file by section name (`SPEC "Nightly sweep"`), so the section headings stay stable.

A knowledge base of one team's customer discovery calls. What the discovery is about (the company, who it sells to, what it is exploring, and optionally what the scope fields and roles mean) is configured in `config/domain.yml`, and the team in `config/people.yml`. Calls come in from Granola or Circleback, get digested in the cloud (Google Cloud or AWS) when they land, and are read in Slack, by any AI agent working in a clone of the repo (`AGENTS.md`), or through an experimental MCP server.

Examples in this spec use the shipped example domain, a fictional startup ("Acme": Alex, Sam and Jordan) exploring AI for P&C insurance. Every name, firm and quote in them is made up.

## What it is for

Weekly uses, in priority order:

1. **Post-call brief.** Minutes after a call, a Slack post that leads with what the call changed against the team's beliefs.
2. **Pre-call prep, on demand.** "Prep me for my call with Jane at Example Mutual on Friday" in Claude Code or Codex.
3. **Cross-call questions.** "What do adjusters say about how often store video is usable?" answered from every call, with verbatim quotes.

Relationship memory (follow-ups, intro offers, who we know at a firm) is captured in each digest and surfaced in prep, with no dedicated feature.

Scale: built for hundreds of calls, for example a backfill of 70 and then 15+ new calls a week.

## Principles

1. **Read everything.** Cross-call answers come from every call and report their coverage ("read 312/312 digests"). Never a sample.
2. **Compute at ingest.** Digests, verdicts and indexes are produced when a call lands. Questions read that layer.
3. **Log freely, believe deliberately.** Every material finding from every call is kept. Only a human turns a finding into a belief.
4. **Quotes are verbatim and checked by code**, against the transcript body only, never the recorder's AI overview.
5. **Two models extract, a third arbitrates.** Verdicts that decide what changed are cross-checked, across providers when more than one is configured.
6. **The repo is the source of truth.** Anything worth knowing can be read from a clone with no server involved. The (experimental) MCP server is a read and request layer over the repo, never a second store.

## Design decisions

| Topic | Decision | Why |
|---|---|---|
| Recorders | One shared Granola Business workspace. Circleback per person, alongside or instead of Granola. Individual Granola accounts are not used for discovery calls | Granola's API only exists on Business, and one workspace key reads the whole Team space |
| Granola filter | Everything in the Team space is ingested. No folder filter | Simplest rule. Private "My notes" stay out. Anything else shared to Team (an investor call, say) gets briefed too |
| Circleback filter | Only meetings tagged `discovery` | Keeps investor and internal calls out |
| Clients | Slack (briefs, buttons, the bot), and any AI agent working in a clone (`AGENTS.md`). A remote MCP server exists but is experimental | No custom frontend to build or maintain |
| Web frontend | None | Slack, agents and GitHub's markdown rendering cover reading |
| Storage | Your private GitHub repo. Plus one bucket for the MCP server's sign-in state, only when MCP is deployed; nothing about calls or beliefs goes there | Agents read it natively, belief history lives in git, GitHub renders markdown. The MCP sign-in files need a durable, shared folder, which the repo cannot be |
| Trigger | Recorder webhooks, plus a nightly sweep and an hourly Granola sweep as safety nets | Granola never replays a failed event, Circleback retries are undocumented |
| Receiver | On a webhook, fetch the overview and transcript, commit them as a markdown file, return 200. All other work happens after | The recorder retries if anything fails before the 200 |
| Compute | Google Cloud: Cloud Run receiver, one Cloud Tasks queue, one Cloud Run processor, Cloud Scheduler. AWS runs the same design on Lambda, SQS, Fargate and EventBridge (`docs/self-hosting-aws.md`) | Scales to zero, one worker, a few dollars a month. GitHub Actions is not used |
| Belief tracking | Curated: findings (automatic), Watching and Beliefs (human-approved) | An automatic ledger at 15+ calls a week turns into noise and false conflicts |
| Seeding beliefs | Derived from the backfill, approved in Slack | No existing list to trust |
| Approvals | Slack buttons | Fastest loop for a small team |
| Models | Anthropic, OpenAI and the Gemini API, all direct, in any mix. Two extract, a third arbitrates. The default is Gemini only (one key); cross-provider is the recommended upgrade, validated with the bake-off | Independent providers catch each other's misses |
| Prep | On demand from an agent, saved to `prep/` | No calendar integration |
| Brief layout | Short top message with what changed, details in a thread | Channel stays readable at 15+ briefs a week |
| Backfill | Drop the existing .md exports. Live calls come through the APIs | Fastest. Missing dates go on a fix-up list |
| Confidentiality | Transcripts are stored in the private repo and sent to the configured model providers | Choose providers and API tiers whose terms fit your calls |

## Architecture

```
Granola webhook ──────┐                     Cloud Run: receiver (public, scales to zero)
Circleback webhook ───┤                     ingest: verify, fetch, create transcript file, enqueue, 200
GitHub push webhook ──┼──────────────────>  github: enqueue for new transcripts and change requests
Slack clicks/mentions ┤                     slack: lock the item, open forms, queue questions
Cloud Scheduler ──────┘                     sweep, rollup: enqueue
                                                        │
                                                        v
                                            Cloud Tasks queue `nua` (one task at a time)
                                                        │
                                                        v
                                            Cloud Run: processor (private)
                                            clone, run task, commit, push, post to Slack
                                                        │
                           ┌────────────────────────────┼────────────────────────┐
                           v                            v                        v
                   GitHub repo (truth)          Slack #discovery-calls     LLM APIs (Gemini,
                           │                                               Anthropic, OpenAI)
                           v
             Cloud Run: mcp (experimental; sign-in limited to config/people.yml)
                           │
                           v
             claude.ai / Claude Code / Codex   (or a local clone)
```

One container image is deployed as the Cloud Run services `nua-receiver`, `nua-processor` and, when switched on, `nua-mcp`.

### Who writes what

| Writer | Files |
|---|---|
| Receiver | New transcript files only, through a create-only helper |
| Processor | New transcript files (sweep, same helper); `digests/`; all generated indexes; `beliefs.md`; `config/themes.yml`, `config/proposals.yml`, `config/rejected.yml`, `config/verdict_overrides.yml`, `config/backcheck.yml`, `config/batches/`; deletes request files once handled |
| MCP server (write tools, through the GitHub App) | `requests/`, `prep/`. It enqueues its own work, because `/github` ignores the app's pushes |
| The team, directly or through an agent | `decisions.md`, `prep/`, `requests/`, `config/overrides.yml`, the hand-maintained configs (`domain.yml`, `people.yml`, `entities.yml`, `seats.yml`, `pipeline.yml`), the prompts in `nua/prompts/`, the manual section of `open-questions.md` |

One exception: the empty `beliefs.md` is committed by hand once, when the repo is set up.

Generated indexes are pure functions of the digests, `beliefs.md`, `decisions.md`, `config/themes.yml`, `config/verdict_overrides.yml` and `config/overrides.yml`, with no model calls. When a processor push is rejected, it re-fetches, regenerates them and pushes again.

## Ingest

### Recorder setup

**Granola**
- One Business workspace for everyone who runs discovery calls (priced per seat; Granola has a startup program).
- An admin creates a **workspace API key**. It never expires and reads the Team space plus any space with "Allow Granola API access" on. It cannot read private notes.
- Turn off "Allow Granola API access" on every space except Team, including any new one (it defaults to on).
- One webhook endpoint created with the workspace key: `scopes: ["workspace"]`, no `folder_ids`, events `note.generated`, `note.access_granted`, and optionally `note.edited`.
- Team rules:
  - A discovery call is shared to the Team space. Everything else stays in My notes.
  - The Granola iOS app saves notes to your oldest workspace. Leave any older workspace, or move iOS notes into the shared one.
  - Transcript auto-deletion stays Off. Granola keeps no audio, so a deleted transcript is gone.

**Circleback**
- Pro for each person who records. Free API access only reaches back 30 days, and webhooks need Pro.
- Each person creates one webhook automation:
  - condition: tag is `discovery`
  - outcomes: Transcript, Notes, Attendees (the transcript is only sent if selected)
  - URL: `/ingest/circleback/<person>`. The payload has no owner field, so the URL says who recorded it.
- Tag the meeting before it ends, or set up automatic tagging. A tag added later is picked up by the nightly sweep.
- Each person's API key and signing secret go into Secret Manager. Circleback keys can also edit and delete meetings.

### Receiver endpoints

| Endpoint | Caller | Does |
|---|---|---|
| `POST /ingest/granola` | Granola | Verify the Standard Webhooks signature. Fetch `GET /v1/notes/{id}?include=transcript`, paging `/transcript` on a `413`. Create the transcript file. Enqueue `process_call`. Return 200 within 15s |
| `POST /ingest/circleback/<person>` | Circleback | Verify the `x-signature` HMAC over the raw body. The payload holds the transcript. One `GET /calendar/events?startTimeLookbackHours=...` call for the start time, matched on `meetingId` (fall back to `createdAt`). Create the file, enqueue, 200 |
| `POST /github` | GitHub App push webhook | See below. Respond within 10s |
| `POST /slack/interact` | Slack | See Slack interactions |
| `POST /slack/events` | Slack | Verify the Slack signature, answer the `url_verification` challenge, queue an `ask` task for an `app_mention`. See Asking in Slack |
| `POST /sweep` | Cloud Scheduler | Verify the Scheduler OIDC token. Enqueue `sweep` (body may carry `granola_only: true`) |
| `POST /rollup` | Cloud Scheduler | Verify the Scheduler OIDC token. Enqueue `rollup` (body may carry `bootstrap: true`) |

Ingest rules:
- **Create-only write.** The transcript file is created through the GitHub Contents API with no `sha`, so it can never overwrite. A conflict or ref race is retried inside the 15s budget.
- **Redelivery still enqueues.** If the file already exists, the receiver skips the write and still enqueues. Task idempotency (see Task types) makes the duplicate harmless.
- **503 on any failure, never a 3xx or 4xx.** Granola treats most 4xx responses as permanent and drops the event. It retries 5xx for up to 4 days.
- **Ingest cutoff.** Recorder items created before `ingest_cutoff` in `config/pipeline.yml` (a go-live timestamp with offset, e.g. `2026-11-02T09:00:00-08:00`) are acknowledged with 200 and ignored. Dropped exports cover everything before it. A date-only value (`2026-11-02`) means midnight in the configured time zone (`timezone` in `config/pipeline.yml`, default `America/Los_Angeles`), for the receiver and the sweep alike. While `ingest_cutoff` is unset or unreadable, the receiver acknowledges every recorder item with 200 and ignores it, like the sweep, and logs an error: before go-live, dropped exports are the only source. Nothing is lost, because once the cutoff is set the sweep ingests every item created on or after it. A Granola note counts by the later of its creation and its calendar start: Granola creates a note when a meeting is opened ahead of time, so a call held after the cutoff can have a note created before it.
- **No transcript yet.** A recorder item with no transcript text (a Granola note still processing, a Circleback payload without a transcript) is acknowledged with 200 and ignored. No file is written, so the sweep ingests it once the text is there.
- **`note.edited`.** Re-fetch the transcript. If it is longer than the stored one (someone hit Resume), create `transcripts/granola_<note_id>_v2.md` (or the next version) and enqueue it. A longer version of the same note supersedes the earlier one (see Duplicates). If it is the same length as the latest stored version and no digest points to that version yet, enqueue that version (a redelivery whose earlier enqueue was lost). Otherwise ignore the event.

`/github` rules:
- Verify `X-Hub-Signature-256`. Ignore pushes whose sender is the app itself (the receiver and processor enqueue their own work).
- Look only at files **added** in the pushed commits:
  - `transcripts/*`: if the push adds more than 5, enqueue one `backfill` task carrying the list of added paths. Otherwise enqueue `process_call` per file.
  - `requests/*`: enqueue `apply_belief_change` per file.
- A push that changes `config/overrides.yml` enqueues `reindex`.
- Files in a subfolder of `transcripts/` are never processed: the pipeline reads `transcripts/` flat. `/github` posts a notice for them to `#discovery-ops`, and the nightly sweep names them in its ops summary until they are moved up.
- GitHub never redelivers a failed webhook. The nightly sweep covers misses.

### Transcript file

Written once, never edited afterwards. API ingests are named by source: `transcripts/granola_<note_id>.md`, `transcripts/circleback_<meeting_id>.md`. Dropped files keep their original names.

```markdown
---
source: circleback            # granola | circleback
source_id: 12345
ical_uid: abc@google.com      # Circleback only; null when unknown
start: 2026-09-23T16:00:00-07:00
start_source: calendar        # calendar | transcript | created
recorded_by: alex
attendees:
  - {name: Jane Doe, email: jane@example.com}
speaker_labels: named         # named | me_them | diarized | single_stream
ingested_at: 2026-09-23T16:41:10-07:00   # when the receiver or sweep created the file, in the configured time zone
---

## Recorder notes (context only, not evidence)

...Granola or Circleback AI overview, verbatim...

## Transcript

[1:13] **Alex Rivera**: ...
[1:14] **Jane Doe**: ...
```

`start` comes from Granola `calendar_event.scheduled_start_time`, else the first transcript item's time. For Circleback it comes from the calendar event, else `createdAt`.

`ingested_at` is stamped by whoever creates the file (receiver or sweep). The write is create-only, so a redelivery never changes it. It is the arrival order the duplicate rule uses.

`speaker_labels` records how much to trust who said what:

| Value | Source | Meaning |
|---|---|---|
| `named` | Circleback; Granola with speaker tags on Zoom, Meet or Teams | Real names per turn |
| `me_them` | Granola desktop | "Me" is the recorder's mic, "Them" is everyone else |
| `diarized` | Granola iOS | Speaker A, Speaker B |
| `single_stream` | In-person or speakerphone on a laptop | Everything is "Me" |

Dropped files are committed exactly as dropped. `parse.py` reads them in place.

### Origin: live or backfill

Every transcript is either `live` or `backfill`, decided from the file itself, never from whoever enqueued it or from git history:
- `live`: written by the receiver or sweep, so its front-matter has `source: granola` or `source: circleback`.
- `backfill`: a dropped file, committed as-is with no front-matter.

The digest records `origin`, and re-runs keep it. Only `live` calls ever get a brief. A dropped file never does, however few are dropped.

### Backfill

- Drop exports into `transcripts/` through the GitHub web UI (up to 100 per upload). A push of more than 5 makes `/github` enqueue one `backfill` task with the list of added paths and the push's head commit as the batch id. Smaller drops are processed one by one, with no brief and no summary.
- `backfill` first commits `config/batches/<id>.yml` (processor-written) with the path list, then enqueues a `process_call` for each path that no digest points to. A retry of the task keeps the file's paths, creation time and `summary_ts`. Order does not matter: each call records `checked`, and backcheck covers any belief approved mid-batch.
- Dates come from the filename or the Granola footer. A footer date has no year; take the latest year that puts it on or before the commit date. Anything else gets `date_source: unknown`.
- Every `process_call` on a backfill transcript, including one that skips, runs the completion check afterwards: if every path in its batch file has a digest or stub, and no Slack message carries `batch:<id>` metadata, it posts one summary: counts, `failed` stubs, and the fix-up list (unknown dates, unknown firms). The nightly sweep runs the same check for every batch file without a `summary_ts`, so a lost post is recovered.
- Once the summary is posted (or found), the check writes its Slack ts into the batch file as `summary_ts` (processor-written) and commits it. A batch file with `summary_ts` counts as complete with no Slack lookup, and the sweep skips it. The metadata lookup only reaches the channel's latest messages, so without this an old summary would scroll out of reach and be posted again.

### Call id

- `call_id` is `YYYY-MM-DD_firstname-lastname`: the call date in the configured time zone (`timezone` in `config/pipeline.yml`, default `America/Los_Angeles`) and the main external speaker. The processor assigns it after extraction. The receiver cannot know the contact for phone and in-person calls, where the API lists only the owner.
- A call with `date_source: unknown` gets `undated_firstname-lastname`.
- A second call with the same person on the same day gets `-2`.
- Stub digests (see below) use the transcript's filename stem as their call id. When a digest of another transcript already has that id (a drop named by this convention, such as `transcripts/2026-09-23_jane-doe.md`, next to the live call's digest), the stub takes the first free `<stem>-2`, `<stem>-3` and so on, so a stub never overwrites another call's digest.
- The call id names the digest and never changes once assigned. A wrong contact, firm, seat or date is fixed in `config/overrides.yml`, not by renaming.

### Duplicates

Two transcripts are the same call when (`ical_uid` equal and same date in the configured time zone) or (`start` within 10 minutes and an overlapping external attendee email), or when they are versions of the same Granola note. An internal email (see Internal calls) never counts as the overlap: the recorder is on every call they record, so two ad-hoc calls one person took minutes apart stay separate.
- The richer copy wins: a later version of the same note beats an earlier one; otherwise `named` beats every other label type; on a tie, the first to arrive wins: by `ingested_at` when every tied copy has one, otherwise a copy that already has its own digest counts as first (files without a stamp predate it).
- There is one digest per call. It lists the other copies under `duplicates:`, which counts as a digest pointing to them.
- The duplicate check runs only on transcripts that no real (non-stub) digest names in its `transcript` field; a digest's own transcript always wins its own re-runs. A copy's own `failed` or `parse_failed` stub does not count, so a Re-run of a failed richer copy still supersedes under the same call id, and a losing copy's own stub is deleted in the commit that records it under the winner's `duplicates`. It compares against every transcript of the same call in the repo, digested or not. A copy that loses adds itself to the winner's digest or stub `duplicates` if the winner has already run, and otherwise stops; the winner lists every same-call copy when it runs.
- `process_call` is delayed 15 minutes (Cloud Tasks `scheduleTime`) when the invite includes another person from `config/people.yml`, so a second recording has time to land before either copy is processed.
- **Supersede:** if a richer copy arrives after the call already has a digest, the call is re-digested from it under the same call id, and the brief is updated in place (see Outputs). Exception: once any history line, verdict dismissal or rejection cites the call, it is never superseded. The richer copy is only added under `duplicates`, because those records are matched on the existing quotes.

### Internal calls

A call is internal only if it lists **two or more** attendees and every one has an internal email: an address on an `internal_email_domains` domain (`config/pipeline.yml`) or the email of someone in `config/people.yml`. It gets a stub digest with `status: internal` and no brief. A call with only the owner listed (phone, in-person, ad-hoc) is **not** internal.

### Nightly sweep

Cloud Scheduler, 2am in the configured time zone. The `sweep` task:
1. **Recorders:** lists every Granola note visible to the workspace key and every Circleback meeting tagged `discovery` for each person's key, created on or after `ingest_cutoff`. Compares `source_id`s against transcript front-matter in the repo and ingests anything missing through the create-only helper. Granola notes created up to 30 days before the cutoff but updated after it are fetched too, and taken when their call started after the cutoff.
2. **Transcripts:** enqueues `process_call` for any transcript that no digest points to. Files in a subfolder of `transcripts/` are not processed; the ops summary names them until they are moved up.
3. **Briefs:** re-posts the brief for any `live` call with `status: digested`, no `brief` recorded and either a call date in the last 48 hours or, at any age, a live copy under `duplicates` (a supersede whose brief update failed; the re-post finds the old top message through that copy's metadata).
4. **Requests:** enqueues `apply_belief_change` for every file still in `requests/` (every final outcome deletes its file, so anything left was missed or hit a transient failure).
5. **Reindex and backcheck:** enqueues `reindex`, and `backcheck` (which skips itself when nothing is missing).
6. **Reminders:** with each person's Circleback key, lists meetings created since the last sweep that have an external invitee and no `discovery` tag, and posts them once to `#discovery-ops`.
7. **Stale digests:** if `reprocess_stale: true` in `config/pipeline.yml`, enqueues `process_call` for up to 40 digests whose `prompt_version` is out of date, skipping any marked `reprocess_failed` at the current version. A stale re-run that keeps the digest (the transcript is gone, the call now reads as internal, or the transcript no longer parses) also sets `reprocess_failed`, so it is picked once. Only the parse case posts to `#discovery-ops`, once.
8. **Backfill batches:** runs the backfill completion check for every `config/batches/*.yml` without a `summary_ts` (see Backfill).

It posts to `#discovery-ops` only for reminders or when it found something the webhooks missed.

An hourly Scheduler job (`sweep-granola`, at :30) runs the sweep with `granola_only: true` (step 1 for Granola only), so a note whose webhook never arrived (for example, one moved into the Team space) is picked up within the hour instead of at 2am.

## Processing

### Task types

All tasks run on the processor, one at a time, from queue `nua`. Delivery is at least once, and task names only block duplicates for a short while, so correctness rests on each task's own check:

| Task | Enqueued by | Skips when |
|---|---|---|
| `process_call` | receiver, `/github`, `backfill`, sweep, Re-run | a digest already points to this transcript at the current `prompt_version`, unless forced by Re-run |
| `backfill` | `/github` | never; it only enqueues paths no digest points to |
| `apply_belief_change` | Slack clicks and forms, `/github`, sweep | its `ref` is already recorded (see History lines), or the request file is already gone |
| `backcheck` | `apply_belief_change` (5 minutes out), sweep | no entry is missing its current version from any digest |
| `reindex` | `/github`, sweep | never; it regenerates |
| `rollup` | Scheduler | every item for this week (or the bootstrap) is already posted; otherwise it posts only the missing ones |
| `sweep` | Scheduler | never |

### `process_call`

In this order:

1. **Parse** (`nua/parse.py`, plain code). Split the recorder notes from the transcript body, so quote checks never match the overview. Pull metadata from front-matter, a Granola footer or the filename. Number the turns. Input shapes: the standard format above; a Circleback copy (`[m:ss] **Name**: text` below an `#### Overview` block); a Granola text export (footer, then `Transcript:` and one block). An unrecognized shape gets a `parse_failed` stub and an error in `#discovery-ops`.
2. **Origin:** `live` or `backfill`, from the transcript's front-matter.
3. **Internal check.** Internal calls get a stub.
4. **Duplicate check** (see Duplicates for when it runs). If this copy loses, record it under the winner's `duplicates` if the winner has run, then stop. If it wins and another copy already has a digest, continue as a supersede (unless that call is cited) and take over its call id.
5. **Extract** (both extraction models, in parallel). Each gets the numbered transcript, the recorder notes labeled as context only, the known seats (`config/seats.yml` plus every seat on a digested call, from `calls.csv`), `config/entities.yml`, the team from `config/people.yml` and the domain brief from `config/domain.yml`. Each returns, schema-enforced with retries:
   - who: contact, firm, seat, institution type, role in the thesis
   - summary, 5 bullets max
   - findings (stored under `claims`): one point stated in `text`, backed by 1 to 3 quotes, each with its turn(s), `question_turn` for a short answer (8 words or fewer) and speaker; scope and flags per finding

   A finding is material when it could change what the team believes or builds: how the work is done, who decides and buys, cost and time, pain, what was tried and why it failed, willingness to pay, market structure, or a disagreement with the team's assumptions. Personal career history and company trivia (funding, IPO plans, headcount) are not findings unless they change how the company buys or works; the person's role goes in the who fields. Statements that make one point become one finding with several quotes, from different parts of the call if need be. There is no target count. The prompt (`nua/prompts/extract.py`) gives the reason for each rule and worked examples.
   - follow-ups, mentions, and up to 3 suggested next questions (`ask_next`)

   The first extractor listed in `config/pipeline.yml` is primary. Its who fields win, and it is the fallback when the other is down.
6. **Assign the call id**, or keep the existing one on a re-run or supersede. A successful run on a transcript whose digest is a stub assigns a real call id and deletes the stub in the same commit.
7. **Verify quotes** (plain code).
   - Matching runs on the transcript body with turn markup (`[m:ss] **Name**:`) stripped, ignoring differences in whitespace, case and punctuation. An offset map points each match back to the raw file.
   - The stored `quote` is the raw transcript text at the matched offset, with turn markup removed, not the model's text.
   - A quote may cross a turn boundary. It records every turn it covers and gets `attribution: inferred`.
   - A short answer must cite the turn of the question it answers in `question_turn`.
   - Each quote of a finding is checked on its own. A miss gets one repair request (for that quote) to the model that produced it. A second miss drops the quote and lists it under `verification.dropped`. A quote that lands on the span of an earlier quote of the same finding is folded into it. A finding is dropped only when none of its quotes survives (`verification.findings_dropped`).
   - The first-pass match rate is recorded, so a weak model or prompt shows up before repairs hide it.
8. **Consolidate** (one model call, the arbiter's model; prompt `nua/prompts/consolidate.py`). It gets both models' verified findings (ids, text, scope, flags, quotes) and returns groups: the input ids that make the same point, with one consolidated text, scope and flags per group. Two models often quote different sentences for the same point, which a span check cannot see. Code then builds each finding: the union of its members' verified quotes (overlapping quotes deduped, at most 3, preferring quotes from different parts of the call, in transcript order), `found_by` = the number of distinct extraction models among the members. The model never writes a quote. Every input finding lands in exactly one group: unknown ids are dropped, an id placed twice stays in its first group, and any id the model leaves out becomes a group of its own. If the call fails or replies with the wrong shape, the findings are merged by quote overlap instead (two findings from different models are the same when any of their quote spans overlap; the better-scoped one leads) and the digest gets `degraded: [no_consolidation]`. Findings found by one model are kept.
9. **Match prep.** A prep file matches by attendee email first, then by contact name (after applying `config/entities.yml`), within 7 days of its `planned_date`. Only its `## Questions` list is used.
10. **Verdicts** (both extraction models, one call each, over the consolidated findings). Each gets the findings with all their quotes, `beliefs.md`, the matched prep questions and surrounding turns. Each returns:
    - for every belief or Watching item a finding bears on: `supports`, `contradicts` or `scope_mismatch`, with finding ids and a one-line reason
    - for each prep question: `answered`, `dodged` or `never_asked`
    - on `me_them`, `diarized` or `single_stream` calls, who said each finding (set on every one of its quotes)

    Compare:
    - Both agree: accepted, `decided_by: agree`.
    - They disagree, including one model linking a finding to a belief and the other not: the **arbiter** (a third model) sees the finding, all its quotes, surrounding turns, the belief and both answers with reasons. `decided_by: arbiter`.
    - Arbiter unavailable: both answers kept, `decided_by: unarbitrated`.
    - One extraction model down: `decided_by: single`.
11. **Write the digest** at `digests/<call_id>.md`: readable markdown, structured data in front-matter. Apply `config/overrides.yml`.
12. **Regenerate indexes** (see Index files).
13. **Brief.** Only for `live` calls with no brief recorded yet, or a supersede (see Outputs). Backfill never posts.
14. **Commit and push** the digest (with the brief's Slack timestamps) and indexes. On a rejected push: re-fetch, regenerate indexes, push again.

### Degraded modes and failures

| Failure | Behaviour |
|---|---|
| One extraction model down | Continue with the primary or whichever is up. `degraded: [one_model]`, brief footer says so |
| Arbiter down | Disagreements marked `unarbitrated` with both answers shown |
| Consolidation call fails | Findings merged by quote overlap. `degraded: [no_consolidation]`, brief footer says so |
| Unrecognized transcript | `parse_failed` stub, error in `#discovery-ops` with a **Re-run** button (left out only when the task is too big for a button value). A transcript that already has a digest keeps it: a stale re-run sets `reprocess_failed` and posts once, a Re-run posts every time |
| Task fails on its last attempt, or hits its cost cap | Posts to `#discovery-ops` with a **Re-run** button (left out when the task, trimmed, still does not fit Slack's 2000-character button value; an empty payload would re-run as an invalid task) and returns 2xx so it is not retried again. For `process_call`, writes a `failed` stub only if the transcript has no digest yet. An existing digest is left untouched apart from `reprocess_failed: <prompt_version>`, which stops nightly retries until a Re-run. The handler knows the last attempt from `X-CloudTasks-TaskRetryCount` |
| Task exceeds its budget | Handler stops itself at 25 minutes and fails the attempt cleanly |

Stub digests (`internal`, `parse_failed`, `failed`) are never re-enqueued automatically, only by Re-run, which gives its task a unique name. `ask` and the indexes skip them.

### Digest schema

```yaml
call_id: 2026-09-23_jane-doe
transcript: transcripts/circleback_12345.md
duplicates: []                   # other transcripts of the same call
status: digested                 # digested | internal | parse_failed | failed
origin: live                     # live | backfill
source: circleback
source_id: 12345
date: 2026-09-23
date_source: api                 # api | filename | footer | manual | unknown
recorded_by: alex
attendees: [{name: Jane Doe, email: jane@example.com}]
contact: Jane Doe
firm: Example Mutual
firm_source: transcript          # transcript | attendee_email | manual
seat: claims adjuster (staff)    # a known seat, or a new short role
institution_type: commercial carrier
role_in_thesis: carrier          # an id from roles in config/domain.yml
speaker_labels: named
duration_min: 38                 # call length from the last turn timestamp; null for untimed exports
prompt_version: v1
models: {extract: [<model id>, <model id>], arbiter: <model id>}
degraded: []                     # one_model | no_arbiter | no_consolidation
brief: {channel: C123, ts: "1727470000.000100", thread: ["1727470001.000200"]}
prep: prep/2026-09-23_jane-doe.md      # null when none matched
summary: [5 bullets max]
claims:                          # findings: one point each, 1 to 3 quotes
  - id: c01                      # local to this digest; other files cite call_id + any of its quotes
    text: Stores send hours of video in which only seconds show the incident, so she wants a tool that marks the moment
    quotes:                      # in transcript order; the first is the one briefs show and Slack keys by
      - quote: "they'll send me three hours of tape and the fall is maybe ten seconds of it."
        turns: [138]
        question_turn: null      # short answers: the turn holding the question
        offset: [41210, 41288]
        speaker: Jane Doe
        attribution: span        # span | inferred
      - quote: "if something just jumped me to the right minute, that alone would save my afternoon"
        turns: [139]
        question_turn: null
        offset: [41380, 41463]
        speaker: Jane Doe
        attribution: span
    scope: {line: general liability, party: insured, seat: claims adjuster (staff)}
    flags: {hedged: false, firsthand: true, prompted: false}
    found_by: 2                  # distinct extraction models among the findings consolidated into it
belief_checks:
  - target: B-004                # B-### or W-###
    verdict: contradicts         # supports | contradicts | scope_mismatch
    claims: [c04]
    decided_by: agree            # agree | arbiter | unarbitrated | single
    votes: {<model id>: contradicts, <model id>: contradicts}
    source: ingest               # ingest | backcheck
    note: Staff adjuster at a carrier, B-004 came from independent adjusters
checked: {B-004: "1727560000.000100:prop-07"}   # entry id: version this digest was checked against
new_findings: [c01, c07]         # snapshot at brief time; findings.md recomputes
question_coverage:
  - {question: "...", status: dodged, claims: [c09], decided_by: agree}
follow_ups:
  - {direction: we_owe, what: send her the pricing one-pager, who: Jane Doe, quote: "..."}
  - {direction: they_offered, what: an intro to her claims director, who: Jane Doe, quote: "..."}
ask_next: [...]                  # suggested next questions, from the primary extractor
mentions: {people: [...], firms: [...]}
verification: {quotes: 14, matched_first_pass: 13, repaired: 1, dropped: [], findings_dropped: 0, follow_up_quotes_dropped: 0}
```

`verification.quotes` counts quotes checked across every finding of both models; `dropped` lists the quotes that failed after their repair, and `findings_dropped` the findings left with none. A digest written before findings carried several quotes (one `quote`, `turns`, `offset`, ... per claim) still reads, as a finding with that one quote.

`verification.follow_up_quotes_dropped` counts follow-up quotes that were not found in the transcript body (made up, or taken from the recorder notes). Those follow-ups are kept, since `what` is model text anyway, with an empty quote, and the digest's verification line says how many.

A finding's `scope` says where it holds. Every key is optional. What `line`, `party` and `segment` mean in your market is described under `scope` in `config/domain.yml`; the shipped example:
- `line`: line of business or coverage, e.g. workers' comp, commercial auto, cyber, property, general liability
- `party`: the kind of firm it is about, e.g. carrier, MGA, broker, insured, TPA, reinsurer, vendor
- `segment`: the market, e.g. small commercial, middle market, E&S, personal lines, specialty
- `seat`: the seat it comes from. A seat matching a known one (case ignored) is spelled as known; anything else is kept, lower-cased and cut to 60 characters, so the list grows itself

`role_in_thesis` places the contact relative to what the team is building. The ids and their meanings come from `roles` in `config/domain.yml` (keep `expert` and `competitor`); the shipped example:
- `carrier`: works at an insurer or reinsurer
- `intermediary`: broker, MGA, TPA or independent adjusting firm
- `insured`: buys insurance, e.g. a risk manager at a fleet
- `vendor`: sells data, software or services to insurers
- `expert`: consultant, advisor or former executive
- `competitor`: builds something close to what the team builds

Stub digests carry only `call_id`, `transcript`, `duplicates`, `status`, `origin`, the parsed metadata and a `reason`. They are named `digests/<transcript-stem>.md`, or `digests/<transcript-stem>-2.md` (then `-3`...) when another transcript's digest already has that stem as its call id (see Call id).

### Index files

| File | One row per | Columns or contents |
|---|---|---|
| `calls.csv` | digest | call_id, transcript, status, origin, date, date_source, contact, firm, seat, institution_type, role_in_thesis, recorded_by, source, speaker_labels, claims, prompt_version |
| `claims.csv` | finding | call_id, claim_id, date, speaker, attribution (both of the first quote), seat, line, party, segment, text, quote (every quote of the finding, joined by ` \| `), belief_targets (`;`-joined, with verdicts), found_by |
| `findings.md` | theme | Findings that no current belief check touches, grouped by the themes in `config/themes.yml`, with call and seat counts. Findings not yet in a theme sit under "Ungrouped since <last roll-up>", newest first, each with call id, seat and its quotes |
| `beliefs-status.md` | belief and Watching item | Evidence for and against by seat with quotes; under pressure (see below); coverage ("checked 70/70 calls", counting digests whose `checked` holds the entry's current version) |
| `open-questions.md` | question | Prep questions dodged or never asked, dropped once a later digest marks the same question text answered; for each belief under pressure, the seats contradicting it and the seats not yet heard from; a hand-edited section between `<!-- manual:start -->` and `<!-- manual:end -->` that the generator preserves |

## Knowledge layers

| Layer | Where | Decided by | Checked on every new call |
|---|---|---|---|
| Claims (findings with their quotes) | digests, `claims.csv` | pipeline | n/a |
| Findings | `findings.md` | pipeline | no |
| Watching | `beliefs.md`, Watching section | a person on the team | yes |
| Beliefs | `beliefs.md`, Beliefs section | a person on the team | yes |
| Decisions | `decisions.md` | a person on the team | via linked beliefs |

- **Findings** in `findings.md` are digest findings (stored as claims) that touch no belief or Watching item. Nothing is dropped. A one-call finding stays visible in `findings.md` and searchable in `claims.csv`.
- **Watching** is for "interesting, not sure yet". It gets checked on every call like a belief, and the brief reports hits separately.
- **Beliefs** are what the team is betting on. 15 to 40 entries.

### `beliefs.md`

Decided by the team, **written only by the processor**. Agents and humans change it through Slack buttons or change requests, never by editing the file. That keeps one id counter and guarantees every change gets a history line and a backcheck.

```markdown
## Beliefs

### B-001 Video footage is the most valuable evidence on a liability claim
- scope: line: general liability, commercial auto; party: carrier
- history:
  - 2026-10-05 added by Alex via bootstrap, source 2026-09-23_jane-doe "Video is the first thing I ask for on any fall claim.", ref 1727560000.000100:prop-07

## Watching

### W-004 Commercial lines get less claims software than personal lines
- scope: party: carrier; segment: small commercial
- history:
  - 2026-10-06 watched by Sam via slack, source 2026-09-23_jane-doe "Commercial gets the leftover software budget, honestly.", ref 1727470001.000200:c01

## Retired
```

- Evidence is **not** stored here. `beliefs-status.md` holds it.
- Links to decisions live only in `decisions.md`.
- Ids are assigned by the processor. A Watching item promoted to a belief gets a new `B-` id; the `W-` entry moves to Retired with "promoted to B-0xx". Watching items never go under pressure.
- An entry's **version** is the `ref` of its latest add, watch, promote, reword, split or merge history line. Keep and dismissals do not change it.

### History lines

Format: `YYYY-MM-DD <action> by <who> via <slack|agent|bootstrap>[, source <call_id> "<quote>"[; <call_id> "<quote>"...]][, reason: <text>], ref <message_ts>:<block_id> | ref requests/<file>@<blob sha>`. Quotes escape `"` and `\`.

An add or promote from a roll-up proposal records every evidence source, not just one.

The `ref` is the idempotency key for `apply_belief_change`. Rejections in `config/rejected.yml` and dismissals in `config/verdict_overrides.yml` carry the same `ref`.

### Under pressure

Computed in `beliefs-status.md`, never stored in `beliefs.md`. A belief is under pressure when `contradicts` verdicts decided `agree` or `arbiter`, and not dismissed, come from at least 2 calls in 2 different seats (thresholds in `config/pipeline.yml`). What counts:
- After a **Keep**, only contradictions from calls dated after the Keep.
- After a **Reword** or **Split**, every verdict against the current version, including backchecked ones.

It clears only when someone on the team acts on it: Keep, Reword, Split or Retire.

A verdict the team judges wrong can be dismissed with the **Wrong read** button on the brief, or through an agent request. It is recorded in `config/verdict_overrides.yml` (call_id, target, quote, offset, verdict, version, who, reason, ref) and matched to verdicts by quote-span overlap, so it survives a re-digest. `offset` is the claim's span, `verdict` the verdict dismissed (the brief's Wrong read button carries it; a request may name it, else every verdict on that span is covered) and `version` the entry's version when dismissed, so a dismissal never hides a verdict against a later wording (after a Reword or Split).

### Change requests

Agents change beliefs by committing a file to `requests/`, through the `request_change` tool or from a clone:

```yaml
# requests/2026-10-06_sam_footage-scrubbing.yml
action: add            # add | watch | promote | reword | split | merge | retire | keep | dismiss_verdict
target: null           # B-/W- id for everything except add, watch and merge
targets: []            # merge only: the ids to merge
statements: []         # split only: [{statement, scope}, ...]
statement: Adjusters lose real time scrubbing long footage for the moment of the incident
scope: {line: general liability, seat: claims adjuster (staff)}
source: {call_id: 2026-09-23_jane-doe, quote: "they'll send me three hours of tape and the fall is maybe ten seconds of it."}
requested_by: sam      # a person key from config/people.yml
reason: null
verdict: null          # dismiss_verdict only: supports | contradicts | scope_mismatch (optional)
requested_at: 2026-10-06T14:05:00-07:00   # optional; the MCP tool writes it so identical requests get distinct refs
```

The processor keys a request on its path plus its git blob sha (`requests/<file>@<blob sha>`) and skips a key it has already recorded. The `request_change` tool writes `requested_at` so an identical request filed again the same day gets new bytes, a new sha and a new ref. When committing a request from a clone, include `requested_at` too (or vary the file name): a request byte-identical to one applied and deleted earlier that day, at the same path, is skipped as already recorded.

`/github` enqueues `apply_belief_change`. The processor deletes the request file in the same commit as its outcome, whatever the outcome: applied, duplicate, stale or invalid. Invalid requests (bad YAML, unknown target) fail at once with no retries. The file is kept only when the commit itself failed (GitHub down), so the sweep can retry it. On success the processor schedules a backcheck and posts a one-line notice in `#discovery-calls` ("Sam added B-017 via agent"). Other outcomes get a one-line reply saying why.

### Applying a change

`apply_belief_change`, for any path:
- Skip if the `ref` is already recorded.
- Reject an add or watch when any of its sources (call id plus quote, matched by span overlap) is already in a history line of a current Belief or Watching entry. Reply that it already exists as B-0xx. Retired entries do not block.
- A source is one quote. It points at the finding one of whose quotes it overlaps (any quote, not only the first), and it is placed, matched and held by that quote's span. The same rule applies wherever a stored quote is matched to a finding: verdict overrides, themes, `findings.md`, the brief's locked findings, the roll-up and `ask` citations.
- For a roll-up proposal, read it from `config/proposals.yml`. If its target's version has changed since, mark it stale instead of applying it.
- Otherwise assign the id, write the entry and history line, commit, schedule a backcheck 5 minutes out, then update the Slack item and post the notice, carrying the `ref` as Slack message metadata.
- Slack always comes after the commit. A task that skips on a recorded `ref` still posts or updates the Slack side if no message carries that `ref`, so a Slack failure is repaired on retry.

### `decisions.md`

Hand-edited, short. D-### ids are assigned by hand. Each decision lists the beliefs it rests on, e.g. `D-002 Footage review tool for adjusters: rests on B-001, B-003, B-004`. A contradiction of a belief linked to a decision goes at the very top of the brief.

## Beliefs loop

### Promotion paths

- **From a brief:** every new finding in the thread has a menu with **Make belief** and **Watch**.
- **From Claude Code or Codex:** "make the footage scrubbing finding from the Jane Doe call a belief." The agent writes a change request.
- **From the weekly roll-up:** proposals with buttons.

### Weekly roll-up

Cloud Scheduler, Mondays 8am in the configured time zone, task `rollup`. One model (`rollup` in `config/pipeline.yml`) reads the claims since the last roll-up, all findings, the previous `config/themes.yml`, `beliefs.md`, `decisions.md`, seat counts from `calls.csv`, and `config/rejected.yml`. It first commits `config/themes.yml` (keeping existing theme names where it can, plus a `rollup` record with the roll-up's key, date, the calls it counted as new under `calls`, and `counted`: every call any roll-up has counted, so a call digested late, dated before the last roll-up, is still new exactly once) and `config/proposals.yml` (each proposal's id, type, statement, scope, sources, target and target version, status). It then posts a summary message to `#discovery-calls` with one thread reply per item. On a retry it reads the thread with `conversations.replies` and posts only the missing items:

1. **This week** (summary message): call count, contradictions, beliefs that went under pressure, Watching items that gained evidence.
2. **Under pressure:** each such belief with its contradicting quotes and **Keep / Reword / Split / Retire**.
3. **Proposals**, each with evidence quotes and **Approve / Edit / Reject**:

| Proposal | Trigger (defaults in `config/pipeline.yml`) |
|---|---|
| Add | a finding theme recurs in 3+ calls from 2+ seats |
| Promote | a Watching item reaches the same bar |
| Split | a belief gets 3+ `scope_mismatch` verdicts, or its contradictions all come from one context |
| Retire | contradicted by 3+ calls in 3+ seats |
| Reword | evidence supports a sharper statement |
| Merge | two entries whose supporting claims mostly overlap |
| Drop | a Watching item with no hit in 6 weeks |

4. **Single-call findings worth a look:** up to 5 that are firsthand, unprompted, or from a scarce seat (2 or fewer calls in `calls.csv`). Menu: Make belief, Watch.

The summary flags when Beliefs exceed 40. The summary message carries `rollup:<ISO week>` as Slack message metadata; the task looks for it before posting, and uses the same key to find last week's roll-up, whose unanswered items lose their buttons and roll into the new one.

Rules:
- Anyone in the channel can approve or reject; the history line records them by Slack handle (a teammate's first name when the Slack user matches someone in `config/people.yml`, for example by `slack_id`). Disagreements get settled in person.
- A rejection is stored in `config/rejected.yml` with its type, target, evidence claims (call id plus quote), who, reason and ref. A later proposal of the same type is suppressed unless it cites at least 2 claims outside the rejected evidence (matched by quote-span overlap).

### Slack interactions

Handled by the receiver at `/slack/interact`, which answers within 3 seconds:
- **Locking is per item.** A click locks only the clicked block (by `block_id`), replacing its buttons with "Queued by <Slack handle>" through `response_url`. Other items in the same message stay live. A second click on a locked item gets an ephemeral "already queued by <Slack handle>".
- **Approve, Reject, Keep, Retire, Watch, Wrong read, Re-run:** lock, then enqueue with `ref` = message ts plus `block_id`, so Approve and Reject on the same item collide.
- **Edit, Make belief, Reword, Split:** the receiver opens the Slack form itself (`views.open` with the click's `trigger_id`, which expires in 3 seconds). Menu options only hold 150 characters, so they carry a short key: `call_id|<offset start>-<end>` for a finding (resolved by span overlap, since claim ids change on re-digest) or a proposal id. The form opens at once, pre-filled only from the interaction payload (the clicked message's blocks and the button's value): the text and quote of a finding, a proposal's statement or split parts, a belief's statement. Each statement has an optional scope field, left blank; there is no `views.update` after the form opens. A blank scope keeps the finding's, proposal's (or proposal part's) or belief's own scope, which apply fills in. The form keeps a fingerprint of each pre-filled statement, so a statement left as shown counts as unchanged and apply keeps the stored wording rather than the message's display text (a Reword left unchanged needs a new scope). Nothing locks until the form is submitted. The submission is validated, enqueued and the item locked, all before the response.
- **No repo on this path.** A click or a submission never reads the repo, calls GitHub or loads a model SDK, so it is answered within 3 seconds even when the receiver starts cold. There is no allowlist: anyone in the channel may click, and the change is queued under their Slack handle. Apply shows a teammate's first name when the Slack user matches someone in `config/people.yml`. Change requests pushed to `requests/` must still name a person there. Nothing runs after the response, since the receiver has CPU only while a request is open.

### Asking in Slack

"@<bot_name> <question>" (`bot_name` in `config/domain.yml`, default "Nua Discovery") in any channel the bot is in runs the same `ask` as the CLI (see Cross-call questions) and answers in the thread. Optional filters anywhere in the message: `since:YYYY-MM-DD` and `seat:"<seat>"`. Anyone in the channel may ask.

- The receiver gets Slack's `app_mention` event at `/slack/events` (Event Subscriptions, bot event `app_mention`, scope `app_mentions:read`), verifies the signature and queues an `ask` task named after the event id (a bare mention gets the help text at once), so Slack's resend of an event it thinks was missed is a no-op. Like a click, this path never reads the repo.
- The processor first routes the message with one cheap call (`nua/prompts/route.py`, the `ask_map` model), reading the thread so far when the mention is a reply: a question about the calls is rewritten to stand alone and searched (below); "list beliefs" or "show B-007" is answered from `beliefs-status.md`, "list findings" from `findings.md`, with no search; anything else (a greeting, thanks, a question about the bot, a request it cannot carry out) gets a short reply. If the router call fails, a "?" or 3+ words is searched and anything else gets the help text.
- For a search, it says "On it", runs `ask` over every digest and posts one thread reply: the answer, the quotes it rests on (call id plus verbatim quote, unverified ones marked) and the coverage line. A bad `since` gets a reply saying so.
- `ask` shares the one-at-a-time queue, so a question waits behind a call being processed, and the other way round.

### Backcheck

When an entry is added, watched, promoted, reworded, split or merged, its current version is missing from past digests' `checked`. `process_call` records `checked` for every entry its verdict step ran against, so new calls never need a backcheck. `backcheck`:
1. Finds every entry whose current version is missing from some digest with `status: digested`. A pending backcheck therefore absorbs later approvals, so an approval session runs one backcheck, not thirty.
2. Pre-filters stored claims by each entry's scope, then runs the verdict step (two models, arbiter on disagreement) concurrently inside the task. It reads transcripts only to give the arbiter surrounding turns.
3. On every `status: digested` digest, whether or not the pre-filter matched, drops that entry's earlier `belief_checks`, writes any new verdicts with `source: backcheck`, and records the version in `checked`. Entries are current Beliefs and Watching items only.
4. Regenerates all indexes.

Debounce: every change that gives an entry a new version also writes `config/backcheck.yml` (processor-written) with `last_change: {ref, at}`, and enqueues `backcheck` with `{"after": <ref>}`, `backcheck_delay_seconds` out (default 300, in `config/pipeline.yml`). A backcheck whose `after` is not the latest change, while that change is younger than the delay, makes no model call: it re-enqueues itself under the latest change's task name, which collapses into the one already queued, and exits. The sweep's `backcheck` carries no `after` and runs at once.

### Bootstrap

`beliefs.md` starts empty. After the backfill, someone on the team runs the paused Scheduler job `rollup-bootstrap` once (`gcloud scheduler jobs run rollup-bootstrap --location=<region>`). It groups all findings from the backfill and posts up to 40 proposed Beliefs and Watching items: a summary message with one thread reply per proposal. Approvals trigger one batched backcheck. The weekly roll-up job runs from go-live (`schedules_on`); before the bootstrap it has no beliefs to report on, only themes and proposals.

## Outputs

### Per-call Slack brief

Posted by the bot to `#discovery-calls` a few minutes after a `live` call. The top message:

```
Jane Doe · Example Mutual · claims adjuster (staff) · 38m · Alex · Granola
Touches D-002 (Footage review tool for adjusters)

WHAT CHANGED
Contradicts B-004 "Adjusters can use outside AI tools on claim files"   [2/2 agree]  [Wrong read]
  "Anything outside our own systems is a firing offense here, AI or not."
  Scope: staff adjuster at a carrier, B-004 came from independent adjusters. Likely split.
Supports B-003 "Adjusters lose real time scrubbing long footage"   (now 3 calls, 2 seats)

3 new findings · 1 dodged question · digest on GitHub
```

Order inside "what changed": contradictions (decision-linked first), then scope mismatches, then supports, then Watching hits. Supports collapse to one line ("Supports B-001, B-007") unless a seat is new to that belief.

**No belief touched** (always the case before bootstrap): WHAT CHANGED reads "No belief touched", and the top message shows the 3 strongest new findings with quotes, ranked firsthand, then unprompted, then `found_by: 2`.

Thread replies, in this order:
1. **Summary** (5 bullets) and a footer: quotes matched on the first pass, findings found by 2 vs 1 model, verdicts decided by the arbiter, degraded modes, "no prep matched" when relevant.
2. **New findings**, each one block with its text, its first quote, then its second quote when that is short (140 characters or less) or "+N more quotes" for the rest, and a menu: Make belief, Watch. The menu values and block id use the first quote's span and stay under 150 characters. Split into several replies before Slack's 50-block limit.
3. **Questions** (when a prep file matched): answered, dodged, never asked.

Follow-ups and suggested next questions (`ask_next`) stay in the digest and are not posted in the brief.

Posting rules:
- The top message carries the transcript path as Slack message metadata. Before posting, the processor looks for a message with that metadata (`conversations.history`). If one exists (a retry after a failed push), it reads the thread with `conversations.replies` and updates those messages instead of posting new ones. Slack, not the repo, is the record of what was already posted.
- **Supersede:** when a richer copy replaces the digest, the top message is updated in place. The thread is listed with `conversations.replies`, every bot reply in it is deleted, and the replies are posted again. A call cited by any team action is never superseded (see Duplicates), so no acted-on finding is re-posted. A supersede that fails partway is tried once more through the recorded top message; if that fails too, the digest records no brief, so the sweep re-posts it.
- The digest records the top message and thread reply timestamps as a convenience; Slack stays the record.

Style: tight, no em dashes, lead with what changed.

### MCP server

Experimental: built and tested, never run in production, and off by default (`deploy_mcp = true` in `infra/terraform` deploys it). A remote MCP server on its own Cloud Run service (`nua-mcp`), added once as a connector in claude.ai (which also covers the phone app), Claude Code or Codex, so the team can use the base with no clone and no keys on their laptops.

- **Sign-in:** Google or GitHub OAuth, limited to the people in `config/people.yml`. Built on the Python MCP library (FastMCP) and its OAuth support. GitHub sign-in asks only for `read:user`, so someone who signed in under the broader `user` scope before signs in again, first revoking the OAuth app grant (GitHub Settings > Applications) if GitHub keeps handing back the old one.
- **Time limit:** `ask` stops at 200 s by default (env `MCP_ASK_DEADLINE_SECONDS`), sized to claude.ai's 240 s per-call timeout. Cloud Run's 300 s service timeout is the outer bound.
- **Repo access:** the service keeps a shallow clone and pulls when it is more than 60 seconds old. It never writes files itself; write tools commit through the GitHub App, and the processor does the rest.
- **Model keys** stay in Secret Manager. Nobody needs keys on their laptop.
- **Server instructions** tell connected AIs the same hard rules as `AGENTS.md`: quotes are verbatim, use `ask` for anything spanning the corpus and report its coverage, never answer from a sample, recorder notes are not evidence, mind speaker labels, skip stub digests.
- **Result size:** claude.ai caps a tool result at about 150k characters, so tools return digests and summaries, and transcripts are paged.

Read tools:

| Tool | Returns |
|---|---|
| `ask(question, since?, seat?)` | The `ask` answer with citations and coverage (see below) |
| `list_calls(person?, firm?, seat?, since?, until?, status?)` | Rows from `calls.csv` |
| `get_call(call_id, transcript_page?)` | The digest; a page of the transcript when asked |
| `get_beliefs()` | `beliefs.md` with each entry's status line from `beliefs-status.md` |
| `get_belief(id)` | One entry with its full evidence for and against |
| `get_findings(since?)` | `findings.md`, optionally only recent ungrouped findings |
| `get_open_questions(seat?)` | `open-questions.md`, optionally for one seat |
| `get_prep_context(contact, firm?, seat?)` | Prior calls, mentions, open follow-ups, beliefs under pressure and Watching items for the seat, open questions for the seat. The client AI writes the prep questions from this |

Write tools:

| Tool | Does |
|---|---|
| `request_change(action, ...)` | Commits a change request (same fields as the YAML below), with `requested_by` set from the signed-in account, then enqueues `apply_belief_change` itself. Returns the request file name. The processor applies it and announces the result in Slack |
| `save_prep(markdown)` | Checks the prep template and commits the file to `prep/` |

`ask` over a few hundred calls can take 1 to 2 minutes. If a client times out on long tool calls even with the deadline above, split it into `ask_start` and `ask_result`.

### Cross-call questions

Through the connector, the AI calls `ask`. On a local clone, agents follow the reading order in `AGENTS.md` and run the same thing:

```
uv run python -m nua ask "what do adjusters say about how often store video is usable?" [--since 2026-09-01] [--seat "claims adjuster (staff)"]
```

1. Map: digests with `status: digested`, in batches of about 20, go to a cheap model, 8 batches at a time. Each returns the relevant claims with quotes. Progress prints to stderr.
2. Reduce: a strong model writes the answer from those claims.
3. Every cited quote goes through the same code check.
4. Output: the answer, citations as `call_id` plus quote, and coverage ("read 312/312 digests").

Models are set in `config/pipeline.yml`. On the MCP server the keys come from Secret Manager; on a local clone they go in a gitignored `.env` (see `.env.example`). About $0.25 to $2 per question at 70 to 300 calls.

### Pre-call prep

On request, the AI gathers context (`get_prep_context` through the connector, or the steps in `AGENTS.md` on a clone), writes at most 5 questions, and saves `prep/<planned-date>_<firstname-lastname>.md` (`save_prep`, or a commit from the clone):

```markdown
---
planned_date: 2026-10-02
contact: Jane Doe
firm: Example Mutual
attendee_email: jane@example.com   # if known
seat: underwriter
caller: alex                       # a person key from config/people.yml
---

## Context
Prior calls, mentions, open follow-ups (we owe / they offered)

## Questions
1. ... (B-003)
2. ...
```

Prep files are named by planned date and contact, not by call id. The digest's `prep` field links a call to its prep file (see process_call step 9).

## Infrastructure

### GCP (one project)

Infrastructure is Terraform in `infra/terraform/` (state in a GCS bucket created once by hand), applied in two stages: stage 1 without an image (APIs, service accounts and IAM, secret containers, Artifact Registry, queue), stage 2 with the first image (Cloud Run services and Scheduler jobs). Secret values are added with `infra/push-secrets.sh` and never reach Terraform state. Code ships with `infra/deploy.sh` (Cloud Build, then `gcloud run services update --image`); Terraform ignores the image, so the two do not fight. Service URLs are Cloud Run's deterministic `https://<service>-<project number>.<region>.run.app`. Details and costs: `infra/README.md`. The first-time sequence, end to end: `docs/self-hosting.md`.

- **Cloud Run:**
  - `mcp`: experimental, not deployed by default (`infra/terraform` creates it only with `deploy_mcp = true`). When deployed: public, sign-in required, min 0 instances, max 1 instance, sign-in state on a Cloud Storage volume at `/oauth` (`FASTMCP_HOME`), 300s timeout (for `ask`).
  - `receiver`: public, 30s timeout, scales to zero (min 0 instances, max 1; one instance takes 40 requests at once), CPU only while a request is open (CPU throttling on), startup CPU boost. Slack's 3-second acknowledgement must hold on a cold start, so the receiver starts nothing at startup, keeps its imports light (git, recorder clients and model SDKs load inside the routes that use them; Cloud Tasks on the first enqueue), never touches the repo for Slack clicks, forms, `/sweep` or `/rollup`, and does all its work before it answers. Only the routes that read repo files (recorder ingest, `/github`) clone the repo, on first use, and pull again when the clone is older than 60 seconds; there is no background sync. A click after a long quiet spell can still miss the 3 seconds now and then (the person clicks again); that is accepted. If it happens often, keep one warm instance (`min_instance_count = 1` in `infra/terraform/cloud_run.tf`).
  - `processor`: private, 1800s timeout, invoker is the Cloud Tasks service account only. Same image.
- **Cloud Tasks:** queue `nua`, `maxConcurrentDispatches: 1`, `minBackoff: 60s`, `maxBackoff: 1800s`, `maxAttempts: 5`. `dispatchDeadline` is a per-task field, so one shared enqueue helper sets it to 1800s on every task.
- **Cloud Scheduler**, all in the Terraform `time_zone` variable (default `America/Los_Angeles`, kept equal to `timezone` in `config/pipeline.yml`), all sending an OIDC token the receiver verifies. Every job is paused until go-live, when `schedules_on = true` in Terraform runs all of them except `rollup-bootstrap`:
  - `sweep`: nightly 2am
  - `sweep-granola`: hourly at :30, `granola_only: true`
  - `rollup`: Mondays 8am
  - `rollup-bootstrap`: never scheduled, run by hand after a backfill
- **Secret Manager:** Granola workspace key and webhook secret; Anthropic, OpenAI and Gemini API keys (Gemini on its paid tier, so prompts are not used to improve Google's products); GitHub App private key and webhook secret; Slack bot token and signing secret. Per person who records on Circleback: an API key and a signing secret (`circleback_people` in tfvars, `CIRCLEBACK_PEOPLE` for `push-secrets.sh`). Only when switched on: the MCP OAuth client secret and JWT signing key (`DEPLOY_MCP=1`).
- **Artifact Registry** for the image. **Cloud Logging** for everything.

### GitHub App

Installed on this repo only. Repository permissions: Contents read and write (Metadata read is implied). Subscribed to the Push event, webhook to `/github` with a secret. Commits show as the app. Installation tokens are short-lived, so nothing expires with one person's account. Pushes the app makes do trigger its own webhook, which `/github` ignores.

### Slack app

Bot scopes: `chat:write` (post, update and delete messages, ephemerals, message metadata), `channels:read` and `groups:read` (resolve channel names), `channels:history` and `groups:history` (find posted briefs by metadata, read threads), `app_mentions:read` (questions). Opening and updating forms needs no scope. Interactivity URL is `/slack/interact`; Event Subscriptions URL is `/slack/events` with the bot event `app_mention`. `docs/slack-app-manifest.yml` sets all of this up. Channels: `#discovery-calls` (briefs, roll-ups, belief notices, backfill summaries), `#discovery-ops` (errors, Re-run buttons, sweep catches, untagged-call reminders), `#discovery-test` (staging).

### Repo layout

```
AGENTS.md  README.md  CONTRIBUTING.md  LICENSE
decisions.md                                              # hand-edited
beliefs.md                                                # written by the processor
beliefs-status.md  findings.md  open-questions.md         # generated
calls.csv  claims.csv                                     # generated
transcripts/  digests/  prep/  requests/
config/
  domain.yml            what the discovery is about; every prompt reads it
  people.yml            the team: name, email, slack id, github login, circleback key name
  entities.yml          ASR alias map, with confidence and source per alias
                        (e.g. "guide wire" for Guidewire)
  seats.yml             optional seed seats (e.g. underwriter, pricing actuary); the list
                        grows from the calls. Only these count as "not yet heard from"
  pipeline.yml          model roles (extractors in order, arbiter, ask models, rollup),
                        thresholds, per-task cost cap, prompt_version,
                        ingest_cutoff, reprocess_stale, internal_email_domains, channels
  overrides.yml         hand corrections to call metadata, keyed by call_id
  verdict_overrides.yml dismissed verdicts: span, verdict, entry version (processor-written)
  backcheck.yml         last_change {ref, at}: the backcheck debounce (processor-written)
  rejected.yml          rejected roll-up proposals (processor-written)
  proposals.yml         current roll-up proposals (processor-written)
  batches/              backfill batch path lists, plus summary_ts once the summary
                        is posted (processor-written)
  themes.yml            finding theme assignments, plus the rollup record with its
                        `counted` call list (roll-up-written)
nua/
  __main__.py           the CLI
  parse.py  ingest/granola.py  ingest/circleback.py
  process.py  extract.py  verify.py  merge.py  verdicts.py  digest.py  indexes.py
  beliefs.py  apply.py  brief.py  rollup.py  backcheck.py  sweep.py  ask.py  slack_ask.py
  interact.py  slackio.py  gitops.py  tasks.py  runner.py  settings.py  providers.py  llm.py
  config.py  context.py  ports.py  prep.py  rules.py  callid.py  frontmatter.py  bakeoff.py
  types.py              shared records (pydantic models)
  schemas.py            reply models to strict JSON schemas, lenient field types
  prompts/              extract.py  consolidate.py  verdict.py  arbiter.py  rollup.py  ask.py  route.py
                        (one str constant per prompt part, e.g. SYSTEM, EXTRACT, REPAIR)
  setup/                the local setup page server and `nua init` (UI source in setup-ui/)
service/
  main.py               receiver and processor routes
  mcp.py                MCP server: tools, sign-in, repo sync (experimental)
  worker.py             the AWS Fargate worker that drains SQS
  Dockerfile
infra/
  terraform/            Terraform for APIs, service accounts and IAM, secret containers,
                        Artifact Registry, queue, Cloud Run services, Scheduler jobs, budget
  deploy.sh             builds the image with Cloud Build and rolls it out (code only)
  push-secrets.sh       copies secret values from .env to Secret Manager
  README.md             what each file creates, costs, first-time setup, day 2
  aws/                  the same for AWS: terraform/, bootstrap.sh, deploy.sh, push-secrets.sh
examples/               sample configs for `nua init --example`
tests/
  fixtures/  labels/  expected/  golden/  unit/
pyproject.toml  uv.lock  .env.example  .gcloudignore
docs/
  SPEC.md  local-quickstart.md  self-hosting.md  self-hosting-aws.md  slack-app-manifest.yml
```

`prompt_version` covers `nua/prompts/extract.py`, `nua/prompts/consolidate.py`, `nua/prompts/verdict.py`, `nua/prompts/arbiter.py` and the model ids, and is bumped by hand when any of them change. Prompts are code: a change ships with the next deploy, not with a config commit.

`reindex` also rewrites the front-matter of digests affected by `config/overrides.yml`, so agents reading digests directly and `ask` see the corrected values.

## Testing

- **Unit tests:** parse (three shapes; unknown gets `parse_failed`); quote check (never matches recorder notes, markup stripped, cross-turn quotes, stored quote is the raw text, offsets map to the raw file, each quote of a finding on its own); consolidation grouping, its validation and the fallback merge by overlapping spans; call id (dated, undated, stubs, collisions); origin; duplicate matching and tie rules; internal-call rule; `beliefs.md` read and write and history-line grammar; brief rendering and block limits.
- **Golden set:**
  - `tests/expected/brief_<fixture>.md` is a fixture's brief, against a test `beliefs.md`, a test `decisions.md` (D-002 resting on test B- entries) and a test prep file, all in `tests/golden/`.
  - Hand-label findings (1 to 3 quotes each) for the 4 fixtures in `tests/labels/`. A merged finding matches a labeled one when any of its quotes overlaps any labeled quote by at least 50% of the shorter span.
  - Use the 3 conflicts in `tests/expected/conflicts_first-four.md` as labeled verdict cases against the test `beliefs.md`.
- **Bake-off:** `python -m nua bakeoff [--models ...]`. Each model runs as extractor on the fixtures and as judge on the verdict cases. Scored on finding recall against labels (merged by quote overlap, no consolidation call), first-pass quote match rate, and verdict accuracy. The recall bar is fixed before the bake-off runs. The best judge becomes arbiter. Results go in `pipeline.yml`; the report is written to `docs/research/bakeoff-<date>.md`.
- **Gate:** any change covered by `prompt_version` re-runs the golden set before it ships.
- **Dry run:** `uv run python -m nua run <transcript> --dry-run [--beliefs <path>] [--decisions <path>] [--prep <path>]` prints the digest and brief without committing or posting: the brief goes to stdout, then the digest to stderr. The golden set runs with the test files.

## Costs

| Item | Monthly |
|---|---|
| GCP (Cloud Run scaled to zero, Tasks and Scheduler free tiers, Secret Manager about $0.54, Artifact Registry cents) | a few dollars |
| Granola Business | per seat |
| Circleback (optional) | per person who records |
| LLM calls per call (two extractions, consolidation, verdicts, arbitration) | $0.25 to $2, depending on the models |
| Roll-ups and backchecks | a few dollars a week at 20 calls a week |
| `ask` questions | $0.25 to $2 each; listing beliefs or findings and short replies are nearly free |
| GitHub | $0 (Actions not used) |

`cost_cap_usd_per_task` in `config/pipeline.yml` stops a single task that runs over.

## Not in v1

- Ingest by dropping a file in Slack
- Automatic prep from the calendar
- Reminders for Granola calls left in My notes (would need personal keys, which expire)
- Web frontend, embeddings, vector search

## Open items

- **Recording consent.** Many places (California among them) require every party's consent to record a confidential call. A one-line disclosure at the start of each call is cheap. Not legal advice.
- **Thresholds** for proposals and under pressure. Defaults above, tune after the bootstrap.
- **Unverified Granola behaviour:** whether a note moved into the Team space fires `note.access_granted` on a workspace-scope endpoint. If not, the hourly `sweep-granola` picks such notes up within the hour.
- **Unverified Circleback behaviour:** whether a tag added after processing fires the automation. The nightly sweep covers it either way.
- **MCP sign-in state:** the build keeps sign-in state in a small Cloud Storage bucket mounted into the `mcp` service, so sign-ins survive deploys and scale-to-zero. The alternative is one always-on `mcp` instance with no bucket, where everyone signs in again after each deploy.
- **Exact source placement in history lines (optional):** an optional `@<start>-<end>` span on each history-line source would let belief changes pin the exact passage. Today a source is placed by matching its quote, which is ambiguous only when one call repeats a short quote word for word.

## Recorder facts this design relies on

Checked against vendor docs on 2026-09-27. Recheck them when a recorder changes its API.

- Granola REST API and webhooks: Business plan and up. Webhook payloads carry only ids. Respond within 15s, before heavy processing; 3xx and most 4xx fail permanently; retries for 4 days, then the endpoint is disabled; missed events are not replayed. `note.generated` fires only if the endpoint can already see the note. https://docs.granola.ai/webhooks
- Granola workspace keys: admin-created, never expire, read the Team space and API-enabled spaces (on by default for new spaces), not private notes. Webhook endpoints created with a workspace key must use `scopes: ["workspace"]`. https://docs.granola.ai/help-center/sharing/integrations/granola-api
- Granola notes expose `calendar_event.scheduled_start_time` and a Google event id, not an iCalUID. https://docs.granola.ai/api-reference/get-note
- Granola speaker labels: "Me"/"Them" on desktop, Speaker A/B on iOS. The iOS app saves notes to the oldest workspace. Resume appends to an existing note. https://docs.granola.ai/help-center/taking-notes/transcription, https://docs.granola.ai/help-center/workspaces
- Circleback webhooks carry the full transcript (when selected as an outcome) with speaker names, attendees and `icalUid`, signed with an HMAC and no timestamp. Retries undocumented. https://circleback.ai/docs/support/automations/export-meeting-data-with-webhooks
- Circleback REST API since 2026-08-20. Free plan API access covers 30 days. `createdAt` is not a start time; `GET /calendar/events` has `startTime` and `meetingId`. https://circleback.ai/docs/api
- Cloud Tasks: HTTP dispatch deadline is a per-task field, default 10 minutes, max 30; delivery is at least once; no dead-letter queue. https://docs.cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks
- Slack: acknowledge interactions within 3 seconds; `trigger_id` expires in 3 seconds and works once. https://docs.slack.dev/interactivity/handling-user-interaction
- GitHub: pushes made with an app installation token do trigger push webhooks; failed webhook deliveries are never redelivered automatically. https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries
