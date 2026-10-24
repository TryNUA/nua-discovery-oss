"""Extraction prompt.

Used by nua/extract.py. Each constant below is one part: SYSTEM is the system message, EXTRACT is
the request both extraction models get, REPAIR asks the model that made a finding to fix one quote
the code check rejected. Words in single braces are placeholders filled by the code with
str.format_map; write a literal brace as two braces. Bump `prompt_version` in config/pipeline.yml
after any change.

Layout follows Anthropic's prompting guide: a short role in SYSTEM; the long transcript first, then
the call context, the instructions (each with its reason) and worked examples, each in its own XML
tag; the request last. The examples come from a fictional call in another market (a freight brokerage)
and show the two kinds of statement that are not findings, two points each built from several
quotes, and a short answer. The domain words ({company}, {market}, {exploring} and the rest) come
from config/domain.yml through nua.prompts.domain_values.
"""

SYSTEM = """\
You are an analyst for {company}, {about}. You read one customer discovery call and pull out the findings that could change what the team believes or builds, each backed by quotes copied exactly from the transcript. Code checks every quote against the transcript, character for character after ignoring case, whitespace and punctuation, and throws away any quote it cannot find, so an exact quote is worth more than a tidy one. You reply only with JSON that matches the schema you are given, and you write without em dashes because the house style bans them.
"""


