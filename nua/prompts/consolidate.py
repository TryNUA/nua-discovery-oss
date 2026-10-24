"""Consolidation prompt.

Used by nua/merge.py `consolidate`, after both extraction models have returned and their quotes are
verified. One call (the arbiter's model) gets every model's findings and groups the ones that make
the same point. SYSTEM is the system message and CONSOLIDATE the request. The code, not the model,
builds each consolidated finding's quotes from the group's verified quotes. Words in single braces
are placeholders filled by the code with str.format_map; write a literal brace as two braces. Bump
`prompt_version` in config/pipeline.yml after any change.

Layout follows Anthropic's prompting guide: a short role in SYSTEM; the findings (the long input)
first, then the call context, the instructions with their reasons and worked examples, each in its
own XML tag; the request last.
"""

SYSTEM = """\
You are an editor for {company}, {about}. Two independent readers each pulled findings from the same customer discovery call. You group the findings that make the same point, so the team sees each point once, with every reader's evidence behind it. You reply only with JSON that matches the schema you are given, and you write without em dashes because the house style bans them.
"""


CONSOLIDATE = """\
<findings>
{findings}
</findings>

<context>
Above are {count} findings from {readers} reader(s) of one call. Each has an id, the reader that found it, its text, its scope (line, party, segment, seat), its flags, and its verbatim quotes with their turn numbers. The quotes have already been checked against the transcript.

The call:
- Title: {title}
- Date: {date}
- {company} people (the interviewers): {internal_people}
- Attendees: {attendees}
- Speaker labels: {speaker_labels}
</context>

<instructions>
Return `groups`: every finding id placed in exactly one group.
1. Put findings in one group when they make the same point, even when they quote different sentences or different parts of the call. The readers often quote different sentences for one point, and the code can only see quotes that overlap, so you are the only one who can join them. A finding repeated by one reader, or split by one reader into a cause and its consequence, belongs in one group too.
2. Keep findings apart when they make different points, even on the same topic. A group the team cannot state in one or two sentences is two groups. When unsure, keep them apart: a missed join costs a repeat line in the brief, while a wrong join hides a point.
3. A finding that matches no other is a group of its own. Every id appears once, because the code drops nothing and fills in any id you leave out as its own group.
4. `text`: the group's point in one or two plain, specific sentences that make sense without the quotes, covering what every member says. Keep the members' specifics (numbers, names, who does what) rather than generalizing them away. For a group of one, repeat its text unless it can be said more plainly.
5. `scope`: the context the group's point holds in, taken from the members. Prefer the most specific value the quotes support, and leave a field empty when the members do not establish it. `seat` is copied from the members as written.
6. `flags`: `hedged`, `firsthand` and `prompted` as the members carry them; `prompted` only when every member was agreeing with a leading question.

Write only `ids`, `text`, `scope` and `flags` for each group. The quotes are not yours to write: the code attaches each group's verified quotes itself.
</instructions>

<examples>
These come from a fictional call in another market, with an operations lead at a freight brokerage.

<example>
<input>
a3: Large shippers audit every load file line by line. quote (T120)
a4: Load notes must be thorough because they can end up in court. quote (T128)
b5: Clean records on every load are critical for her team. quote (T124)
</input>
<group>ids: a3, a4, b5
text: Large shippers audit the brokerage's load files line by line, so every load needs complete records; load notes also end up as evidence when a cargo claim goes to court.</group>
<why>One point (the load file is what shippers and courts examine), quoted in three places by two readers.</why>
</example>

<example>
<input>
a1: Carrier payment disputes are the most painful part of the job. quote (T88)
b1: One badly handled dispute can damage the brokerage's standing with every carrier. quotes (T96, T108)
</input>
<group>ids: a1, b1
text: Carrier payment disputes are her team's most painful recurring problem, because one badly handled dispute can damage the brokerage's standing with carriers across the load boards.</group>
<why>The pain and the reason it hurts are one point; the readers quoted different turns, so no overlap check would join them.</why>
</example>

<example>
<input>
a6: New internal tools are piloted with a few team members for about four weeks. quote (T152)
b7: An outside vendor needs VP and finance sign-off and took four to five months to contract. quotes (T174, T178)
</input>
<group>ids: a6
text: New internal tools are piloted with a few team members for about four weeks before rollout.</group>
<group>ids: b7
text: Bringing on an outside vendor needs sign-off from the VP of operations and finance, and the last one took four to five months to contract.</group>
<why>Same topic (adoption), different points: an internal pilot and an outside vendor contract have different buyers and timelines.</why>
</example>
</examples>

Now group the {count} findings above and return the JSON.
"""
