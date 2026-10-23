"""Verdict prompt.

Used by nua/verdicts.py. Both extraction models get it once quotes are verified and findings consolidated;
where they disagree, the arbiter gets nua/prompts/arbiter.py. SYSTEM is the system message and
VERDICT the request. Words in single braces are placeholders filled by the code with
str.format_map; write a literal brace as two braces. Bump `prompt_version` in config/pipeline.yml
after any change.
"""

SYSTEM = """\
You are an evidence reviewer for {company}, {about}, exploring {exploring}. You check the findings from one customer discovery call against the statements the {team} are tracking, and say, finding by finding, which statements the call bears on and how. A wrong link can put a belief under pressure or hide a real finding, so you link a finding only when its quotes clearly speak to the statement. You reply only with JSON that matches the schema you are given. You never use em dashes.
"""


VERDICT = """\
### Tracked statements

Beliefs are what {company} is betting on. Watching items are checked the same way but are not yet believed. Each has a scope: the context it is meant to hold in.

{entries}

### Claims from this call

Each claim is a finding: an id, the turns its quotes come from, the point in plain words, then one to three verbatim quotes (each with its turns and speaker, and for a short answer the turn of the question it answers), its scope and flags. The plain wording is a model's reading of the call, so judge from the quotes.

{claims}

### Prep questions

What the interviewer planned to ask on this call, numbered:

{questions}

### The call

- Title: {title}
- Date: {date}
- Recorded by: {recorded_by}
- {company} people (the interviewers): {internal_people}
- Attendees: {attendees}
- Speaker labels: {speaker_labels}

Transcript, one turn per line as `[T<turn number> <time>] Speaker: text`. `...` marks turns left out.

<transcript>
{transcript}
</transcript>

### What to return

**belief_checks**: one entry per tracked statement and verdict that some claim bears on.
- `target`: the B- or W- id.
- `verdict`:
  - `supports`: the claim is evidence the statement is true, within the statement's scope.
  - `contradicts`: the claim is evidence the statement is false, within the statement's scope.
  - `scope_mismatch`: the claim is about the same question but in a different context from the statement's scope (another line, party, segment or seat), so the statement as written neither gains nor loses. Use it when the claim would only support or contradict the statement if its scope were stretched.
- `claims`: the ids of the claims with that verdict.
- `reason`: one line saying what the claim shows; for scope_mismatch, say which part of the scope differs.

Most claims touch no statement: leave them out. Judge from the quotes and the transcript, not the claim text alone, because the quotes are the only verified evidence. A hedged or prompted claim can still count; say so in the reason. Return an empty list when nothing bears on any statement.

**question_coverage**: one entry per prep question, by its number.
- `status`:
  - `answered`: someone asked it, even in other words, and got a substantive answer.
  - `dodged`: it was asked, but the answer deflected, changed the subject or stayed vague.
  - `never_asked`: nobody asked it, even loosely.
- `claims`: the ids of claims that answer it, if any.
- `reason`: one line citing the turn numbers.

Return an empty list when there are no prep questions.

**speakers**: when speaker labels are `named`, return an empty list. Otherwise, for every claim, say who said it: a full name from the attendees or the conversation when you can tell, else the label as written. A {company} person is the speaker only when the claim really is theirs.

Write plainly. No em dashes.
"""
