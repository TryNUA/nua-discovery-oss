# Local quickstart

Run the whole pipeline on your laptop: no cloud, no Slack, no webhooks. You drop transcripts in a folder, the pipeline writes digests and indexes into your working tree, and you ask questions across every call from the command line. About five minutes.

When you want calls to flow in by themselves and briefs in Slack, move on to [self-hosting](self-hosting.md). Everything you build here carries over.

## 1. Install

You need Python 3.12+, git and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/<owner>/discovery-base.git
cd discovery-base
uv sync
```

`uv sync` builds `.venv` from `uv.lock`.

## 2. Try it with no keys

`--fake` replaces every model with fakes that answer from the hand labels in `tests/labels/`, so the shipped example calls run end to end without an API key:

```sh
uv run python -m discovery run tests/fixtures/darius-kemp.md --dry-run --fake
```

You get the Slack brief as plain text on stdout and the digest on stderr. Add `--beliefs tests/golden/beliefs.md --decisions tests/golden/decisions.md` to see a brief that checks the call against a set of example beliefs ("Contradicts B-004 ...", "Supports B-001 ...").

`--dry-run` works in a temporary copy of the repo and changes nothing. `--fake` only gives useful output for the fixtures in `tests/fixtures/`; any other transcript gets a placeholder reply.

## 3. Add a model key

The default models (`config/pipeline.yml`) are all Gemini, so one key runs everything.

1. Get a key at [Google AI Studio](https://aistudio.google.com/apikey). Turn on billing for it: on the paid tier Google does not use your prompts to improve its products, and the free tier's rate limits are too low for a backfill.
2. Copy the env file and add the key:

   ```sh
   cp .env.example .env
   ```

   ```sh
   GEMINI_API_KEY=your-key-here
   ```

   Write values bare: no quotes, no trailing comments. `.env` is gitignored.

Anthropic and OpenAI keys are optional. See [Choosing models](self-hosting.md#choosing-models) before you switch.

## 4. Describe your discovery

Three files tell the models what you are doing. Edit them before your first real run; the shipped values are a fictional startup ("Acme") exploring AI for P&C insurance.

| File | What to put in it |
|---|---|
| `config/domain.yml` | Your company name, the team, one line on what you are deciding, the market, the problem areas you are exploring, who you interview, what the three scope fields (`line`, `party`, `segment`) mean in your market, the `roles` a contact can play relative to what you build (keep `expert` and `competitor`), examples of institution labels, and the Slack bot's display name. Every prompt reads this file. |
| `config/people.yml` | Everyone who runs calls. The key (`alex`) is used in prep files and change requests; `email` is the address their calls are recorded under. |
| `config/seats.yml` | The roles your interviewees speak from (for example "head of finance", "IT buyer", "end user"). Evidence for each belief is counted by seat, so keep the list short and distinct. |

Also set `internal_email_domains` in `config/pipeline.yml` to your company's email domain. A call where every attendee is on that domain (or is someone in `people.yml`) is treated as internal and gets no digest.

`config/entities.yml` is an optional alias map for names that speech recognition garbles. Start it empty (`{}`) and add entries as you spot them.

## 5. Drop in a transcript

Put transcript files (`.md`) directly in `transcripts/`, not in a subfolder. Three shapes are read:

| Shape | How to get it | What the parser looks for |
|---|---|---|
| Granola export | Copy a note's notes and transcript from Granola into a file | A `Transcript:` line, then turns labeled `Me:`, `Them:` or `Speaker A:`. The footer above it (`Meeting Title:`, `Date:`, `Meeting participants:`) fills in the title, date and attendees. |
| Circleback copy | Copy a meeting page from Circleback | Turn lines like `[1:13] **Jane Doe**: text`, below the AI overview |
| Plain Me/Them | Any transcript you can paste | A `Transcript:` line, then `Me:` and `Them:` turns. "Me" is you, "Them" everyone else. |

A minimal plain file, `transcripts/2026-10-20_dana-ortiz.md`:

```text
Transcript:
Me: Thanks for making time. How do you get store footage today?
Them: Honestly we mostly ask the store to email it, and half the time it is gone by then.
Me: Gone how?
Them: Overwritten. Most systems keep thirty days at best.
```

Anything above `Transcript:` (or above the first turn line) is treated as recorder notes: context for the models, never evidence. Quotes only ever come from the turns.

The call date comes from a `YYYY-MM-DD` prefix in the filename, else the Granola footer, else it is unknown and the call gets an `undated_` id. Fix wrong or missing details later in `config/overrides.yml`.

## 6. Run one call

Dry run first: it calls the models but writes nothing to your repo.

```sh
uv run python -m discovery run transcripts/2026-10-20_dana-ortiz.md --dry-run
```

Then for real:

```sh
uv run python -m discovery run transcripts/2026-10-20_dana-ortiz.md
```

This writes `digests/<call id>.md` and regenerates the indexes (`calls.csv`, `claims.csv`, `findings.md`, `beliefs-status.md`, `open-questions.md`). It never commits, pushes or posts to Slack: anything it would have posted is printed to stderr as a preview. Review the files and commit them yourself.

A file outside `transcripts/` is copied in first, unchanged.

## 7. Backfill your past calls

Process a whole folder (or a list of files). One bad file never stops the rest:

```sh
uv run python -m discovery backfill transcripts/
```

It prints one line per transcript, a summary, and a fix-up list of calls with an unknown date or firm. Correct those in `config/overrides.yml`, then apply the corrections:

```sh
uv run python -m discovery reindex
```

```yaml
# config/overrides.yml
2026-10-20_dana-ortiz: {firm: Example Mutual, seat: claims manager / supervisor}
```

Re-running is cheap: a transcript whose digest is current is skipped. Add `--force` to redo it.

## 8. Ask across every call

```sh
uv run python -m discovery ask "what do claims managers say about footage retention?"
uv run python -m discovery ask "what would they pay for?" --since 2026-09-01 --seat "claims manager / supervisor"
```

It reads every digest (a cheap model maps over them in batches, a strong model writes the answer), checks every quote it cites against the transcripts, and ends with a coverage line such as `read 42/42 digests`. Citations it could not verify are marked.

## 9. Propose your first beliefs

Once you have a backfill, the bootstrap roll-up groups every finding into themes and proposes the first Beliefs and Watching items:

```sh
uv run python -m discovery rollup --bootstrap
```

Locally it writes `config/themes.yml` and `config/proposals.yml` and prints the proposals. Approving them needs the deployed Slack app (the Approve, Edit and Reject buttons), which is what keeps `beliefs.md` written by the pipeline alone. To see how briefs look against beliefs before you deploy, pass a hand-written beliefs file to a dry run:

```sh
uv run python -m discovery run transcripts/2026-10-20_dana-ortiz.md --dry-run --beliefs my-beliefs.md
```

(`tests/golden/beliefs.md` shows the format.)

## Command reference

Every command takes `--root <repo>` (default: `$DISCOVERY_ROOT`, else the nearest folder with `config/pipeline.yml`), `--fake` and `-v` (pipeline log and tracebacks on stderr).

| Command | Does |
|---|---|
| `run <transcript> [--dry-run] [--force] [--beliefs F] [--decisions F] [--prep F] [--[no-]digest]` | Process one transcript and print its brief |
| `backfill <paths...> [--force]` | Process files or folders of `.md` files, then print a summary and fix-up list |
| `ask "<question>" [--since YYYY-MM-DD] [--seat SEAT]` | Answer from every digest, with verified quotes and coverage |
| `reindex` | Apply `config/overrides.yml` to digests and regenerate the indexes |
| `rollup [--bootstrap] [--week 2026-W43]` | The weekly roll-up, or the first-beliefs bootstrap |
| `backcheck` | Check past digests against new or changed beliefs |
| `sweep [--granola-only]` | The nightly sweep (needs `GRANOLA_API_KEY` to list recorder notes) |
| `bakeoff [--models spec,spec] [--cap-usd N]` | Score models on the labeled fixtures |

Use `uv run python -m discovery <command> --help` for every flag.

## Next

- [Self-hosting](self-hosting.md): webhooks from your recorder, briefs and buttons in Slack, the `@Discovery Base` bot, and everything committed to your private repo.
- `AGENTS.md`: point Claude Code or Codex at your clone and it knows how to answer across every call, prep you for a call, and file belief changes.
