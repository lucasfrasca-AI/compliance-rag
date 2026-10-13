# ADR-0032 — Offshore generation serves registered testers over a registered prefix

- Status: accepted
- Date: 2026-10-14
- Issue: #155
- Extends: ADR-0023 (the build-phase residency gate), ADR-0026 (synthetic is operator-registered)
- Amends: `D-16` (`DECISIONS.md`)

## Context

ADR-0023 lets generation run outside Australia during the build phase, and
only while the corpus is public law plus synthetic data. Its gate asks one
store, the vector store: does it hold any tenant chunk the synthetic registry
doesn't vouch for? It asks at startup and before every generation call.

The answer endpoint (#149) sends the model two inputs that never pass through
that store:

- **The member's question.** A signed-in member can type anything into it,
  real customer data included. ADR-0023's own context names queries as text
  the generator processes.
- **The policy prefix.** `tenant_policies` reads it from the corpus bucket
  and the ledger. Today policy text reaches the bucket only through
  `CorpusLifecycle`, which refuses real uploads while generation is offshore,
  so the prefix is synthetic in practice. But nothing at generation time
  checks that.

These are the two inputs most likely to carry customer text, and the gate saw
neither. The human's instruction (2026-10-14): a real gap and a Phase 3
blocker, to be closed now rather than at deploy time.

#155 listed three options:

- refuse offshore generation to signed-in members altogether;
- allow it only for an explicit list of build-phase testers;
- check that every prefix clause is registered synthetic.

The first two differ only in whether the list can be non-empty. The third
covers the prefix but not the question. So the question needs one of the
first two, and the prefix needs the third.

## Decision

**While generation runs outside Australia, an answer request is refused
unless both hold, checked before anything is retrieved or generated:**

1. **The member is a registered build-phase tester.**
   - The question counts as real data unless its asker is on the operator's
     list, `OFFSHORE_GENERATION_TESTERS`, compared case-insensitively.
   - A tester is someone who has agreed to ask only about public law and
     synthetic data.
   - The list is configuration, set by whoever deploys. A request can't add
     to it, as an upload can't add to the synthetic registry (ADR-0026).
   - Empty, the default, means no member's question leaves Australia.
2. **Every clause of the policy prefix is registered synthetic.** Its
   canonical text, under the tenant that owns it, is in the synthetic registry
   (ADR-0026, the same keys the write path checks). One unregistered clause
   refuses the request, for a tester too.

A refusal is an `error` event, `"error": "residency"`, whose message names the
rule and, for the prefix, the first unregistered clause id. It never echoes the
question. The model is never called.

**In Sydney, neither check applies.** Generation in Australia may process real
data, so the gate lifts itself, as ADR-0023's does.

## Why a tester list for the question

The registry vouches for content by hash. A question is typed fresh each time,
so no registry can vouch for it. What the operator can register is the
**person**: someone who knows the build-phase rule.

This is a weaker control than a content hash. A tester could still paste real
data. It is the same trust ADR-0023 places in whoever runs the golden-set
harness, and it is scoped to named people rather than to everyone who can sign
in. The alternative was refusing all offshore generation through the API until
generation is in Sydney. That would leave Phase 3's answer path untestable
end to end against a live model for the whole build phase.

## Consequences

- **Deploying the API offshore serves no one until the operator names
  testers.** That is the fail-closed default.
- **The prefix check costs no extra reads.** It hashes text the request has
  already loaded, about one sha256 per policy clause.
- **The repair prompt is covered.** It carries one cited clause's canonical
  text, which is either retrieved (the startup and per-call store gate) or
  from the prefix (this check).
- **Moving generation to Sydney** (a Gemini model on Provisioned Throughput,
  ADR-0023) lifts both checks with no code change.
- **D-16 now names the request-time checks** beside startup and ingest.
