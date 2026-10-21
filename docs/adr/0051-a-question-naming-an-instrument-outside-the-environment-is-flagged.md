# ADR-0051 — A question naming an instrument outside the environment is flagged, in every layer, by the pipeline

- Status: accepted. The human set the behaviour (quoted below). The model made
  the design calls, each named as such.
- Date: 2026-10-21
- Issue: #252
- Extends: D-17 / ADR-0020 (the environment is what a tenant holds),
  ADR-0049 (held instruments are known by their recorded titles), ADR-0047
  (a flag the reader is shown is recorded with the chat turn, as #228 did)
- Touches: ADR-0003 / D-02 (the verifier is unchanged; it cannot see this),
  D-01 (the register and the golden case are reviewed content)

## Context

A tenant subscribed only to `privacy-act-1988` asked: "under the spam act,
can i send emails to customers without consent from a personal email instead
of business one". The spans show Track B ranked `sch1/app7/8/b` first in both
channels: APP 7.8(b), "the Spam Act 2003;", the Privacy Act's own provision
that APP 7 does not apply to the extent the Spam Act applies. Then APP
7.1–7.3 and 7.6 on direct marketing.

Nothing in the pipeline knew the question named an instrument the tenant
doesn't hold:

- The system prompt said to answer "using only the source clauses provided",
  and nothing about a named instrument that isn't there.
- The verifier checks that a quote matches canonical text. An answer that
  presents APP 7 as Spam Act law verifies, because every quote is APP 7's.
- So the only guard was the model's discretion, which is not a control.

## The human's decision

> I deliberately asked about the Spam Act, which isn't in the corpus — only
> the Privacy Act and Acme's policies are loaded. That's the subscription
> boundary, and it's the case I most want to see handled well. What should
> happen: either "the Spam Act isn't in your environment" or an answer from
> APP 7 on direct marketing that is explicit it's citing the Privacy Act, not
> the Spam Act. What must never happen is a confident answer about the Spam
> Act built from APP 7 text.

## Decision

**1. Detection is deterministic, from the question alone, before anything
is retrieved.** No model call. The question is the member's input, so
matching on it is allowed; retrieved text never reaches it (#29). APP
7.8(b)'s own words name the Spam Act, and they are never read
(`environment.not_in_environment`, `test_retrieved_text_never_reaches_detection`).

Two matchers (the model's design):

- **A reviewed register**, `corpus/register/instruments.yaml`: instrument
  names and aliases, matched case-insensitively as whole words, longest
  first. An entry is one of three kinds:
  - an instrument the library holds (`library_id`);
  - one it doesn't (the Spam Act 2003, the Do Not Call Register Act 2006, the
    Telecommunications (Interception and Access) Act 1979, the GDPR);
  - a **part** of a library instrument (`part_of`, with the clause-path
    prefixes it covers).
- **A conservative pattern**, "<Name> Act [year]", for names the register
  doesn't hold. Case-insensitive, but it reads the words around "act":
  - a title-case name before a capitalised "Act" ("Spam Act", "Fair Work
    Act") is a statute, unless a verb's word follows and nothing before says
    statute ("Can Acme Act on it" names nothing; "under the Spam Act on 1
    July" does), and so is any name with a year after "Act";
  - a lowercase one is a statute only where its context says so: after a
    word like "under" or "against", after "the" with such a word before it
    ("under the spam act"), or after "the" with a statute's verb or pinpoint
    following ("does the spam act apply", "the spam act s 16");
  - never the verb ("how should the officer act on it", "the board act on
    advice", "we must act quickly"), never "the Act" alone, and never the
    all-capitals "ACT", the territory.
  - It stops at a function word, so "the do not call register act" gives the
    pattern nothing (the register names it), and "the Do Not Call Register
    Act 2006" gives "Call Register Act 2006": flagged, under less than its
    full name.

**Why both.** The pattern alone catches the human's case, lowercase, and any
"<Name> Act" the library has never heard of, so the mechanism works with no
register at all. It can't catch a name that isn't "<Name> Act" (GDPR, the
NDB scheme), can't tell a part from its whole, and can't give a reader the
instrument's proper name. The register adds those, once a person has signed
it.

**2. Held is decided against the ledger's record of the environment.**
`LedgerStore.environment_titles(tenant_id, as_of)` is one read: each
instrument the tenant's retrieval can see on `as_of` (its own documents of
every class, in force, never withdrawn or purged; the library instruments it
subscribes to; for `_public`, the whole library), to the recorded titles of
its versions in force (ADR-0049).

- A pattern match is held when its words appear, in order, in a held title:
  "privacy act" is in "Privacy Act 1988"; "privacy act 2020" is not, so a
  year that differs is a different instrument, flagged. The model's call:
  exact words, never a suffix, because "Data Privacy Act" would otherwise be
  held by "Privacy Act 1988", the dangerous direction.
- A register entry is held by its library id, or by any of its names in a
  held title (a tenant that supplied the Spam Act as an external upload holds
  it).
- **A part** is held only where its instrument is held *and* every committed
  selection manifest of that instrument selects into one of its prefixes.
  Every, not any (from the review): which version is in force on a date isn't
  known when the register loads, so a version that leaves the part out flags
  it on every date, the safe direction.
  The Notifiable Data Breaches scheme is Part IIIC of the Privacy Act
  (ss 26WA–26WT, paths `s26w…`). C2026C00227's selection is Schedule 1 and ss
  6, 6C–6EA and 15, so a tenant holding the Act does not hold the scheme. The
  flag says so: `known_as` names the scheme and `part_of` the Act. The model's
  call: the selection, not the ledger, because the ledger records versions,
  not which of their provisions were loaded, and the selection manifest is
  the reviewed record of exactly that.

**3. The model is told, in a fence.** A `not_in_environment` section, after
the retrieved clauses and before the question, holds one marker per
instrument: `[B instrument named "<the question's words>" known as "<name>"
part of "<instrument>"]`. Each value is a JSON string, and the boundary is
hashed from the markers, so no value can forge the closing line. The system
prompt says that for each one the answer must:

- say plainly, first, that it is not in this environment;
- never state, summarise, guess or imply what it requires, permits or
  prohibits;
- if it answers from the clauses provided, say so and name the instrument
  each relied-on clause is from, never presenting it as the named
  instrument's law. A clause that mentions the named instrument (7.8(b)) may
  be cited for what it says, and nothing more.

A prompt with nothing absent keeps its bytes, so no cache is missed.

**4. The flag is the pipeline's, and every reader gets it.**
`Answer.not_in_environment` (`named`, `known_as`, `part_of`) is set from the
detection, never from the model. The API's answer event carries it, with a
verified answer and a withheld one alike. A chat turn records it, and the
read-back returns it (ledger 0018: `chat.not_in_environment`, JSONB, empty on
a question and on a failed request, which is a marker only). A corrupt
stored element reads back as `{"unreadable": true}` in its place, as #232
reads a citation. The `answer` span records `answer.environment` (the
instrument ids held) and `answer.not_in_environment`.

**5. The guard: the flag is unconditional, never withheld, never a judgement
of the model's prose.** The model's call, within the human's two acceptable
outcomes:

- **Flag, don't withhold.** Withholding every answer to such a question
  would also withhold the second outcome the human accepted, the APP 7 answer
  that says it is citing the Privacy Act. Both outcomes show text, so the
  control is what goes beside the text.
- **Unconditional, not "when the answer claims a requirement".** Deciding
  that the model's prose claims what the Spam Act requires is a judgement
  about free text: a regex is evaded by paraphrase, and a model judging it is
  the discretion that isn't a control. So the pipeline doesn't decide it: an
  answer to a question that names an out-of-environment instrument carries
  the flag, whatever it says. A confident Spam Act answer from APP 7 text can
  still be generated; it cannot reach a reader without the structured flag
  saying the Spam Act isn't in the environment, in the event and in the chat.
  `test_a_confident_spam_act_answer_from_app7_text_verifies_and_still_carries_the_flag`
  and `test_a_verified_confident_spam_act_answer_reaches_the_reader_flagged`
  prove it with a fake generator that writes exactly that answer.
- **How often the model gets it right is measured, not enforced:** the
  answer eval (decision 7).

**6. The register and the golden case are reviewed content (D-01), drafted by
the model, pending the human's signature.** `load_instrument_register`
refuses a pending file (`UnsignedInstrumentRegister`), and the pipeline reads
the committed one when it is built, so an unsigned register stops the build
as an unsigned `incomplete.yaml` does. With none committed the register is
empty and the pattern works alone. The register is therefore **not in the
mechanism's PR**: it is drafted in the golden-set PR, beside the case it is
needed for, red until signed.

**7. The golden case lives in the answer eval's expectations, not in
`queries.yaml`.** `corpus/golden/not_in_environment.yaml`, with its own front
matter, refused while pending (`test_the_committed_cases_are_signed` is CI's
gate). The model's call:

- **Not a recall label in `queries.yaml`.** The recall gate embeds every
  query from the checked-in vector snapshot, and a new question has no
  vector until someone refreshes it (a live Vertex call), so adding it there
  would turn the gate red for a reason the signature can't fix. It would also
  measure ranking, and this case is about what the answer says.
- **The answer eval checks it**, `run_answer_eval.py --environment`, over the
  whole compilation (APP 7 isn't in the golden excerpt), with the case's
  tenant subscribed as it says and the question embedded live where the
  snapshot lacks it. Per case: the flag names the instrument by the
  register's name; APP 7.8(b) is in the delivered list; and the answer either
  says the instrument isn't in the environment, or names the held
  instrument with every citation of held law and never speaks as the named
  instrument's law ("under the Spam Act you may…", "the Spam Act requires…"
  outside a negation), since naming the Privacy Act in passing doesn't make a
  Spam Act answer acceptable. The last two are a plain-text
  proxy on the model's prose, recorded for a reviewer to read. A case failing
  any of them fails the run. It can't run today: Claude's quota is zero.

## Consequences

- **The system prompt changed**, so the first answer after deploy misses the
  prompt cache once. A user prompt with nothing absent is byte-identical.
- **One more ledger read per answer** (`environment_titles`), before
  retrieval.
- **Measured false positives: none.** Every golden query (25) and every live
  injection probe (3), with the committed register and without, names no
  instrument at all; no synthetic policy title (6), nor the Act's, is flagged
  for a tenant that holds the Act or one that holds nothing.
- **The register flags known instruments; it does not detect unknown ones**
  (the human's wording, 2026-10-21). Measured with the register empty: the
  "<Name> Act [year]" pattern flags the Fair Work Act, the Corporations Act,
  the My Health Records Act 2012, the NSW Privacy and Personal Information
  Protection Act 1998 and the Victorian Information Privacy Act 2014; it does
  not flag APRA CPS 234, the Privacy (Credit Reporting) Code 2014, the CDR
  rules or the Spam Regulations 2021, and the GDPR only through the register.
  So the gap is an unknown instrument that isn't named as an Act, plus an Act
  whose title starts with a common word, which is reported shortened ("My
  Health Records Act 2012" as "Health Records Act 2012"). The general case,
  statute-shaped references beyond Acts, is #261. GE-02 keeps the gap visible
  in the answer eval.
- **Known limits, the model's:**
  - The pattern misses a lowercase name in a context it can't read as a
    statute's ("is spam act ok?"); the register catches it once signed.
  - A member's private addition (user scope, ADR-0036) is recorded in the
    ledger under the tenant, not the member, so its title counts as held for
    every member of the tenant, though only its owner retrieves it. No upload
    API writes one today; when one does, `environment_titles` needs the
    member.
  - "Held" is the instrument, not the clause: a held instrument whose
    selected clauses don't answer the question is not flagged, as before.
- **Ledger 0018** adds `chat.not_in_environment`. Every existing row takes the
  default, `[]`, which is what those answers were shown with.
- **Neither version moves** (ADR-0034): nothing under `corpus/instruments`,
  `corpus/mappings`, `src/compliance_rag/corpus` or `src/compliance_rag/graph`
  changed.
