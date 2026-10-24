"""Ask prompt.

Used by nua/ask.py to answer one question across every digested call. SYSTEM is the system message
for both steps, sent as written (it has no placeholders). MAP goes to the cheap `ask_map` model once
per batch of about 20 digests and picks the claims that bear on the question. REDUCE goes to the
strong `ask_reduce` model once and writes the answer from those claims. Every quote the answer cites
is checked by code against the call's claims and transcript afterwards. Words in single braces are
placeholders filled by the code with str.format_map; write a literal brace as two braces.
"""

SYSTEM = """\
You help the team at {company} answer questions across their customer discovery calls with people in {market}. They are exploring {exploring}. You work only from the call digests and quotes you are given. A quote is the evidence: a verbatim span of what someone said on a call. Summaries and claim text are someone's reading of the call, never evidence on their own. You never invent a call id, a claim id or a quote. You reply only with JSON that matches the schema you are given. You never use em dashes.
"""


MAP = """\
### Question

{question}

### Who is who

{company} people (the interviewers): {internal_people}. What they say on a call is {company}'s own view, not evidence from the market. In a call whose speaker labels are `me_them`, `Me` is the {company} person who recorded it. In `diarized` or `single_stream` calls the speaker was inferred, so treat it with care.

### Call digests

Digests in this batch: {count}. Each starts with a line `=== <call id>`, then who was on the call, a summary (context only) and the claims. Each claim (a finding) has an id in brackets, its scope and flags, the claim in plain words, and one to three verbatim quotes, each with its speaker.

{digests}

### What to return

**relevant**: one entry per claim that helps answer the question, including claims that cut against the obvious answer or show where it holds and where it does not.
- `call_id`: exactly as written after `===`.
- `claim_id`: the claim's id without brackets, such as `c04`.
- `why`: one line on how the claim bears on the question.

Keep a claim only when its quote, not just the summary or the claim text, speaks to the question. Leave out claims spoken by a {company} person unless the question is about what {company} said. Return an empty list when no claim in these digests bears on the question. Do not answer the question yet.
"""


REDUCE = """\
### Question

{question}

### Who is who

{company} people (the interviewers): {internal_people}. Their words are {company}'s own view, not evidence from the market.

### Evidence

Digests the map step read: {read} of {total} ({scope}). Claims it kept as bearing on the question: {count}. Each item gives the call id, date, contact and firm, seat, speaker, the claim, its scope and flags, why it was kept, and its verbatim quotes (one to three).

{evidence}

### What to return

**answer**: a direct answer, lead with it. Then the points that back it, each saying how many calls and which seats it rests on, with call ids in parentheses. Say where calls disagree and where a view held only in one context (line, party, segment or seat). Say when the evidence is thin, one-sided, hedged or prompted by the interviewer. If the map step read fewer digests than it had, say the answer may miss evidence from the rest. Keep it tight: short paragraphs or bullets, no preamble.

**citations**: the quotes the answer rests on, each with:
- `call_id`: exactly as given in the evidence.
- `quote`: copied exactly from one of that item's quotes, character for character. To cite part of a quote, copy one continuous piece of it without changing any word.

Cite only quotes from the evidence above. Write plainly. No em dashes.
"""
