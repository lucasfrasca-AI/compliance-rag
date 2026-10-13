# ADR-0032 — Offshore generation serves registered testers over registered tenant text

- Status: accepted. The model chose among #155's options, under the human's
  instruction of 2026-10-14 to close #155 now. The human hasn't ratified the
  choice and can revise it.
- Date: 2026-10-14
- Issue: #155
- Extends: ADR-0023 (the build-phase residency gate), ADR-0026 (synthetic is operator-registered)
- Amends: `D-16` (`DECISIONS.md`)

## Context

ADR-0023 lets generation run outside Australia during the build phase, and
only while the corpus is public law plus synthetic data. Its gate asks one
store, the vector store: does it hold any tenant chunk the synthetic registry
doesn't vouch for? It asks at startup and before every generation call.

The answer endpoint (#149) sends the model inputs that never pass through that
store:

- **The member's question.** A signed-in member can type anything into it,
  real customer data included. ADR-0023's own context names queries as text
  the generator processes.
- **The CAG prefix.** `tenant_policies` reads it from the corpus bucket
  (`ClauseStore`) and the ledger.
- **A clause sent for repair.** The repair prompt carries one cited clause's
  canonical text, which the verifier also reads from the bucket. Reviewing
  this ADR's first draft found this one.

Today tenant text reaches the bucket only through `CorpusLifecycle`, which
refuses real uploads while generation is offshore, so both are synthetic in
practice. But nothing at generation time checked that. The human's
instruction: this is a real gap and a Phase 3 blocker, to be closed now
rather than at deploy time. The human didn't pick among #155's options:

- refuse offshore generation to signed-in members altogether;
- allow it only for an explicit list of build-phase testers;
- check that every prefix clause is registered synthetic.

The first two differ only in whether the list can be non-empty. The third
covers text read from the bucket but not the question. So the question needs
one of the first two, and the bucket text needs the third.

## Decision

**While generation runs outside Australia, an answer goes ahead only if both
of these hold:**

1. **The asker is a registered build-phase tester** (CONTEXT.md).
   - **The rule.** A member's question counts as real data unless its asker is
     on the operator's list, `OFFSHORE_GENERATION_TESTERS`. The list is
     configuration, set by whoever deploys, and a request can't add to it, as
     an upload can't add to the synthetic registry (ADR-0026). It is compared
     lowercased, as the sign-in verifier compares members. Empty, the default,
     means no member's question leaves Australia.
   - **Where it runs.** `assert_question_allowed`, first thing in the API's
     answer stream, before coverage or anything else is computed. The member
     is known only there.
2. **Every piece of tenant text read from the bucket is registered synthetic.**
   - **The rule.** That covers each clause of the CAG prefix, and the clause a
     repair would send. The registry must hold its text under the tenant that
     owns it, which is ADR-0026's key, the one the write path checks.
   - **Where it runs.** `TenantTextGate`, inside `AssessmentPipeline`, which
     can't be built without one, as the generator can't be built without its
     store. It checks the prefix before anything is retrieved or generated,
     and a non-public clause before its repair is sent. No caller of `answer()`
     skips it, whether the API or a script. The live eval registers its
     invented injection probe in memory for its own run, the operator path
     ADR-0026 allows.

**A refusal is loud.** The API ends the stream with an `error` event,
`"error": "residency"`, whose message names the rule and, for bucket text, the
first unregistered clause id. It never echoes the question or the text. The
request span is marked `answer.refused: residency`. The model is never called
with the refused input.

**In Sydney, neither check applies.** Generation in Australia may process real
data, so the gate lifts itself, as ADR-0023's does.

## Why a tester list for the question

The registry vouches for content by hash. A question is typed fresh each time,
so no registry can vouch for it. What the operator can register is the
**person**: someone who knows the build-phase rule.

This is a weaker control than a content hash. A tester could still paste real
data. It is the same trust ADR-0023 places in whoever runs the golden-set
harness, scoped to named people rather than to everyone who can sign in. The
alternative was refusing all offshore generation through the API until
generation is in Sydney. That would leave Phase 3's answer path untestable end
to end against a live model for the whole build phase.

## Consequences

- **Deploying the API offshore serves no one until the operator names
  testers.** That is the fail-closed default.
- **Bucket text costs no extra reads.** The check hashes text the request has
  already loaded, about one sha256 per clause.
- **Retrieved text stays where ADR-0023 put it.** It comes from the vector
  store, which the generator's per-call store gate asks.
- **Moving generation to Sydney** lifts every check with no code change. That
  means a Gemini model on Provisioned Throughput (ADR-0023).
- **D-16 now names the request-time checks** beside startup and ingest.
