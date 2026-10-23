# Finding labels

Hand labels for the four fixtures in `tests/fixtures/`, plus the judge test set. The bake-off scores extraction models against them, and the `--fake` models return them, so the pipeline runs end to end with no keys.

These are insurance calls (the market in `examples/insurance/`). To score models on calls from your own market, label a few of your own in a folder of their own: see [Labels for your own market](#labels-for-your-own-market).

| File | What it holds |
|---|---|
| `darius-kemp.yml`, `lena-okafor.yml`, `viktor-lang.yml`, `wade-pruitt.yml` | One per fixture: who, summary, the material findings (each one point with 1 to 3 quotes), follow-ups, ask next, mentions, and the expected belief checks against `tests/golden/beliefs.md` |
| `verdict_cases.yml` | The three known conflicts, both sides of each, as judge cases |

The four fixtures are synthetic P&C insurance discovery calls (fictional people and firms, written for these tests) chosen to cover different seats and all three speaker label kinds:

| Fixture | Seat | Recorder format | Speaker labels |
|---|---|---|---|
| `darius-kemp` | claims adjuster (staff), commercial general liability | Circleback copy | named |
| `lena-okafor` | claims manager / supervisor, trucking | Granola export | me_them |
| `wade-pruitt` | risk manager (insured), building materials distributor fleet and yards | Granola export | me_them |
| `viktor-lang` | independent adjuster / TPA, commercial property | Granola export | single_stream |

`tests/unit/test_labels.py` checks every rule below that code can check.

## Labeling rules

- **Quotes are verbatim.** Copy them character for character from the transcript body, ASR garble included. Circleback copies: the text after `**Name**:` on a `[m:ss]` turn line. Granola text export: the text after the `Transcript:` line, inside one `Me:` or `Them:` turn, label left out. Never quote the `#### Overview` notes or anything above `Transcript:`.
- **Material findings only.** A finding is material when it could change what the cofounders believe or build: how the work is done, who decides and buys, cost and time, pain, what was tried and why it failed, willingness to pay, market structure, or a disagreement with Acme's assumptions. Personal career history and company trivia (funding, IPO plans, headcount, growth) are not findings unless they change how the company buys or works; the person's role goes in `who`. Skip small talk and restatements. There is no target count.
- **One point, one finding.** Statements that make the same point (a cause and its consequence, a claim and the example behind it, the same point said twice) are one finding with up to 3 quotes, which may come from different parts of the call. Statements that only share a topic stay separate findings.
- **Quotes never overlap.** No two quotes in a file overlap. Findings are listed in the transcript order of their first quote, and each finding's quotes in transcript order.
- **Short answers** (8 words or fewer) carry `question_turn` on that quote: the 1-based index of the turn holding the question, counting turns the way `nua/parse.py` does. It is an earlier turn of the other side, except on `viktor-lang`, a single turn, where it is that turn (1) and the question comes before the quote in it.
- **Speakers.** Each quote carries its own `speaker`. On named transcripts, it is the label on the quote's turn (Circleback's `Participant 2` included). On `me_them` transcripts (`lena-okafor`, `wade-pruitt`) every quote comes from a `Them` turn, and on `viktor-lang` (one "Me" stream) from context; in both the speaker is set to the contact, with `attribution: inferred`. Alex's own questions are never labeled as findings.
- **Scope** uses only `line` (line of business or coverage), `party` (carrier, MGA, broker, insured, TPA, reinsurer, vendor), `segment` (small commercial, middle market, E&S, personal lines, specialty) and `seat`, with seats from `examples/insurance/seats.yml` (the market of the fixtures; your own labels use your `config/seats.yml`).
- **No em dashes** anywhere. A span that would need one is cut short or left out.

## Label file schema

```yaml
fixture: darius-kemp
transcript: tests/fixtures/darius-kemp.md   # optional; see "Labels for your own market"
speaker_labels: named            # named | me_them | diarized | single_stream
who: {contact, firm, seat, institution_type, role_in_thesis}
summary: [up to 5 bullets]
findings:
  - text: the point, in our words
    quotes:                      # 1 to 3
      - quote: "exact transcript text"
        speaker: Darius Kemp
        question_turn: 109       # short answers only
        attribution: inferred    # me_them and single_stream only
    scope: {line, party, segment, seat}
follow_ups: [{direction: we_owe | they_offered, what, who, quote}]
ask_next: [up to 3 questions]
mentions: {people: [...], firms: [...]}
belief_checks:                   # expected verdicts against tests/golden/beliefs.md
  - {target: B-004, verdict: contradicts, quotes: [quotes of labeled findings], note: optional}
question_coverage:               # darius-kemp only: tests/golden/prep_darius-kemp.md, in order
  - {question, status: answered | dodged | never_asked, quotes: [quotes of labeled findings]}
```

`belief_checks` and `question_coverage` name findings by the exact text of any of their quotes, because finding ids (`c01`, ...) are assigned only after consolidation.

