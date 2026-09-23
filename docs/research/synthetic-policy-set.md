---
author: synthetic-fixture (drafted by Claude Sonnet 5 for issue #11)
reviewed_by: pending — awaiting human sign-off before use as a Phase 3 fixture
date: 2026-09-23
---

# Synthetic policy set and its conflict matrix

Issue #11. Six synthetic policy documents for fictional company **Acme Pty
Ltd** (`tenant_id: acme`), checked in under
`docs/research/fixtures/synthetic-policies/`. Designed against the matrix
below rather than written to be merely plausible: a policy set that only
looks plausible produces almost entirely `consistent` findings against APPs
1, 5, 6, 8, 11, 12 and 13, and the interesting paths — a genuine contradiction
between two of a company's own documents, a policy that's silent on
something it should cover, a policy whose language is too vague to assess —
stay untested until Phase 3, where they'd be discovered as bugs instead of
exercised on purpose.

These are wholly fictional documents about a fictional company. No clause,
obligation or crosswalk here describes anything real; the risk that
`CLAUDE.md`'s corpus rule exists to prevent — misrepresenting an actual
published instrument — does not apply to test fixtures with no real-world
referent, in the same way `CONTEXT.md`'s Golden set entry already permits
LLM-drafted query text once a human reviews it. `reviewed_by` above is
deliberately left pending: these documents should not be treated as a
finished, human-approved fixture until someone signs off on the specific
verdicts in the matrix below.

## The documents

| Document | File | Role |
| --- | --- | --- |
| D1 | `acme-privacy-policy.md` / `.pdf` | Public-facing privacy policy. Also checked in as a PDF, byte-identical in text content to the Markdown (verified by extracting both and diffing), to exercise PDF position capture. |
| D2 | `acme-data-security-retention-policy.md` | Internal security and retention controls, referenced by D1 §5. |
| D3 | `acme-employee-data-handling-policy.md` | Internal — employee/contractor personal information, a separate context from customer data. |
| D4 | `acme-vendor-cross-border-transfer-policy.md` | Internal — vendor engagement and overseas data transfer. |
| D5 | `acme-marketing-communications-policy.md` | Internal — Marketing team's use of customer data for campaigns. |
| D6 | `acme-access-correction-procedure.md` | Internal — the procedure behind D1 §6, handling access and correction requests. |

## The matrix

A cell is populated only where a document addresses, or should reasonably
be expected to address, the given APP. A document not mentioned for an APP
simply isn't relevant to it (e.g. D5 never addresses APP 11): that's not
the same as `silent`, which is reserved for a document that should cover a
topic — because of what it's for, or because another document points to it
— and doesn't.

| APP | Document | Verdict | Why |
| --- | --- | --- | --- |
| APP 1 (Open and transparent management) | D1 | consistent | Names the policy, its review cadence, and a complaints contact with response timeframes (§1). |
| APP 1 | D2 | **silent** | D1 §5.1 points to D2 for security detail, but D2 never states how it itself is kept current or made available — the same transparency commitment D1 makes about itself isn't extended to the document D1 delegates to. |
| APP 5 (Notification of collection) | D1 | consistent | §2.3 states notice is given at the two collection points, describing what's collected, why, and to whom it may be disclosed. |
| APP 5 | D6 | **ambiguous** | §1.3 refers to "the notice shown to the customer at signup or checkout" when a customer disputes what they were told, but explicitly disclaims specifying what that notice must contain or which version applied — compliance with APP 5 can't be assessed from D6 alone. |
| APP 6 (Use or disclosure) | D1 | consistent (see conflict below) | §3.2: marketing use/disclosure requires **opt-in** consent, revocable at any time. |
| APP 6 | D5 | **conflicting with D1** | §1.1: data collected at signup/checkout may be used for marketing "unless the customer has opted out" — an opt-out default, contradicting D1 §3.2's opt-in requirement. **This is the required contradicting pair.** |
| APP 6 | D3 | consistent | Employee data used only for the stated HR/payroll purposes (§2.1), no secondary use. |
| APP 8 (Cross-border disclosure) | D1 | **ambiguous** | §4.1 promises "reasonable steps" for overseas recipients in general terms, without naming a country or an APP 8.1 mechanism — too vague to assess against a specific transfer. |
| APP 8 | D4 | **conflicting with D1** | §2.1 names an approved vendor in a jurisdiction with no APP-equivalent protection, and §2.2 explicitly waives any jurisdiction-specific assessment before engaging an overseas vendor. Unlike the D1/D5 pair below, this isn't a direct textual contradiction — it's D4 waiving the very assessment D1's "reasonable steps" commitment would require, an omission that undermines rather than restates D1's language. Still a real conflict for Phase 3 to surface, just a softer one. **A second contradicting pair.** |
| APP 11 (Security) | D2 | consistent | Encryption, access control, logging and a breach-response procedure (§1–§4). |
| APP 11 | D3 | consistent | Device encryption, MDM enrolment, and HR-system access control mirroring D2's controls (§3). |
| APP 11 | D4 | **silent** | Vendor engagement covers contracts and jurisdiction (§1, §3) but never requires a vendor to meet any security standard — the one place vendor security obligations would belong, and it's absent. |
| APP 12 (Access) | D1 | consistent | §6.1 states the right to request access and points to D6 for process. |
| APP 12 | D6 | **ambiguous** | §2.1 provides access "as soon as practicable" with no defined timeframe — a real but unquantified commitment, not assessable as consistent or non-compliant on its own. |
| APP 13 (Correction) | D1 | consistent | §6.1 states the right to request correction. |
| APP 13 | D6 | consistent | §3.1–§3.2 give a concrete correction procedure, including notifying third parties who received the incorrect information. |

A note on terminology: this matrix uses `silent`, matching issue #11's own
wording, for a document that should cover a topic and doesn't. `CONTEXT.md`'s
**Finding** entry uses `not_addressed` for the analogous case at
assessment time. The two aren't yet reconciled — whoever builds Phase 3's
conflict-detection logic against this matrix should either treat `silent`
as this fixture's design-time name for `not_addressed`, or extend
`CONTEXT.md`'s vocabulary explicitly; this ticket doesn't decide which.

## Verdict count check

Computed by hand from the table above, not by any script — if a cell in
the table changes, this section must be re-counted by hand too, since
nothing currently checks the two stay in sync.

- `consistent`: D1×APP1, D1×APP5, D1×APP6, D3×APP6, D2×APP11, D3×APP11, D1×APP12, D1×APP13, D6×APP13 — 9 instances.
- `conflicting`: D5×APP6, D4×APP8 — 2 instances, forming two separate contradicting pairs (D1↔D5 on APP 6, D1↔D4 on APP 8).
- `silent`: D2×APP1, D4×APP11 — 2 instances.
- `ambiguous`: D6×APP5, D1×APP8, D6×APP12 — 3 instances.

Each of the four verdicts appears at least twice; at least one pair of
policies contradicts each other (two pairs, in fact); D1 exists in both
Markdown and PDF with identical text content; every document carries
`author`, `reviewed_by` and `date` front matter.
