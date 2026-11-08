# ADR-0053 — Phase 3 product decisions: Ask first, a submitted document is only ever checked

- Status: accepted. The human accepted all nineteen recommendations of the
  Phase 3 design session on 2026-11-08, with three specific instructions
  (quoted below). The recommendations, their options and the worked Acme
  example are in `docs/research/phase3-product-options.md`; this record is
  what was decided.
- Date: 2026-11-08
- Touches: D-02, D-03, D-06, D-11 (amended by ADR-0054), D-14, D-17, D-18;
  ADR-0018, ADR-0021, ADR-0027 point 7, ADR-0032, ADR-0034, ADR-0036,
  ADR-0049, ADR-0051; CONTEXT.md *Document class*, *Claim*, *Finding*,
  *Evidence*

## The human's instructions

> I've read all nineteen. Your recommendations are sound and consistent with
> what's locked, so accept all nineteen as recommended and build them.

> Decision 14 (two surfaces): accepted, but build Ask first and completely
> before starting Check. Ask is the thinner path over machinery that already
> works end to end; Check needs claim extraction, the findings taxonomy and
> the upload API. One working surface beats two half-built ones, and Ask will
> teach us things Check's design assumes.

> Decision 19 (htmx): accepted. Reversible is the right criterion at this
> stage and we can replace it if it chafes.

Decision 4, the prefix cap, is ADR-0054.

## Decisions

### Area 2 first: what is uploaded

1. **U1. A proposal is a *submitted document*,** a fourth document class
   (CONTEXT.md). It is stored in the bucket and versioned like any document,
   with its manifest row and position map. It is never chunked, never in the
   CAG prefix, never retrieved, has no graph node (D-14), and is cited only by
   its own assessment. Withdraw and purge are ADR-0021's operations. It can
   never become evidence for another answer, and an assessment keeps the
   document it judged (ADR-0034). Asking about a submitted document outside
   its assessment is not built; whether to record it as a follow-up stays
   open.
2. **U2. Formats.** Policies: structured Markdown (`## N.` sections, `N.M`
   clauses), with an optional PDF for positions. Submitted documents: PDF
   (with positions), Markdown or plain text. DOCX for submitted documents is
   the first follow-up, once a customer names their format. Every format is
   tested against every available fixture.
