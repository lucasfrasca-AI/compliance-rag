---
author: "model: Claude Opus 5.5 — preparation for the Phase 3 product design session"
reviewed_by: pending — the design session itself
date: 2026-10-14
---

# Phase 3 product: options for the design session

This document prepares the three Phase 3 areas the human kept for a design session (2026-10-14):
- the findings taxonomy;
- the upload API;
- the UI, including chat history (#162) where it meets the UI.

For each question it gives:
- the options, and what each makes easy or forecloses;
- the locked decision or `CONTEXT.md` entry it touches;
- a concrete case from Acme or the Privacy Act;
- a recommendation.

Nothing here is decided. The recommendations are inputs to the session, and every product choice stays with the human. The document is built to be argued with, the same way #93's was.

It is built around one worked example. Phase 3's goal is one sentence: "a signed-in compliance lead uploads a proposal and gets findings against policy and law, with click-to-clause". So the example is an invented Acme proposal, checked by hand.

The options are measured against PHASES.md's three exit criteria:
1. A full document check end to end under 60 s p95, under $0.50.
2. Every citation in the UI opens the source at the clause.
3. Admin can approve a policy-to-law mapping and see it change a finding.

## Since this was written (updated 2026-10-14)

The human's decisions and the merged fixes that change the premises below:
- **#164, an obligation's exceptions (gap 2): option (a), and treated as a correctness bug.** "An answer citing APP 11.1 without 11.3 is incomplete in a way the reader can't detect." An obligation names its exceptions in reviewed mapping content. Track A's scope includes them, and an answer whose context holds a rule clause is given its exceptions by id, not left to ranking. The drafted exception mapping waits for review (D-01).
  - For this document: F1 (a) holds, since exceptions reach a finding as its citations. F2 (b)'s lookup by id fetches an obligation's exceptions with its clauses. Claim 1's APP 7.2 and 7.8 become citable once the mapping is signed.
- **#171, user scope: private to its owner.** "User-private means private." Being fixed with a leakage test against the real store. U1 (b) is still not recommended: a proposal stored as a user document would be retrieved for its owner's other questions.
- **ADR-0032 confirmed: `OFFSHORE_GENERATION_TESTERS` stays empty.** Nobody asks a question offshore until Phase 1's live run, which the human does alone. So during the build phase no member's question or chat history (I4) leaves Australia.
- **#166 and #159 are merged.** The residency gate counts user scope as real, and `answer()` goes through the router.
- **#153 is live on Aura.** The graph records corpus version `corpus-2026.10.14` and mapping version `mappings-2026.10.14`, each with its commit, so F7's coverage statement has real versions to name.
- **#144: the deployed Qdrant collection holds the compilation.** All 779 clauses are in it, and the recall gate has run once against the cloud cluster. The numbers are in BUILD_LOG.

## An agenda for the session

The decisions in the order they depend on each other. A row marked "amends" needs an ADR, because it changes a locked decision or a CLAUDE.md rule.

| # | Decision | Recommendation | Depends on | Amends |
|---|---|---|---|---|
| 1 | U1: what a proposal is | A new term, **submitted document**: stored, never chunked, cited only in its own assessment | | CONTEXT (a new term) |
| 2 | F1: the unit of a finding | Claim × obligation for law, claim × clause for policy | 1 | |
| 3 | F2: which obligations a claim is checked against | The model picks from the obligation digest, and the clause text is fetched by id | 2 | Only if (c): the ingested-text rule |
| 4 | U3: the CAG prefix cap | About 30k tokens, the cache warmed before parallel calls, a loud failure above the cap | 3 | **D-11** (200k) |
| 5 | F3: the four verdicts' boundaries | As defined, with every `ambiguous` naming what would settle it | 2 | |
| 6 | F4: unassessable obligations | Conditional findings with Track A's questions | 5 | |
| 7 | F5: provenance | The weakest input's tier, plus a CONTEXT term for confirming an intention | 5 | CONTEXT (a new term) |
| 8 | F6: mapping approval changes a finding | Derived, `implements` only, through the ledger table | 2, 7 | ADR-0027 point 7 (the table) |
| 9 | F7: what a report didn't check | A fixed coverage statement now; web fallback as its own ticket | | |
| 10 | F8: order | A fixed verdict order, plus a banner when policies disagree | 5 | |
| 11 | U2, U4, U5, U6: formats, supersession, prerequisites, roles | Markdown policies; PDF, Markdown or text for submitted documents; explicit versions; two roles | 1 | |
| 12 | I1–I6: the UI | Check, Ask and Admin; HTML anchors for every source; server-rendered pages first | 1–11 | |

Two corpus questions only the human can answer, independent of the rest:
- Should Part IIIC (breach notification, gap 3) join the selection manifest?
- Should the Spam Act join the library, given APP 7.8 (claim 1)?

## Sources, and ten gaps found while preparing this

**Sources:**
- PHASES.md Phase 3.
- Decisions D-01, D-02, D-04, D-06, D-10, D-11, D-12, D-14, D-17 and D-18.
- ADRs 0003, 0012, 0018, 0019, 0021, 0026, 0027 (point 7), 0028, 0030, 0032, 0033 and 0035.
- CONTEXT.md's Claim, Finding, Provenance tier, Document class, Evidence, Coverage, CAG prefix, Loop B and Position map.
- The six Acme fixtures and their matrix (`synthetic-policy-set.md`).
- The signed Privacy Act mapping and applicability labels.
- The API as built (#146–#149) and the ledger schema.
- The reranker benchmark.

**Gaps:**

1. **Track B couldn't see paragraphs (#163, fixed while preparing this: PR #165, ADR-0035).**
   - The mismatch: obligations cite subclauses such as `sch1/app11/2`, the law is stored down to paragraphs, and Track A's scope matched exact ids. With GA-01's facts, none of the 6 paragraph-level Schedule 1 chunks in the golden corpus was admitted.
   - The effect: APP 11.2's stored text is its chapeau, "If: the entity must take such steps as are reasonable in the circumstances to destroy the information or to ensure that the information is de-identified." Its conditions (a)–(d), including (b) "no longer needs the information" and (d) "not required … to retain", were never retrieved. Every retention finding would have rested on the duty without the conditions its "If" refers to.
   - Now a cited clause admits the clauses inside it. Mean recall@10 of the expected law under Acme's scope went from 0.593 to 0.796.
2. **An obligation's exceptions are never in scope (#164: decided 2026-10-14, option (a), a correctness bug; see "Since this was written").**
   - The mapping lists exceptions and permissions as "clauses, not obligations": APP 7.2–7.6 and 7.8, 8.2, 11.3 and others. Nothing cites them, so Track B never retrieves them.
   - The worked example's claim 1 turns on two of them. APP 7.2 permits the marketing, and APP 7.8 disapplies APP 7 where the Spam Act applies.
3. **The corpus can't check breach notification.**
   - Part IIIC, the Notifiable Data Breaches scheme, isn't in the selection manifest, though the parser produces it.
   - D2 §4 (assess within 24 hours, notify "as soon as practicable") has nothing to be checked against.
   - Adding it is a reviewed corpus change, so it is the human's call.
4. **A proposal has no document class.**
   - CONTEXT.md has three classes: library law, external upload and internal policy. A proposal under check is none of them. `handoffs/0000-setup.md` flagged the missing document term.
   - D-14 and ADR-0018 already say an uploaded document gets no graph node at all. See U1.
5. **Publishing stores no clause text (#233, fixed: ADR-0048; `publish_version` now takes clauses and writes the clause store first).**
   - `CorpusLifecycle.publish_version` writes chunks and the ledger row, but no clause text or manifest to `ClauseStore`. Only `replace_in_place` (a correction in place), compilation ingest (library law) and operator scripts write clause text.
   - The verifier and the CAG prefix both read clause text from `ClauseStore`. So an upload API over today's lifecycle would publish a policy whose citations can never verify.
   - The CI writer allowlist (`test_synthetic_registry.py`) would catch a handler that wrote directly instead. See U5.
6. **No citation carries a link yet, and a policy citation reads "3/1".**
   - The API sends no link for any citation, law included.
   - The eval harness's `clause_link` builds law links from the stored HTML's anchors, but returns None for a policy.
   - `display_form` falls back to the raw path for a policy.
   - Exit criterion 2 needs links, and a readable policy citation. See I2.
7. **There is no policy-to-law mapping table.** ADR-0027 point 7 moves tenant mappings to a ledger table, which doesn't exist yet. `corpus/mappings/README.md` still describes approval as a PR. Exit criterion 3 needs the table. See F6.
8. **The exit criteria constrain the design.** The arithmetic is under "What the exit criteria cost" below.
   - **Retrieval.** Semantic retrieval per claim would put model-extracted upload text into the retrieval query, which CLAUDE.md forbids. It would also need a rerank per claim, at 6–8 s each on one instance.
   - **Cost.** D-11's 200k-token prefix cap can't fit under $0.50 even on a warm cache, because twenty cached reads cost $0.88. Twenty findings calls started together on a cold cache each pay the cache write, which nearly spends the budget even for Acme.
9. **The residency gate ignored user scope (#166, fixed: PR #167).**
   - It counts only tenant-scoped chunks as real, so a user-scoped chunk (one person's private additions) would pass offshore.
   - No path writes one yet. The fix counts user-scoped chunks as real, fail-closed, before any upload path exists (U1(b)).
10. **Housekeeping.**
   - The fixture matrix says `silent` where CONTEXT says `not_addressed`, and the matrix is still unsigned.
   - PHASES.md line 3 says "Current phase: 1" on purpose. `0002-phase1-close.md` leaves Phase 1 open until the US quota allows the verifier pass rate and cost per query to be measured. Phase 2 has no close handoff yet, though its work is merged.
   - `docs/workflow-map.html` still says Claude runs in Australia and reranks 50 candidates.
   - `graph-ontology-options.md:246` says "D2 §2's retention rule", but retention is §3.

## The worked example: Acme's "Customer insights programme"

Acme on GA-01's facts ($4M turnover in FY2025-26): Track A says 35 of the 37 obligations apply. The two that don't are the agency-only APP 3.1 and 12.7. Acme's six policies are the CAG prefix: 43 clauses, about 3,200 tokens.

An invented proposal from Acme's marketing team, in five sentences:

> 1. We will use customers' purchase history to send personalised offers by SMS and email to every customer who has not opted out.
> 2. Purchase histories will be exported weekly to CloudAnalytics Inc for segmentation.
> 3. CloudAnalytics is on the Approved Vendor Register, so no separate assessment of its jurisdiction is needed.
> 4. Segment profiles are kept for 7 years after a customer's account closes, then securely destroyed.
> 5. Customers can ask for a copy of their segment profile, and we will respond within 30 days.

What a correct check says, worked by hand against the clause text. This is not system output.

| Claim | Against | Verdict | Why |
|---|---|---|---|
| 1 | D1 3/2: "…unless you have given us your opt-in consent" | **conflicts** | Opt-out where the policy promises opt-in. The fixture set's planted pair. |
| 1 | D5 1/1: "…unless the customer has opted out" | consistent | |
| 1 | APP 7.1, with 7.2 and 7.8 | ambiguous | 7.2 permits direct marketing if the individual would reasonably expect it and (c) "a simple means" to opt out is given. Whether they'd expect it is a judgement the corpus can't settle. And 7.8 disapplies APP 7 to the extent the Spam Act applies. Neither 7.2 nor 7.8 is retrievable until #164's exception mapping is reviewed and signed. |
| 1 | Email and SMS under the Spam Act 2003 | not checked | Not in the library, and by APP 7.8 possibly the governing law here. The report must say so, not imply compliance. |
| 2 | APP 8.1 | **not_addressed** | It applies: D4 2/1 puts CloudAnalytics in "a jurisdiction with no data protection law substantially similar to the Australian Privacy Principles". The proposal says nothing of the steps "reasonable in the circumstances" that 8.1 requires. |
| 2 | D4 2/1 | ambiguous | CloudAnalytics is approved "to provide website usage analytics". Whether that covers segmenting purchase histories isn't stated. |
| 2 | D4 1/2: vendor contracts "must specify the services provided, the personal information categories involved…" | not_addressed | A new service and a new category of information, and the proposal says nothing about the contract. |
| 3 | D4 2/2: "…does not require a separate assessment…, provided the vendor is listed in the Approved Vendor Register and has signed Acme's standard vendor agreement" | ambiguous | The proposal relies on the listing alone. The exemption also needs the signed agreement, which the proposal doesn't mention. |
| 3 | D1 4/1: "We take reasonable steps to ensure any overseas recipient…" | **conflicts** | The fixture set's second planted pair, through D4. |
| 3 | APP 8.1 | ambiguous | Skipping an assessment isn't by itself a breach: the standard vendor agreement might be the reasonable step. |
| 4 | D2 3/1: "Customer account information is retained … 7 years after closure to meet our obligations under tax and consumer law" | ambiguous | Is a segment profile "account information"? And the 7 years is justified by tax and consumer law, which don't obviously reach marketing segments. |
| 4 | APP 11.2 | ambiguous | Turns on (b), "no longer needs the information", and (d), "not required … to retain". These are the paragraphs #163 kept out of scope until PR #165. |
| 5 | APP 12.4(a)(ii): "within a reasonable period after the request is made" | ambiguous | For an organisation the Act sets no number. The 30 days in 12.4(a)(i) binds agencies, and the OAIC's guidance for organisations isn't in the library. A web fallback could show that guidance as `external`, beside the finding and never inside it. |
| 5 | D6 4/1: escalate after 10 business days | consistent | |

The example shows three things the questions below turn on:
- **Most verdicts against the law are `ambiguous`, and they should be.** The law sets standards ("reasonable", "would reasonably expect"), and a proposal rarely says enough to settle them. A taxonomy that pushes these to `consistent` or `conflicts` makes "a legal judgement the system must not make" (`graph-ontology-options.md:143`).
- **The sharpest findings set policy against policy.** Claim 1 is consistent with D5 and conflicts with D1. Claim 3 leans on D4 2/2's exemption, which conflicts with D1 4/1's promise of reasonable steps. The proposal exposes contradictions in the policy corpus itself.
- **What wasn't checked matters as much as what was:**
  - the Spam Act for claim 1;
  - the exceptions Track A's scope leaves out (#164);
  - Part IIIC, for anything about breaches.

---

## Area 1: the findings taxonomy

### F1: The unit of a finding

CONTEXT.md defines a finding as "one claim against one obligation or policy clause".

| Option | Makes easy | Forecloses |
|---|---|---|
| **(a) Claim × obligation for law; claim × policy clause for policy** | Track A already decides applicability per obligation. A finding's citations are its obligation's clauses. The ledger's `finding` row already has both `obligation_id` and `policy_clause_id`. | A finding about a law clause that no obligation cites, such as a definition or an exception |
| (b) Claim × clause, law and policy alike | One shape | Applicability: Track A doesn't decide clauses, so a finding against an inapplicable clause can't be suppressed |
| (c) Claim × document | Short reports | Clause-level citations, which exit criterion 2 needs |

- **Touches:** CONTEXT Finding; D-06; ADR-0028 (obligations derive from clauses by edge).
- **Example:** claim 2 × OB-PA-APP8-1, citing `sch1/app8/1` and `sch1/app8/1/a` (in scope since #163).
- **Recommendation: (a).** It's what CONTEXT already says. Exceptions and definitions reach a finding as its citations, not as findings of their own.

### F2: Which obligations a claim is checked against

| Option | Makes easy | Forecloses |
|---|---|---|
| (a) Every applicable obligation × every claim | Completeness | The budget: 35 × 5 = 175 pairs for the example, most of them noise |
| **(b) The model picks obligations from the obligation digest; their clause text is then fetched by id** | The digest is already a Phase 3 deliverable (CAG prefix = system prompt + obligation digest + policy corpus). One call per claim. Ids are validated against the digest, as clause ids are against the retrieved set. No text from the upload ever becomes a query, and there's no semantic search to rerank. | A relevant obligation the model fails to pick goes unreported. F7's coverage statement mitigates this. |
| (c) Semantic retrieval with the claim as the query: the top law clauses, mapped back to their obligations | Deterministic selection | CLAUDE.md: ingested text (uploads included) "never reaches query construction", and a claim is model-extracted upload text. It also costs a rerank per claim (see "What the exit criteria cost"). |

- **Touches:**
  - D-02: the model never authors a citation, and here it must not author an obligation id either;
  - D-03: Track B stays within Track A's scope;
  - D-11: what the CAG prefix contains.
- **Example:** for claim 4 the model picks OB-PA-APP11-2 from the digest. The lookup then fetches `sch1/app11/2` and its paragraphs (a)–(d), within Track A's scope.
- **Recommendation: (b).** Add a schema check that every obligation id is in the digest, and keep citation verification unchanged. (c) would need an explicit exception to the ingested-text rule. For example, a claim the user has confirmed could count as member-typed text. That's a decision in its own right.

### F3: The four verdicts, defined

PHASES.md and CONTEXT.md fix the four verdicts: `consistent`, `conflicts`, `not_addressed` and `ambiguous`. What needs deciding is where each one ends, so that labels and the golden set agree:
- **consistent:** what the claim describes meets what the clause requires, on the claim's own words.
- **conflicts:** doing what the claim describes would breach the clause, on the claim's own words. Example: claim 1 against D1 3/2.
- **not_addressed:** the clause applies to what the claim describes, and the claim says nothing that meets or breaches it. Example: claim 2 against APP 8.1, an overseas disclosure with no word on reasonable steps.
- **ambiguous:** the verdict turns on a fact or a judgement that neither the claim nor the corpus settles. Example: claim 4 against APP 11.2. Every `ambiguous` finding names what would settle it, as an unassessable obligation names its questions.

| Option | Makes easy | Forecloses |
|---|---|---|
| **(a) The definitions above, with every `ambiguous` finding carrying what would settle it** | Mirrors Track A's unknown leaves, so a reviewer can act on every ambiguous finding | Nothing in CONTEXT |
| (b) Let the model resolve ambiguity under a stated assumption ("assuming 30 days is reasonable…") | Fewer ambiguous findings | It is the legal judgement the system must not make, and the assumption is the model's |

- **Touches:** D-06 (no scores); CONTEXT Finding and Coverage ("'Possibly' is a first-class answer").
- **Recommendation: (a).** Rename the matrix's `silent` to `not_addressed` when the matrix is signed.

### F4: A finding against an obligation Track A can't decide

On GA-02's facts ($2.5M, nothing else known), every APP obligation except the agency-only APP 3.1 and 12.7 (not applicable) is unassessable for Acme.

| Option | Makes easy | Forecloses |
|---|---|---|
| (a) No finding until the profile settles applicability | Simplicity | A useful report for an incomplete profile, which every new tenant has |
| **(b) A conditional verdict ("would conflict with APP 8.1 if the APPs apply to Acme"), flagged unassessable with Track A's questions** | The `finding` table already has `assessable` and `unassessable_reason`. The questions link to the questionnaire (#123). | Nothing |
| (c) Assume the obligation applies | | D-06: that's guessing |

- **Recommendation: (b).** It makes the questionnaire's value visible: answer two questions, and the conditional findings become firm.

### F5: A finding's provenance

A finding joins two things: the claim (what the document says) and the verdict (what that means against the clause). CONTEXT.md says a claim is "Provenance inferred until the user confirms", and "never promoted or mutated", and that confirming one "creates evidence rather than changing the claim". So confirmation is a record of its own, never an edit to the claim.

| Option | Makes easy | Forecloses |
|---|---|---|
| **(a) One tier per finding: the weakest of its inputs. A claim counts as `asserted` once a confirmation record exists. A verdict becomes `asserted` only when a person accepts it.** | D-06's one tier per finding, never overstated | A display with two separate axes |
| (b) Separate tiers for the claim and the verdict | Precision | A more complex UI for the same information |

- **Example:**
  - Claim 3 is extracted (`inferred`).
  - The compliance lead confirms it, which writes a confirmation record; the claim itself is unchanged.
  - The verdict against D1 4/1 is still the model's, so the finding stays `inferred` until the lead accepts the verdict too.
- **Recommendation: (a).**
- **A CONTEXT question for the session.** "Confirming a claim creates evidence" fits a claim that states a fact about the tenant (#123's D1 §3.3 example). A proposal's claim states an intention, not a fact, so its confirmation needs a record that isn't evidence: a term to add.

### F6: How an approved policy-to-law mapping changes a finding (exit criterion 3)

A mapping says, for example, that Acme's D2 3/1 is how Acme meets OB-PA-APP11-2. The model proposes it and a person approves it (D-01). It lives in a ledger table (ADR-0027 point 7), which isn't built yet.

| Option | Makes easy | Forecloses |
|---|---|---|
| (a) Approved mappings go into the prompt, and the model takes them into account | Little code | Exit criterion 3 would rest on the model choosing to change its answer, run to run |
| **(b) Derived: a claim's finding against the mapped policy clause carries over to the obligation, cited to both and marked `asserted` through the mapping** | Deterministic and demonstrable. The human approval is what changes the finding. | Only `implements` mappings derive anything. A partial mapping derives nothing. |
| (c) Mappings only annotate findings and never change them | The safest option | Exit criterion 3 |

- **Example:**
  - Before: the claim "account records are kept 7 years after closure, then destroyed" is `ambiguous` against APP 11.2, because it turns on (d), whether a law requires the retention.
  - The admin approves "D2 3/1 implements OB-PA-APP11-2".
  - After: the claim is `consistent` with D2 3/1, so it is `consistent` with APP 11.2 through Acme's own approved policy. It is cited to D2 3/1 and APP 11.2(d), and it is `asserted` (the admin approved it), not `inferred`.
- **Recommendation: (b).**
  - Two mapping kinds, `implements` and `partially_implements`. Only `implements` derives a finding.
  - Each approval is recorded with its approver and `trace_id`.

### F7: What a report says it didn't check

| Option | Makes easy | Forecloses |
|---|---|---|
| (a) Nothing: findings only | | The user reads silence as compliance |
| **(b) A fixed coverage statement on every report: the instruments and versions checked, the policies (by version), `as_of`, the corpus and mapping versions (#153), and "not checked: anything else"** | Honesty. The assessment row has columns for the versions, and #153 records them. | Nothing |
| (c) (b), plus a web fallback for each claim (`external`, shown after the findings and never blended, as PHASES says) | Catches the Spam Act and the OAIC's guidance | Needs the web search key and its own residency decision: a query to Brave or Tavily is offshore egress |

- **Recommendation: (b) now, and (c) as its own ticket.** Decide first what a web query may contain. Claim text is customer text: under ADR-0032's logic it can't leave Australia during the build phase. It also can't go to a search API at all without its own decision, or the egress audit PHASES asks for fails.

### F8: Ordering and severity

D-06 forbids scores, so there's no option table: any severity ranking would be a score in disguise.

**Recommendation:**
- **A fixed order:** `conflicts`, then `not_addressed`, then `ambiguous`, then `consistent`. Within a verdict, findings follow claim order.
- **A banner for policies that disagree.** Contradictions between Acme's own policies (claims 1 and 3) get one, e.g. "Your policies disagree: D1 3/2 and D5 1/1".

---

## Area 2: the upload API

### U1: What can be uploaded

| Option | Makes easy | Forecloses |
|---|---|---|
| **(a) Three kinds: a new policy version (internal policy), an external upload, and a document to check (a proposal), with a new CONTEXT term for the last** | Each kind already has its own rules: a policy enters the prefix, an external upload is retrieved, a proposal is only checked | Nothing |
| (b) Treat a proposal as a user-scoped document (`Scope.USER`) | Reuses an existing scope | It would be retrieved for its owner's other questions. User scope is being made private to its owner (#171, decided 2026-10-14); until that lands it is tenant-wide. The residency gate counts it as real (#166, fixed). |
| (c) Don't store proposals: parse, extract and assess, keeping only the claims and findings | The smallest data footprint | Click-to-claim (opening the document at the claim) after the session ends |

- **Touches:** CONTEXT Document class ("a document's class is fixed"); D-14 and ADR-0018 (an uploaded document gets no graph node at all); D-17.
- **Recommendation: (a).** The proposed term is **submitted document**. It is stored in the bucket with its row in `document_manifest` (which exists). It is never chunked into Qdrant, never in the prefix, and cited only in its own assessment.

### U2: Formats

What exists today:
- Markdown policies with a strict structure: `## N.` sections and `N.M` clauses.
- A PDF accepted only as a companion, to supply positions (pymupdf).
- No DOCX parser, and no OCR (Document AI is on demand only).

| Option | For | Makes easy | Forecloses |
|---|---|---|---|
| **(a) Structured Markdown, with an optional PDF for positions** | Policies | Clause ids are stable and readable (`3/1` → "§3.1") | Word-native policy owners, who would have to export or convert |
| (b) PDF or DOCX, with the structure recovered by a parser | Policies | The formats companies actually hold | It needs a structure-recovery parser per format: a project in itself, tested on every fixture (CLAUDE.md) |
| **(c) PDF (with positions), Markdown or plain text** | Submitted documents | Click-to-claim through PDF positions | DOCX, until a parser is added |
| (d) DOCX as well | Submitted documents | The commonest proposal format | It needs a parser, and positions in a format with no pages |

- **Recommendation: (a) and (c) for v1.** DOCX for submitted documents is the first follow-up, once a real customer says which format they use. Every format added is tested against every available fixture.

### U3: Size limits

These come from the budget, not from taste:
- **A submitted document:** up to 30 pages or 20,000 tokens, and 25 claims. The cost model below puts a 20-claim check at about $0.24 for Acme. Claims drive both cost and latency.
- **An upload request:** up to 10 MB, checked before parsing.
- **The policy corpus** is limited by the CAG prefix cap. D-11 says ≤200k tokens, but 200k tokens can't fit under $0.50 even on a warm cache: twenty cached reads cost $0.88. For a 20-claim check, the prefix (policies, system prompt and digest) can be at most:
  - **about 44k tokens** if the first findings call warms the cache before the rest start;
  - **about 71k** if the cache is already warm;
  - **about 6k** if all twenty calls start together on a cold cache. Each one pays the write, because a cache entry exists only once the first response has begun.

  These figures follow from the cost model below: the work other than the prefix costs about $0.19.

  The options:
  - (a) Keep 200k and accept that large corpora exceed $0.50 on every check.
  - **(b) Cap the prefix at about 30k tokens, warm the cache with one call before the rest run in parallel, and fail loudly above the cap.** ADR-0012 already requires a loud failure.
  - (c) Keep 200k, but send only the policies relevant to the claim. That is RAG for policies, against D-11.
- **Recommendation: (b), at 30k tokens,** revisited once a real tenant's policy corpus is measured. Acme's policies are about 3,200 tokens.

### U4: How a new version supersedes the old one

CONTEXT's "Version and retire": a new version is in force from its effective date, and the old one's `valid_to` is set to that date.

| Option | Makes easy | Forecloses |
|---|---|---|
| **(a) The uploader explicitly picks "new version of \<policy\>" and states an effective date: today by default, a future date allowed, a past date allowed with a recorded reason** | Backdating stays visible. A future date lets a policy be loaded before it takes effect. | |
| (b) Match the version to its policy automatically, by title | One less click | A renamed policy becomes a new instrument, with no supersession |
| (c) The effective date is always today | The simplest option | Loading a policy before it takes effect |

- A backdated version changes what an `as_of` answer says for past dates. An assessment, once #153 records it, keeps the versions it cited (ADR-0021).
- Withdraw and purge stay the operations ADR-0021 built. The UI adds no enforcement of its own (PHASES).
- **Recommendation: (a).**

### U5: What must exist before any upload ships

These aren't options, just the prerequisites:
1. **#78's test.** The API takes no `synthetic` field and writes only through `CorpusLifecycle`. PHASES lists this as a blocker.
2. **The lifecycle writes the clause text and the manifest** (gap 4), inside the same guarded path, so a published policy's citations verify.
3. **The residency gate stands.**
   - While generation runs outside Australia, `assert_upload_allowed` refuses any tenant or user chunk the synthetic registry doesn't vouch for (#166 added user scope). The registry holds a hash of the tenant and each chunk's exact text, so in practice only the registered fixtures pass.
   - So in the build phase the upload API can be built and tested, but **no real customer can use it** until generation moves to Sydney (ADR-0023, about $2.2–3k a month).
   - That isn't a defect, but it's worth knowing before the UI is designed around uploads.
4. **Roles (U6).**

### U6: Who may upload

There are no roles yet. That was #145's default 2, which the human has since confirmed for the API built so far.

| Option | Makes easy | Forecloses |
|---|---|---|
| (a) Every member may do everything | Nothing to build | Stopping an analyst from purging a policy |
| **(b) Two roles: member (ask, check documents) and admin (policy versions, subscriptions, mapping approvals, withdraw and purge, the questionnaire)** | Matches PHASES' admin panel. Purge already requires an acknowledged count. | |
| (c) Finer roles, e.g. an approver separate from the uploader | A second pair of eyes on mapping approval | Complexity, before there's a second customer |

- **Recommendation: (b).** Hold the admins in configuration beside `API_MEMBERS` (an `API_ADMINS` list) until there's a user table worth managing.

---

## Area 3: the UI

### I1: The product's shape

| Option | Makes easy | Forecloses |
|---|---|---|
| (a) Chat only, with uploads as attachments to a message | One surface | A findings report you can file. Exit criterion 1 is a document check, not a chat turn. |
| **(b) Two surfaces sharing one citation panel: Check (a document in, a findings report out) and Ask (chat), plus Admin** | The report is the deliverable; chat is for follow-up | |
| (c) The report only | The simplest option | "Chat with history" (PHASES, #162) |

- **Recommendation: (b).** A report can open a chat about one finding: "why is claim 4 ambiguous?"

### I2: Click-to-clause (exit criterion 2)

What exists today:
- **Law:** clauses have anchors in the stored HTML (`#clause-…`, ADR-0019).
- **Policies:** they have no anchors, and a citation displays as "3/1".
- **PDFs:** only D1 has one.

| Option | Makes easy | Forecloses |
|---|---|---|
| **(a) Render every source the system holds as HTML with anchors. The law uses its stored HTML; policies are rendered from their Markdown with the same anchor scheme. PDF.js is used only for documents that arrived as PDF: a submitted proposal, or a policy's PDF companion.** | One mechanism for every citation, and it works for every policy today | A policy's original layout, unless it came with a PDF |
| (b) PDF.js for everything, with positions | The original look | The law has no PDF, every policy would need one, and positions are fuzzy-matched |
| (c) Show the clause text alone, with no document | Trivial to build | Exit criterion 2 ("opens the source at the clause") |

- **A display form for policies:** "Data Security Policy §3.1", built from the document title and the Markdown's `## N.` and `N.M` numbering, in place of "3/1".
- **Recommendation: (a).** PDF.js stays where PHASES put it: positions in a PDF the user supplied, which is where click-to-claim needs them.

### I3: What the user sees while a check or an answer runs

D-02 and ADR-0003 mean nothing unverified is shown, so there's no token-by-token streaming of an answer.

**Recommendation:**
- Show stage progress from the SSE events: coverage, retrieval, then per-claim progress for a check. Then show the verified result.
- Show a withheld answer as withheld, never as an error page, with its blocked citations and the reason (ADR-0003).

### I4: Chat history (#162): do earlier turns reach the model?

Storing history is decided (#162). Whether the model sees it is not.

| Option | Makes easy | Forecloses |
|---|---|---|
| (a) History is display only, and each question stands alone | No change to the prompt contract | Follow-ups: "and for agencies?" means nothing on its own |
| **(b) Earlier turns go into the prompt as fenced data: the member's questions and the verified answers, never a draft. Retrieval uses the current question plus the previous member question.** | Follow-ups work. #29's control holds, because only member-typed text reaches the retrieval query, never anything retrieved or generated. | Longer prompts. Each earlier member turn is member-typed text, so under ADR-0032 only a tester's history could go offshore, and the tester list stays empty (confirmed 2026-10-14). |
| (c) A model rewrites the follow-up into a standalone query | The best retrieval for follow-ups | #29: generated text would flow into the retrieval query |

- **Recommendation: (b), capped at the last 3 exchanges.** (c) is ruled out by #29 unless that control is revisited.
- **Who sees which chats:** a member sees their own.
- **Retention:** until the tenant deletes them. Deletion is its own decision, as purge is.

### I5: The admin panel's first version

PHASES lists four parts: profile fields, policy upload and versions, the mapping approval queue, and framework scope.

**Recommendation**, built in this order:
1. **Subscriptions.** Already built.
2. **Policy versions, with withdraw and purge.** Built in the adapter.
3. **The questionnaire** (#123), built from Track A's unknown leaves.
4. **The mapping approval queue.** This needs the table (F6).

"Framework scope" is the subscription list until a second framework exists. #12 and #136 are Phase 4.

### I6: How the frontend is built and served

SERVICES.md allows Firebase Hosting or Cloudflare Pages.

| Option | Makes easy | Forecloses |
|---|---|---|
| **(a) Server-rendered pages from the API's own Cloud Run service (FastAPI templates, with htmx for SSE)** | No second deployable and no build chain. Every byte is served from Sydney, under one CSP. | Rich client-side interaction. PDF.js still needs JavaScript. |
| (b) A single-page app (React or similar) on Firebase Hosting | The usual choice for panels and PDF viewers. The Firebase Auth SDK is native. | A build chain. Static assets come from a global CDN: they hold no customer data, but they're a second thing to audit. |
| (c) Cloudflare Pages | | A second vendor, for no gain over (b) |

- **Recommendation: (a)** for a solo build, while the surfaces are a report, a chat and an admin list. Move to (b) when the citation panel needs more than htmx gives. This is the most reversible choice here, and the one most worth arguing about.

---

## What the exit criteria cost

**Inputs, measured and estimated:**
- **Measured: Acme's policy corpus.** 43 clauses, 12,672 characters as fenced into the prompt, which is about 3,200 tokens at four characters a token.
- **Estimated: the rest of the cached prefix.** The cached prefix is everything up to the cache breakpoint: the system prompt (about 800 tokens) and the obligation digest (not built; about 1,500 tokens for 37 obligations). So the whole cached prefix is about 5,500 tokens.
- **Prices:** `claude-sonnet-5` at the US multi-region endpoint, at ADR-0024's rates per 1M tokens: $2.20 input, $11.00 output, $2.75 cache write, $0.22 cache read.
- **Measured: the reranker.**
  - 6–8 s warm per query of 20 candidates, and 28 s cold (`reranker-benchmark.md`).
  - It runs on 2 vCPU, with max instances 1 and concurrency 4 (SERVICES.md).
- **Estimates only for every generation latency below.** No live generation has run yet: the US quota is still pending.

**Cost.** A 10-page proposal of about 5,000 tokens yields 20 claims, and findings are produced as in F2 (b): clause text by id, no semantic search.

| Step | Tokens | Cost |
|---|---|---|
| Claim extraction | 5,800 in, 1,600 out | $0.030 |
| Findings, per claim | 1,300 uncached in (the claim and its obligations' clause text), 5,500 cached read, 400 out | $0.0085 |
| … for 20 claims | | $0.17 |
| Cache write, once (the first findings call runs before the rest) | 5,500 | $0.015 |
| Repairs (about 1 citation in 10) | | ~$0.01 |
| **Total** | | **≈ $0.23** |

Against $0.50, Acme fits, but only if the cache is warmed first. If all twenty findings calls start together on a cold cache, each pays the $0.015 write, and the total is about $0.49, right at the limit. And a 200k-token prefix doesn't fit at all: twenty cached reads alone cost $0.88 (see U3).

**Latency** for the same check, all estimated:
- **Extraction:** one call, about 20 s for 1,600 output tokens.
- **One warming findings call:** about 10 s.
- **The other 19 findings calls, in parallel:** about 15 s, if the quota allows 19 concurrent requests. The clause text comes by id, which is a store read, with no embedding and no rerank.
- **Total:** about 45 s against the 60 s p95, which leaves little room. Streaming each claim's findings as they finish (I3) helps the wait feel shorter, but not the p95.

**If claim retrieval were semantic (F2 (c)),** each claim would also need a rerank, at 6–8 s on one instance. Twenty claims would take 2–2.7 minutes, so the options would be:

| Option | Makes easy | Forecloses |
|---|---|---|
| (a) Raise the reranker's max instances to 5 | Keeps D-04's pipeline | Five parallel cold starts (28 s) on the first check. It's a SERVICES.md change, and it uses up free-tier headroom. |
| (b) Fusion only for claims, and the reranker kept for chat | Close to the 45 s above | D-04 ("fuse first, rerank second"): this needs an ADR and a recall check on claim-shaped queries |

That is one more reason to prefer F2 (b).

## Which decisions each exit criterion depends on

| Exit criterion | Decided by |
|---|---|
| A document check under 60 s p95 and $0.50 | F2, U3, and warming the cache before parallel calls. The US quota sets concurrency. |
| Every citation opens the source at the clause | I2 and the policy display form. #163, so the right paragraphs are cited. |
| An admin approves a mapping and sees a finding change | F6, U6 (roles) and the `policy_mapping` table |

## Follow-ups that need neither the human nor the model

- **#166,** the residency gate's user-scope gap: fixed (PR #167).
- **#159,** sending `answer()` through the router: fixed (PR #170). **#160,** a deterministic order for RRF ties: in progress.
- **#164,** an obligation's exceptions in scope: decided (a) on 2026-10-14. The exception mapping is drafted for review; it decides when claim 1's APP 7.2 and 7.8 can be cited.
- **The lifecycle's clause and manifest write** (gap 4), with #78's API test once an upload route exists.
- **A display form for policy clauses** (I2). It's needed whatever the session decides about rendering.
- **The housekeeping in gap 10:** the workflow map and the #93 document's section number, both fixed with this update. Signing the matrix and closing the phase gates are the human's.
