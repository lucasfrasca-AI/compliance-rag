# ADR-0059 — Check: how a submitted document becomes claims and findings

- Status: accepted. The human answered on 2026-11-15 (quoted below).
- Date: 2026-11-15
- Decision IDs: none in `DECISIONS.md` change. Extends ADR-0053 (Area 1 and
  Area 2), and refines its F5 and F6 (point C1 below).
- Issues: #296, #297, #298, #299, #300, #301, #303, #305, #306. Q9 (where a
  PDF-only submission's text comes from) stays deferred with #307.

## The human's decision

Check batch 2 put ten questions, each with a recommended option (a). On
2026-11-15 the human chose:

> All recommended (a)

## Decisions

1. **Q1. A submitted document is stored as a versioned document of its own
   class,** `submitted_document`: an instrument version, so ADR-0021's
   versioning, manifest, position map, withdraw and purge are reused. It is
   never chunked and never retrieved (ADR-0053 U1). The `assessment` points
   at that instrument version. Not chosen: a bare manifest row with no
   versioning. (#296)
2. **Q2. A claim is a verbatim quote from the submitted document,** checked
   against the stored text the way a citation is (ADR-0003), plus a one-line
   summary. The quote is the claim; the summary only labels it. Not chosen:
   the model's paraphrase with a pointer. (#298)
3. **Q3. The member confirms claims after the findings run.** Rejecting a
   claim removes its findings from the report, and the rejection is recorded
   with who rejected it and when. Not chosen: confirming before findings run
   (two waits, cheaper on bad extractions). (#301)
4. **Q4. The model picks the obligations to check in the extraction call,**
   then one findings call per claim (ADR-0053 F2). Not chosen: a separate
   pick call per claim, about twice the calls. (#298, #299)
5. **Q5. A check is visible to its submitter and the tenant's admins.**
   Anyone else gets 404, as for a chat that isn't theirs (#162). Not chosen:
   the whole tenant. (#297, #300)
6. **Q6. The "policies disagree" banner shows** when one policy's verdict on
   a claim is `conflicts` and another's is `consistent` or `ambiguous`. Not
   chosen: only a direct `conflicts` against `consistent` pair. (#299, #300)
7. **Q7. An issued report never changes.** A report issued before a mapping
   was approved says "a mapping approved since would change this" and offers
   a re-check, which issues a new report. Where a derived finding and a
   direct finding disagree, both are shown. Not chosen: updating reports in
   place. (#306)
8. **Q8. Policy-to-law mappings are proposed when an admin asks,** for one
   policy version at a time. Not chosen: proposing automatically on every
   policy upload. (#305)
9. **C1. A finding derived from an approved mapping is as sure as its
   weakest input:** the claim, the policy verdict and the mapping. The
   approval makes only the mapping link certain; it doesn't lift the claim
   or the verdict. This refines ADR-0053 F6's "`asserted` through the
   approval": the mapping link is `asserted` through the approval, and the
   finding's tier is still F5's weakest input. Not chosen: fully certain
   through the approval. (#306)
10. **C2. A tenant's policy-to-law mappings are ledger rows, never repo
    YAML,** because the repository is public. Repo YAML under
    `corpus/mappings/` stays for public-law mappings only. A proposal is a
    row with `status: proposed` until an admin approves it (CLAUDE.md,
    *Corpus rules*). Not chosen: repo YAML approved by pull request. (#305,
    #306)

## Consequences

- #296 starts now. The Check chain is #296 → #297 → #298 → #299 → #300,
  then #301; #303, #305 and #306 follow ADR-0053's admin order.
- Every option chosen here was the recommended one, so no ticket's scope
  widens. Each ticket's "Pending the human" line is settled by this ADR.
- C1 tightens F6: a test should prove that approving a mapping never raises a
  finding above its claim's tier.