3. **U4. A new policy version** is chosen explicitly ("new version of
   \<policy\>") with an effective date: today by default, a future date
   allowed, a past date allowed only with a recorded reason. Never matched by
   title.
4. **U6. Two roles.** A member asks and checks documents. An admin also
   manages policy versions, subscriptions, mapping approvals, withdraw and
   purge, and the questionnaire. Admins are configuration (`API_ADMINS`
   beside `API_MEMBERS`) until a user table is worth managing.
5. **U5 is not a decision but a gate.** No upload path ships before #78's
   test (an API upload can't mark itself synthetic), with the lifecycle
   writing clause text first (ADR-0048) and the residency gate standing.
   While generation runs outside Australia, no real customer document passes
   that gate.

### Area 1: the findings taxonomy

6. **F1. The unit of a finding** is one claim against one obligation for
   law, and one claim against one policy clause for policy. Exceptions and
   definitions reach a finding as its citations, never as findings of their
   own.
7. **F2. Which obligations a claim is checked against:** the model picks
   from the obligation digest in the CAG prefix. Every picked id is
   validated against the digest by schema, as clause ids are against the
   retrieved set, and the obligation's clause text is then fetched by id
   within Track A's scope (D-03). No text from a submitted document ever
   becomes a retrieval query. A relevant obligation the model doesn't pick
   goes unreported, which the coverage statement (F7) says.
8. **F3. The verdicts' boundaries.**
   - `consistent`: what the claim describes meets what the clause requires,
     on the claim's own words.
   - `conflicts`: doing what the claim describes would breach the clause, on
     the claim's own words.
   - `not_addressed`: the clause applies to what the claim describes, and the
     claim says nothing that meets or breaches it.
   - `ambiguous`: the verdict turns on a fact or judgement neither the claim
     nor the corpus settles. Every `ambiguous` finding names what would
     settle it. The model never resolves ambiguity under an assumption of its
     own.
9. **F4. An obligation Track A can't decide** gets a conditional finding
   ("would conflict with APP 8.1 if the APPs apply to Acme"), flagged
   unassessable with Track A's questions.
10. **F5. Provenance: one tier per finding,** the weakest of its inputs (D-06).
    A claim counts as `asserted` once a *claim confirmation* exists
    (CONTEXT.md); the verdict counts as `asserted` only when a person accepts
    it. A claim confirmation is not evidence: a proposal's claim states an
    intention, not a fact about the tenant.
11. **F6. An approved policy-to-law mapping changes a finding by
    derivation,** not by prompt. Two kinds, `implements` and
    `partially_implements`; only `implements` derives. A claim's finding
    against the mapped policy clause carries over to the obligation, cited to
    both, `asserted` through the approval. Each approval is a ledger row
    (ADR-0027 point 7) with its approver and `trace_id`.
12. **F7. Every report carries a coverage statement:** the instruments and
    versions checked, the policies by version, `as_of`, the corpus and
    mapping versions, and "not checked: anything else". A web fallback is
    its own ticket, after a decision on what a web query may contain, since
    it is egress outside Australia.
13. **F8. Order:** `conflicts`, `not_addressed`, `ambiguous`, `consistent`,
    then claim order within each, with no severity score (D-06). A banner
    names policies that disagree with each other.

### Area 3: the UI

14. **I1. Three surfaces sharing one citation panel:** Ask (chat), Check (a
    submitted document in, a findings report out), and Admin. **Ask is built
    first and completely, before Check starts** (the human's instruction).
15. **I2. Click-to-clause:** every source the system holds is rendered as
    HTML with clause anchors. Law uses its stored HTML (ADR-0019); a policy is
    rendered from its Markdown with the same anchor scheme, and cited by its
    title and number (ADR-0049). PDF.js is used only for a document that
    arrived as PDF.
16. **I3. While an answer runs,** the page shows stage progress from the
    stream's events and then the verified result. Nothing unverified is shown
    (ADR-0003). A withheld answer is shown as withheld, with its blocked
    citations and reason, never as an error.
17. **I4. Chat history reaches the model** as fenced data: the member's
    earlier questions and the verified answers they were shown, never a
    draft, for the last 3 exchanges. Retrieval uses the current question
    plus the previous member question: member-typed text only, never
    anything retrieved or generated (#29). A member sees only their own
    chats (ADR-0036). Under ADR-0032 only a build-phase tester's history can
    go offshore.
18. **I5. The first admin panel,** in order: subscriptions; policy versions
    with withdraw and purge; the questionnaire from Track A's unknown leaves
    (#123); the mapping approval queue (needs F6's table). "Framework scope"
    is the subscription list until a second framework exists.
19. **I6. Pages are server-rendered** by the API's own Cloud Run service in
    Sydney, with htmx, every asset served from that service under one CSP.
    Chosen because it is the most reversible; replaced if it chafes.

### Corpus

20. **Part IIIC (the Notifiable Data Breaches scheme) joins C2026C00227's
    selection.** "Breach notification is the most-asked Privacy Act question
    in practice and right now we can't touch it." The selection change is a
    reviewed manifest change (D-01, ADR-0042), drafted by the model and
    signed by the human. Once it is selected, the register's NDB entry is
    held, so golden case GE-03, which expects the flag, is re-drafted for
    signature in the same change.
21. **The Spam Act does not join the library now.** "APP 7.8's carve-out
    being detected and flagged is honest, and a whole instrument is Phase 4."
22. **s 7A and s 16C join the selection** (#227): both change who an
    obligation covers. Same path as 20.
23. **TIA s 187LA is held back** pending the human: see *Consequences*.

### Open items answered

24. **s 6DA(2)'s formula:** extract it from the PDF compilation first. If
    that fails, the human transcribes it and signs it (ADR-0047).
25. **#213, #215, #216, #217 are recorded as known limits** in CONTEXT.md,
    with their issues open. None blocks Ask.

## Consequences

- **CONTEXT.md** gains *Submitted document* (a fourth document class),
  *Claim confirmation*, *Policy mapping*, *Coverage statement* and *Known
  parse limits*, and *Finding* gains the verdicts' boundaries.
- **PHASES.md** Phase 3 records the build order: Ask, then Check.
- **TIA s 187LA conflicts with ADR-0051 as built**, so it is held back for
  the human. It is a section of another Act (the Telecommunications
  (Interception and Access) Act 1979), so selecting it means ingesting a
  second instrument, which #227 itself filed as Phase 4. And ADR-0051
  decides "held" per instrument: once any of the TIA Act is in the library, a
  question about its data-retention duties would no longer be flagged as
  outside the environment, though only s 187LA is held. That reverses the
  behaviour the human set for exactly this case ("What must never happen is
  a confident answer about the Spam Act built from APP 7 text"). The two
  routes are: register the TIA Act's held part with `part_of`-style
  semantics (a held instrument whose selection is one section, flagged for
  anything else), or wait for Phase 4.
- **Nothing here changes a stored clause or mapping** until the manifest
  change is signed.
