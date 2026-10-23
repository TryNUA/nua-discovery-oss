# Golden brief: Darius Kemp

The v2 brief for `tests/fixtures/darius-kemp.md`, as plain text: the top message, then one section per thread reply. Built from the findings, verdicts and question coverage in `tests/labels/darius-kemp.yml`, against `tests/golden/beliefs.md`, `tests/golden/decisions.md` and `tests/golden/prep_darius-kemp.md`.

How it is compared (BUILD_PLAN P1): what-changed items match on (target, verdict); new findings match on quote-span overlap of their first quotes; Alex judges the rest.

Assumptions a reader needs:
- Darius's is the only digest in the repo, as in a dry run on a fresh copy, so every supported belief is at 1 call and 1 seat, and the seat is new to it. That is why the supports are not collapsed to one line.
- The header shows the recorder and source (`Alex · Circleback`) as they would appear on the live copy, and the count line ends with the digest link. A dry run on the bare fixture has no front-matter and prints neither.
- Both extraction models are taken to have found every labeled finding, the consolidation step to have joined each pair, and both verdict models to agree.
- A finding shows its first quote, then its second when that is short, else a count of the quotes left in the digest.
- Follow-ups and the suggested next questions stay in the digest; the brief does not post them.
- Circleback labels part of Darius's audio `Participant 2`; the short answer "Nowhere close to that." keeps that label.

## Top message

```text
Darius Kemp · claims adjuster (staff) · 38m · Alex · Circleback
Touches D-002 (Footage review tool for adjusters)

WHAT CHANGED
Contradicts B-004 "Adjusters can use outside AI tools on claim files"  [2/2 agree]  [Wrong read]
  "But anything outside the company is off limits for us, because compliance is terrified of claimant data leaving the building."
  A staff adjuster at a carrier; B-004 came from an independent adjuster. Likely split.
Supports B-001 "Video footage is the most valuable evidence on a liability claim"  (now 1 call, 1 seat; new seat: claims adjuster (staff))
Supports B-002 "Small business footage is often overwritten before the claim is reported"  (now 1 call, 1 seat; new seat: claims adjuster (staff))
Supports B-003 "Adjusters lose real time scrubbing long footage for the moment of the incident"  (now 1 call, 1 seat; new seat: claims adjuster (staff))
Watching W-001, supports "Commercial lines get less claims software than personal lines"  (now 1 call, 1 seat)

12 new findings · 1 dodged question · digest on GitHub
```

## Thread 1: Summary

```text
SUMMARY
- Slip and fall is the biggest injury bucket on his small business desk, and the first three days carry the file: statements, an investigator for footage, sweep logs, the policy.
- Video is the best evidence on a liability file, but on small business claims it is missing or junk most of the time, and it is often recorded over before a late claim arrives.
- Stores send whole afternoons of footage with no marker for the fall; he wants a tool that finds the minute.
- His day-one gaps are a state and county deadline lookup, prewritten statement questions and an investigation checklist.
- His carrier bars outside AI tools, and commercial lines get fewer platform modules than personal lines.
Quotes matched first pass 50/50 · Findings found by 2 models: 18, by 1 model: 0 · 0 verdicts by arbiter
```

## Thread 2: New findings

```text
NEW FINDINGS (12)
- Slip and fall is the largest injury claim bucket on his small business accounts.
  "slip and fall is easily the biggest bucket on my desk when it comes to injury claims on these small business accounts"
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- On a severe injury he alerts the account underwriter and asks for the insured's prior loss runs.
  "If it looks like a big one, say a broken hip or a head injury, I'll ping our underwriter on the account so they can see it and I'll ask them for the prior loss runs."
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- A lawyer makes a claim cost more from the start: an unrepresented claim closes in about four months, a represented one takes eighteen months to three years.
  "Unrepresented, I'm trying to wrap it up in about four months. Once a lawyer signs on you can figure eighteen months at the low end and sometimes three years."
  "The minute a lawyer is retained, the claim costs more, period."
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- He wants a lookup that returns a state and county's deadlines and notice rules, because a missed deadline becomes a bad faith argument and the internal sources are so scattered that a web search is faster.
  "what I really want is a lookup where I type in the state and the county and it spits out every deadline and notice rule for that spot."
  "If you blow one of those deadlines, that's a bad faith argument waiting for you when the suit gets filed."
  +1 more quote
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- Statement questions written for the specific loss would spare him improvising them on the call.
  "Something like that would hand me the right questions for that exact loss so I'm not making them up while the claimant is on the phone."
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- He wants an investigation checklist that lists every document and person still needed for a loss type.
  "I'd love a checklist that builds itself, where you put in the loss type and it tells you every document and every person you still need."
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- An AI agent that drafts the note after a lawyer negotiation would give him back an hour a day.
  "If a bot could listen in and draft that note and I just signed off on it, I'd get an hour of my day back easy."
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- AI agents break down on long chains of insurance steps because one early error carries through the rest.
  "Where they fall apart is a long chain of steps, because if step two is wrong then every step after it is wrong too."
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- Guidewire has long been the leading claims platform.
  "because guide wire has been the big name in claims systems for as long as I've been working."
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- Far fewer than half of his files have usable video; on small shops the camera was not recording or the footage is junk about seven times in ten.
  "Nowhere close to that."
  "On the small shops I'd say seven times out of ten the camera wasn't recording or the footage is junk."
  Participant 2 · found by 2 models · unprompted  [Make belief | Watch]
- Large chains give usable video about half to two thirds of the time.
  "The big chains are better, maybe half to two thirds of the time you get something you can use."
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
- Repeat claimants know where the cameras point and fall in the gaps.
  "the professional fallers know where every camera in that store is pointed and they go down right in the gap"
  Darius Kemp · found by 2 models · unprompted  [Make belief | Watch]
```

## Thread 3: Questions

```text
QUESTIONS FROM PREP
Answered
- What happens in the first few days of a slip and fall claim?
    "With no lawyer on it, those first three days are everything."
- How often do your files have usable video at all?
    "Nowhere close to that."
- When you do have video, how does it change the claim? (B-001)
    "Honestly video is the single best thing you can have on a liability file."
Dodged
- Do carriers buy their claims platform or build their own in house?
    "because guide wire has been the big name in claims systems for as long as I've been working."
Never asked
- What would a carrier pay per location for a footage preservation tool?
```