EXTRACT = """\
<recorder_notes>
{notes}
</recorder_notes>

<transcript>
{transcript}
</transcript>

<context>
Above are the transcript of one call and a summary the recording tool wrote itself. Each transcript turn starts a new line as `[T<turn number> <time>] Speaker: text`, and the turn number is what you cite. A turn can be very long; on single_stream calls the whole recording may be one turn.

The call:
- Title: {title}
- Date: {date}
- Recorded by: {recorded_by}
- {company} people (the interviewers, never the contact): {internal_people}
- Attendees: {attendees}
- Speaker labels: {speaker_labels}

Speaker labels tell you how far to trust who said what:
- named: real names on every turn.
- me_them: "Me" is the recorder's microphone, so one of the {company} people. "Them" is everyone else.
- diarized: "Speaker A", "Speaker B" and so on. Work out who is who from the conversation.
- single_stream: a phone or in-person recording. Usually the whole call is one turn (T1), labeled "Me", holding both sides of the conversation with no marker where the speaker changes. Work out who is speaking from what is said: questions and pitches are usually {company}'s; answers about the speaker's own job and company are the contact's.

Seats. A seat is the job role someone speaks from. The knowledge base counts evidence by seat, so the same role must get the same seat on every call. These seats are already in use:
{seats}
Reuse one, spelled exactly as shown, when the person's role matches it. Otherwise write a new short generic job role: lowercase, no firm or person names, such as "claims manager" or "head of operations". Use "other" only when the role cannot be told.

Known names. Speech recognition garbles names, and the knowledge base finds a firm only under its canonical name. Each line is a canonical name and the ways it has been misheard:
{entities}
</context>

<instructions>
Who reads this. The team at {company} reads your findings to decide what to build for {market}. They are exploring {exploring}. A finding matters when it could change what they believe or build: how the work is done today, who decides and who buys, what it costs in money and time, what hurts, what was tried and why it failed, what someone would pay for, how the market is structured, or a disagreement with what {company} assumed. Each finding lands in a short Slack brief and in a knowledge base that every later question is answered from, so one sharp finding is worth more than three thin ones.

What a finding is. One point, stated once, with the quotes that support it.
1. Take findings only from what people outside {company} say in the transcript. The recorder notes are context only: they help you orient, but the code cannot verify them, so a finding, quote, follow-up or mention never comes from them, and where they disagree with the transcript, the transcript wins. {company}'s own pitches and questions are what is being tested, not evidence, so a finding never rests on them; a quote may answer one.
2. Combine related statements into one finding. People circle back to the same point, and a reader who sees it split three ways counts three findings where there is one. When several statements make one point (a cause and its consequence, a claim and the example behind it, the same point repeated later), write one finding and give it up to three quotes, preferring quotes from different parts of the call over near-repeats.
3. Leave out what does not bear on the decision. The person's career history belongs in the `who` fields, and company trivia (funding, IPO plans, headcount, culture) is not a finding unless it changes how the company buys or works. Small talk, logistics and anything {company} said about itself are left out too.
4. There is no target count. A short or thin call may give two findings and a dense one fifteen; the call decides.

How to write each finding:
5. `text`: the point in your own words, one or two plain, specific sentences that make sense without the quotes. Fix garbled names here, using Known names; never in a quote.
6. `quotes`: one to three quotes, in the order they occur. Each `quote` is copied exactly from the transcript: the same words in the same order, with filler words, repeats, false starts and speech-recognition errors left in, and without the `[T.. ..] Speaker:` prefix. Keep each quote to the one to three sentences that carry the point. A quote may run from one turn into the next only when the passage really continues across them; otherwise use a second quote, since words stitched from places that are not next to each other cannot be found.
7. `turns`: for each quote, the turn numbers it comes from.
8. Short answers: when a quote is {short_answer_words} words or fewer (for example "I don't think so."), set its `question_turn` to the number of the turn that holds the question it answers, because the answer means nothing without it; then make `text` state the full point. That is an earlier turn. When one turn can carry both sides of the call (speaker labels single_stream, or the whole transcript is the single turn T1), it may instead be the quote's own turn, as long as the question comes before the quote in that turn. For a longer quote `question_turn` is null.
9. `speaker`: for each quote, who said it. On named and diarized calls, the label on the turn exactly as written. On me_them and single_stream calls the labels do not separate people reliably, so give the person you worked out from context (usually the contact's name).
10. `scope`: the context the finding holds in, so it is never stretched beyond what was said. `line` ({scope_line}), `party` ({scope_party}), `segment` ({scope_segment}), `seat` (the speaker's seat, as in Seats above). Null for anything the call does not establish.
11. `flags`: `hedged` when the speaker was unsure or qualified it; `firsthand` when it comes from their own direct experience rather than what they heard; `prompted` when they were agreeing with a leading question rather than volunteering it. Readers weigh a prompted or secondhand finding less, so mark it.

The rest of the reply:
- `who`: the main external person on the call. `contact` is their full name as the transcript or attendee list gives it. `firm` is the company they speak for, with `firm_source` `transcript` when the call names it or `attendee_email` when you only know it from their email domain; null when unknown. `seat` follows Seats above. `institution_type` is a short plain label ({institution_examples}...). `role_in_thesis` is one of these ids, by where the contact stands relative to what {company} is building: {roles}; null when unclear.
- `summary`: up to 5 plain sentences on what the call was about and what we learned.
- `follow_ups`: commitments made on the call. `direction` is `we_owe` ({company} promised something) or `they_offered` (they offered an intro, a document, another call). `what` is a short description, `who` the person involved, `quote` the exact words from the transcript.
- `mentions`: people and firms named in the transcript, other than {company} and its people, under their canonical names.
- `ask_next`: up to 3 questions worth asking this person or their seat next time, aimed at what this call left open.
</instructions>

<examples>
These come from a different call in a different market, with an operations lead at a freight brokerage (speaker labels me_them, so "Them" is the contact). They show the judgment, not the subject matter; your call will be about something else.

<example>
<excerpt>
[T118 14:28] Them: So how it works is, a big shipper will come in and audit us, like the whole year.
[T120 14:37] Them: Full audits. We had to pull every load file, the rate confirmations, the proof of delivery, and send it to their compliance team and they went through it line by line. ...
[T124 15:22] Them: We do that every quarter now. Usually it's me pulling those packets together for my accounts. I mean, you have to keep clean records on every load. It's just a big thing.
[T128 15:43] Them: Um, and if a claim goes to court, it's all in the load notes, so you're making sure you're thorough about that. ...
</excerpt>
<finding>
text: Large shippers audit the brokerage's load files line by line, so every load needs complete records; load notes also end up as evidence when a cargo claim goes to court.
quotes:
- "Full audits. We had to pull every load file, the rate confirmations, the proof of delivery, and send it to their compliance team and they went through it line by line." (turns [120], speaker Priya)
- "I mean, you have to keep clean records on every load. It's just a big thing." (turns [124], speaker Priya)
- "Um, and if a claim goes to court, it's all in the load notes, so you're making sure you're thorough about that." (turns [128], speaker Priya)
scope: party broker, seat as in Seats; flags firsthand
</finding>
<why>Three statements, one point: the load file is what shippers and courts examine. Split three ways ("shippers audit us", "records are critical", "notes reach court") it reads as three findings and buries the point.</why>
</example>

<example>
<excerpt>
[T88 9:49] Them: ... Honestly I'm not the right person for that one, but we get a lot of disputes. So the biggest thing is disputes.
[T95 11:22] Me: Yeah. And when you say disputes, is that the carrier disputing a payment?
[T96 11:33] Them: Yeah, they're not happy with how we settled a detention charge or whatever, and they post about it on the load boards and that can tank your rating with every carrier out there. So that's a big risk. ...
[T108 13:22] Them: Fast and right. You can lose your best carriers over one bad payment. So
</excerpt>
<finding>
text: Carrier payment disputes are her team's most painful recurring problem, because one badly handled dispute can damage the brokerage's standing with carriers across the load boards.
quotes:
- "but we get a lot of disputes. So the biggest thing is disputes." (turns [88], speaker Priya)
- "and they post about it on the load boards and that can tank your rating with every carrier out there. So that's a big risk." (turns [96], speaker Priya)
- "You can lose your best carriers over one bad payment." (turns [108], speaker Priya)
scope: party broker, seat as in Seats; flags firsthand
</finding>
<why>The pain and the reason it hurts are one point. "Disputes are the biggest problem" and "disputes risk the carrier network" as two findings would make the reader join them back up.</why>
</example>

<example>
<excerpt>
[T32 2:55] Them: Well, I started in dispatch, like, as a trainee, and then I did carrier sales, and then I moved to account management, and then I switched companies and ended up running operations here. ...
[T38 3:45] Them: Oh yeah, a lot of growth. We just raised our Series B, so, you know, everything's changing, we're hiring a ton. They're very tech-forward. They're trying to bring in new AI stuff, which is good. Like it does help.
</excerpt>
<finding>none</finding>
<why>Her path from dispatch to operations tells the reader nothing about what to build; it belongs in `who` (seat, institution_type). The funding round, the hiring and the push for AI are company trivia: nothing here says how the company buys, what it pays for or what hurts. Neither is a finding.</why>
</example>

<example>
<excerpt>
[T235 29:21] Me: I guess, which do you think is easier to start with? The. Like
[T236 29:28] Them: Carrier onboarding, 100%.
...
[T248 29:55] Them: Because nobody fights you on onboarding. Once you touch payments, finance gets involved and it's six months of security reviews. ...
</excerpt>
<finding>
text: She thinks a new tool gets in more easily through carrier onboarding than through payments, because anything that touches payments brings in finance and months of security review.
quotes:
- "Carrier onboarding, 100%." (turns [236], question_turn 235, speaker Priya)
- "Because nobody fights you on onboarding. Once you touch payments, finance gets involved and it's six months of security reviews." (turns [248], speaker Priya)
scope: seat as in Seats; flags firsthand
</finding>
<why>The first quote is four words, so it carries question_turn 235, the turn that asked where to start, and `text` states the full point. The interviewers on that call assumed payments was the way in, so a contact disagreeing with it is one of the most important findings of the call.</why>
</example>

<example>
<excerpt>
[T152 18:31] Them: So what we'd probably do is, like our pilots, we pick a few people on the team to use it for real, and then they basically give feedback on what's working, what's not working, what they need, you know what I'm saying? And that runs about 4 weeks.
[T173 21:23] Me: I guess, like, who has to sign off on it? And how long would that take?
[T174 21:32] Them: It would have to go through. It would have to go through our VP of ops and finance, to be honest, to bring somebody in. ...
[T178 22:10] Them: ... but it took about 4 or 5 months to get that contract signed, with legal and procurement in the mix of it. So yeah.
</excerpt>
<finding>
text: A new internal tool is piloted with a few team members for about four weeks before rollout.
quotes:
- "and then they basically give feedback on what's working, what's not working, what they need, you know what I'm saying? And that runs about 4 weeks." (turns [152], speaker Priya)
</finding>
<finding>
text: Bringing on an outside vendor needs sign-off from the VP of operations and finance, and their last vendor contract took four to five months with legal and procurement involved.
quotes:
- "It would have to go through. It would have to go through our VP of ops and finance, to be honest, to bring somebody in." (turns [174], speaker Priya)
- "but it took about 4 or 5 months to get that contract signed, with legal and procurement in the mix of it." (turns [178], speaker Priya)
</finding>
<why>Both are about adoption, but they are different points with different approvers and timelines: a pilot of an internal tool versus an outside vendor contract. Combine statements that make one point, not statements that share a topic.</why>
</example>
</examples>

Now read the call above and return the JSON: `who`, `findings` in transcript order, `summary`, `follow_ups`, `mentions` and `ask_next`. Write plainly and briefly, in sentences without em dashes.
"""


REPAIR = """\
<transcript>
{transcript}
</transcript>

<context>
Above are the transcript turns around a quote you gave, one per line as `[T<turn number> <time>] Speaker: text` (`...` marks turns left out). Speaker labels on this call: {speaker_labels}.

The finding the quote supports: {text}

The quote as you gave it:
- quote: {quote}
- turns: {turns}
- question_turn: {question_turn}
- speaker: {speaker}

It failed the code check that every quote must appear, word for word, in the transcript. Why: {reason}
</context>

<instructions>
Return the corrected quote, because a finding is only kept with quotes the code can find.
- Copy `quote` exactly from the turns above: the same words in the same order, filler words and speech-recognition errors left in, no `[T.. ..] Speaker:` prefix.
- If the quote is {short_answer_words} words or fewer, set `question_turn` to the turn that holds the question it answers: an earlier turn, or the quote's own turn when one turn can carry both sides of the call (single_stream, or the whole transcript is the single turn T1) and the question comes before the quote in that turn.
- Keep `speaker` unless the correct passage changes it.
- If no passage in these turns supports the finding, return null for `quote`: a missing quote is better than a misquoted one, and the finding keeps its other quotes.

Write without em dashes.
</instructions>
"""
