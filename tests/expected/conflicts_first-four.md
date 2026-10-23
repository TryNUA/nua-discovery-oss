# Verdict cases from the four insurance fixtures

Output from four synthetic transcripts (fictional people and firms): a staff claims adjuster (Darius Kemp), a trucking claims supervisor (Lena Okafor), an independent adjuster on commercial property (Viktor Lang) and a risk manager at a distributor (Wade Pruitt). Each is a disagreement between the callers, with both quotes verbatim. Nothing here is resolved: resolution is yours.

These three conflicts are judge cases, encoded in `tests/labels/verdict_cases.yml` against the test beliefs in `tests/golden/beliefs.md`: C-001 is B-001, C-002 is B-002, C-003 is B-003. The supporting side of each is expected `supports`. The contradicting side is expected `contradicts`, with `scope_mismatch` also accepted, since every diagnosis below points at scope.

---

## C-001: Is video the most valuable evidence on a claim? (B-001)

**Severity:** escalate. The footage thesis rests on it, and D-002 rests on B-001.

**Darius Kemp** (staff adjuster, commercial general liability): video is the best evidence there is.

> "Honestly video is the single best thing you can have on a liability file."

**Lena Okafor** (claims supervisor, trucking): dashcam footage helps and her team always asks for it.

> "Having the dashcam clip makes a huge difference, so we ask for it on basically every liability claim."

**Viktor Lang** (independent adjuster, commercial property): video almost never matters.

> "Honestly, in my line of work, video almost never makes a difference."

> "For me it's mostly a waste of time, to be honest."

**Wade Pruitt** (risk manager at a distributor, the insured): on warehouse and workers' comp claims the employer rarely uses it.

> "On the yard and warehouse stuff, the workers' comp, the slip and fall stuff, hardly ever."

**Diagnosis:** scope by line and seat. Liability claims (slip and fall, auto) turn on who did what, which footage shows. Property claims turn on damage an inspector sees on site, so footage adds little. A self-insured employer on its own workers' comp claims has reasons not to investigate. Suggested resolution: scope B-001 to liability lines and confirm with a carrier property examiner.

---

## C-002: Is footage often gone before the claim is reported? (B-002)

**Severity:** escalate. It decides whether preservation is the product or a feature.

**Darius:** small business claims arrive months late, after the recording is overwritten.

> "I wish they'd hang onto it longer because a lot of these claims don't get reported to us until two or three months later."

> "So I get an investigator out to the location inside those three days too, because a lot of these DVR systems loop over in a week or two."

**Lena:** she has not seen footage lost in trucking.

> "and I can't remember a single insured telling me, sorry, that video is gone."

> "because most fleets hang on to their own dashcam video for their safety program anyway."

**Diagnosis:** likely scope. Darius describes small business CCTV with short retention and late reporting. Lena describes fleets that hold on to dashcam video for their safety programs. She also says she never tracked it, so her side is hedged.

---

## C-003: Do adjusters have to scrub long footage to find the incident? (B-003)

**Severity:** queue. It shapes what a first tool does, not whether footage matters.

**Darius:** stores send hours of video and only seconds show the fall.

> "a lot of these stores will just send over the whole afternoon, like three hours of footage, and the fall is maybe twenty seconds of it."

> "I'd love to drop the file in and have it tell me the fall happens at one hour twelve minutes and change"

**Viktor:** his clips are a minute at most; the pain is the file transfer, not the watching.

> "The clips I actually receive are usually forty five seconds, maybe a minute."

**Diagnosis:** scope by line and source. Retail CCTV on a liability claim comes as long exports; property and theft clips arrive already cut. Worth one question on the next general liability claims call.

---

## Non-conflict worth noting

Darius and Wade agree that an attorney changes a claim's cost, from opposite sides of it.

- Darius: **"The minute a lawyer is retained, the claim costs more, period."**
- Wade: **"they hire a lawyer and an $8,000 soft tissue claim turns into a $250,000 claim."**

Two independent sources, two seats, same conclusion. The pipeline would log this as a single finding with two pieces of evidence rather than as agreement between two separate findings.
