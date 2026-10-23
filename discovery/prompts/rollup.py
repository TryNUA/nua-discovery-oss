"""Roll-up prompt.

Used by discovery/rollup.py, once per roll-up, with the `rollup` model in config/pipeline.yml. SYSTEM is
the system message. The request is WEEKLY (Monday roll-up) or BOOTSTRAP (first roll-up after the
backfill), followed by OUTPUT. Words in single braces are placeholders filled by the code with
str.format_map; write a literal brace as two braces. The code checks every proposal against the
thresholds and the rejections itself, so this prompt only has to aim well.
"""

SYSTEM = """\
You maintain the knowledge base of customer discovery calls for {company}, {about}, exploring {exploring}. Every call is broken into findings (called claims below), each one point backed by one to three verbatim quotes. Claims that bear on a tracked statement (a Belief or a Watching item) are checked against it on every call. Everything else is a finding. Your job is to group findings into themes and to propose changes to the tracked statements for the {team} to approve or reject. Only one of the {team} turns a finding into a belief, so you propose and never decide. A weak proposal costs the {team} time, so you propose only what the evidence carries. You cite claims only by the refs you are given, never by retyping quotes. You reply only with JSON that matches the schema you are given. You never use em dashes.
"""


WEEKLY = """\
### Roll-up {week}, run on {today}

{calls_total} digested calls in total, {calls_new} new since the last roll-up. {findings_total} findings in total.

### Tracked statements

Beliefs are what {company} is betting on. Watching items are "interesting, not sure yet". Evidence counts only verdicts both models agreed on or the arbiter decided, against the statement's current wording.

{entries}

### Decisions

Each decision rests on beliefs. Be careful with proposals that touch them.

{decisions}

### Calls per seat

{seat_counts}

Seats to use in a scope: {seats}

### Findings

Every finding, grouped by the current themes, then the ones in no theme. `NEW` marks findings from calls since the last roll-up. Each line is `[ref] seat; date; speaker; flags. claim text. Quote: "verbatim quote"` (`Quotes: "..."; "..."` when a finding has several).

{findings}

### Claims from new calls that bear on tracked statements

{new_claims}

### Rejected proposals

One of the {team} rejected these. Do not propose the same change again unless at least 2 claims outside the rejected evidence back it. The same change means the same type on the same target (the same set of targets for a merge), or for a new entry the same statement or any of the same evidence. A rejection says nothing about unrelated proposals of the same type.

{rejected}

### Open proposals from the last roll-up

Nobody answered these. Propose one again, with the same type and target, if it still holds. Leave it out if it does not.

{open_proposals}
"""


BOOTSTRAP = """\
### Bootstrap roll-up, run on {today}

{company} has no tracked statements yet. This is the first pass over the backfill: {calls_total} digested calls and {findings_total} findings. Propose the first Beliefs and Watching items, up to {cap} in all, strongest first.

### Calls per seat

{seat_counts}

Seats to use in a scope: {seats}

### Findings

Every finding, grouped by any themes already recorded, then the ones in no theme. Each line is `[ref] seat; date; speaker; flags. claim text. Quote: "verbatim quote"` (`Quotes: "..."; "..."` when a finding has several).

{findings}

### Rejected proposals

{rejected}

### What to propose

- `add`: a Belief, for a pattern that recurs in {add_calls} or more calls from {add_seats} or more seats.
- `watch`: a Watching item, for a pattern worth checking on every call that has less evidence than a Belief needs.
- No other types in the bootstrap.
"""


OUTPUT = """\
### Themes

Return **themes**: groups of findings that make the same point about the market.
- `name`: a short plain phrase naming the point, for example "Covenant tracking across many facilities". Keep an existing theme's name unless it no longer fits. To rename one, give the new name and put the old one in `renamed_from`.
- `renamed_from`: the old name when renaming, else null.
- `claims`: refs of the findings to put in this theme, written exactly as shown, for example `2026-09-24_jane-doe#c04`. A finding already in a theme stays there unless you list it under another theme, so list only findings that are new to the theme or move into it. A theme you leave out keeps its findings.

Group by the underlying point, not by shared words. A finding can sit in one theme only. Leave a finding out of every theme when nothing else makes its point.

### Proposals

Return **proposals**: changes to the tracked statements.
- `type`: `add`, `watch`, `promote`, `split`, `retire`, `reword`, `merge` or `drop`, as allowed above.
- `statement`: for add, watch, promote, reword and merge, one sentence that could turn out false, in present tense and plain words, 20 words or fewer. Say what is true and for whom. Else null.
- `scope`: where the statement is meant to hold. `line` ({scope_line}), `party` ({scope_party}), `segment` ({scope_segment}) and `seat` (from the seat list). Null for anything the evidence does not pin down. Put the scope here, not in the statement.
- `statements`: for split only, two or more `{{statement, scope}}` items, one per context the evidence separates. Else an empty list.
- `sources`: refs of the claims that back the proposal, from any section above. For add and watch, cite every claim that makes the point.
- `target`: the B- or W- id for promote, split, retire, reword and drop. Else null.
- `targets`: for merge only, the ids to merge. Else an empty list.
- `theme`: for add and watch, the theme the proposal comes from, if any. Else null.
- `rationale`: one line on why, citing counts or the sharper reading.

When each type is warranted:
- `add`: a finding theme recurs in {add_calls} or more calls from {add_seats} or more seats.
- `promote`: a Watching item has support from {add_calls} or more calls in {add_seats} or more seats.
- `split`: a belief has {split_scope_mismatch} or more scope mismatch verdicts, or its contradictions all come from one context. Give the statements for each context.
- `retire`: contradicted by {retire_calls} or more calls in {retire_seats} or more seats.
- `reword`: the evidence supports a sharper statement than the current one. Cite the claims that show it.
- `merge`: two entries whose supporting claims mostly overlap.
- `drop`: a Watching item with no hit in {drop_watching_weeks} weeks.

Return an empty list when nothing is warranted. That is a normal outcome.

Write plainly. No em dashes.
"""