## Matching rule

A model finding matches a labeled finding when any of its quotes overlaps any of the labeled finding's quotes by at least 50%. Locate the quotes in the raw transcript with `nua.verify.QuoteIndex`, then compute `nua.merge.span_overlap(a, b)`: the intersection divided by the shorter span. A match needs `span_overlap >= 0.5` for some pair. Each labeled finding and each model finding is matched at most once. A finding none of whose quotes can be located matches nothing.

**Recall** for a fixture is matched labels divided by labels. It is scored on merged findings (after verify and the quote-overlap merge; the bake-off makes no consolidation call) and reported per fixture and overall.

## Recall bar

Proposed: **0.8** overall on merged findings, with no fixture below 0.7. It is fixed here before the bake-off runs, per the SPEC. Changing it later needs a note in `docs/research/` saying why.

## Verdict cases

`verdict_cases.yml` is a list of `{conflict, fixture, target, expected, accept, quote, question_turn}`:
- `expected` is the verdict a judge should return for the finding holding `quote` against `target` in `tests/golden/beliefs.md`.
- `accept` lists other verdicts that also count. The contradicting side of each conflict is a likely scope split, so it accepts `scope_mismatch`.
- A judge's verdict is correct when it equals `expected` or is in `accept`. Verdict accuracy is correct cases divided by cases.

The P1 pass rule is that all three conflicts come out `contradicts` or `scope_mismatch` on their contradicting side. The write-up behind them is `tests/expected/conflicts_first-four.md`.

## Adding or changing a label

1. Copy the quote from the fixture body and grep for it (`grep -F "<quote>" tests/fixtures/<name>.md`).
2. Keep the findings in transcript order of their first quote, 8 to 25 per file, 1 to 3 quotes each.
3. If you change a quote that a belief check, question, verdict case or `tests/expected/brief_darius-kemp.md` uses, change it there too.
4. Run `uv run pytest tests/unit/test_labels.py -q`.
5. Any label change that moves recall invalidates past bake-off numbers; re-run the bake-off.

## How the findings are grouped

Each label file groups statements into findings under the rules above. The multi-quote findings are:

- `darius-kemp`: 18 findings. Joined: the first three days and the investigator sent for footage; attorney cost and attorney duration; the deadline lookup, bad faith exposure and "faster to just search the web"; video as best evidence and store-pulled video as most trusted; "Nowhere close to that." and the seven in ten figure; the three hour export and the time-marker tool.
- `lena-okafor`: 17 findings. Joined: no direct footage access and waiting on the insured; footage rarely lost and no insured saying it was gone; footage helps and speeds the liability decision; statements, photos and police reports and word against word; coverage questions and waiting on documents. Not labeled: "We're still pretty new in the commercial space" (company trivia that does not change how it buys or works).
- `wade-pruitt`: 12 findings. Joined: claim volume, auto dollars and passenger car severity; footage hardly used on yard claims and not worth pulling on every file; Lytx cameras and automatic crash clips; hazard footage and warehouse cameras for safety; yard walks, monthly visits and untracked near misses; suits against fleets and lawyer-driven claim growth; claims value to carriers and where the business sits; carrier buyers and a claims manager's buying power.
- `viktor-lang`: 16 findings. Joined: video almost never matters, a waste of time and of limited use; the leak clip and dating a leak from staining; video, expertise and on-site inspection; the fifteen day first report and its five billed hours. Not labeled: "I only handle commercial." (his role, which belongs in `who`). The single-stream short answer is "Ten days is pretty normal.", in the finding on slow examiner assignment.

## Labels for your own market

The shipped labels measure models on insurance calls. To choose models for your market, label 3 to 5 of your own calls and score on those.

1. Draft a label from one extraction:

   ```sh
   uv run python -m nua label transcripts/2026-10-20_dana-ortiz.md
   ```

   It runs the primary extractor (`--model spec` for another) once, verifies and merges its quotes as the bake-off does, and writes `labels/<transcript name>.yml` in the format above, with a `transcript:` line pointing at the call. It never overwrites a label file without `--force`. `--labels DIR` writes into another folder.
2. Correct the draft by hand under the rules above: add the findings the model missed (the draft only holds what one model found, so a label left as drafted scores that model at 1.0), delete what is not material, join statements that make one point, fix scopes and seats, check every quote against the transcript, and delete the DRAFT header.
3. Optionally add `verdict_cases.yml` (same format as here) and a `beliefs.md` the cases name; without a `beliefs.md` in the folder, cases are checked against the repo's `beliefs.md`. With no cases, judging is skipped.
4. Score:

   ```sh
   uv run python -m nua bakeoff --labels labels --models gemini:...,anthropic:...,openai:...
   ```

Each label file finds its call by its `transcript` field (relative to the repo root, else to the labels folder), else `<fixture>.md` in the labels folder, `tests/fixtures/` or `transcripts/`. Keep your labels out of `tests/labels/`: the tests check that folder against the shipped fixtures.
