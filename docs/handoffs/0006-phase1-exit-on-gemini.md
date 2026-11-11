# Handoff 0006 — Phase 1 exits on Gemini; Phase 3 continues

- **Date:** 2026-11-11.
- **Phase line:** **3 — The product.** Phase 1 exited today, and Phase 2 was already complete.
- **Supersedes** handoff 0005 for current state. 0005 is still the reference for the deploy batch's shape.

## The generator

- **Generation runs on Gemini 3.5 Flash at the Vertex `us` endpoint** (ADR-0057), Standard PayGo, with no quota request. It is offshore during the build, behind ADR-0023's and ADR-0032's gates.
- **Claude on Vertex is dormant** (`GENERATION_BACKEND=claude`), waiting on its quota. Switching back is the human's decision, recorded by an ADR.
- **Bedrock (ADR-0056) was withdrawn.** The human has no AWS account. No AWS resource was ever created.
- **The history:** Claude Code proposed Bedrock and the human accepted it, but Claude Code didn't check for an AWS account first.
- **Settings:** `GENERATION_BACKEND` is `gemini`, `claude` or `demo`. One `GenerationConfig` is the only source of the model and region (#326/#328).

## Phase 1 exit (live, 2026-11-11)

Measured values are in PHASES.md Phase 1 and in BUILD_LOG. The recorded files are in `corpus/golden/answer_eval/2026-11-11-gemini-3.5-flash*.json`.

| Measure | Result |
|---|---|
| Citations verified | 89/89 |
| Answers verified | 24/25 |
| Cost per question | $0.0103 |
| Monthly cost at 2,000 questions | $20.64 |
| Cache hits | 8/10 |
| Injection probes obeyed | 0/3 |

To run it again:

```
QDRANT_URL=http://localhost:<throwaway port> GEMINI_REGION=us GENERATION_BACKEND=gemini \
  uv run python scripts/run_answer_eval.py [--injection | --environment]
```

Use a throwaway Qdrant with telemetry off. Leave `crag-demo-qdrant` alone. The run loads its own temporary collection.

## Known gaps carried

- **GQ-12** is an uncited answer: retrieval doesn't deliver s 6C(1)(e) or s 6D.
- **GE-01:** the model may answer with only "not in this environment" and no citation, which the product withholds.
- **GE-02** waits on #261.
- **The recall targets** (#200, #221) stay open by the human's decision #220.

## Waiting on the human

1. **The deployment**, all on GCP:
   - approval lines 1–5 from handoff 0005;
   - the three `.env` secret commands;
   - Firebase on `compliance-rag-508904`;
   - fork-PR approval.

   The push, deploy and account-lookup-role lines get re-issued for the current `main` image.
   - The env file must use `GENERATION_BACKEND=demo`, not `GENERATOR`, since #328.
2. **Batch 1 signatures:** s 6DA(2), #282 with Part IIIC Division 5, and GE-03.
3. **Batch 2,** the Check questions Q1–Q8, C1 and C2. They are sent after the signatures.

## Learned

- **Google's 429 on Standard PayGo means "try again later",** so retry with backoff (#329). A 429 is not a quota wall.
- **The live harness and the gates use different stores.** A bug can live in one and not the other: #331 was a fake reranker accepting what the service refuses, and #333 was two ledgers.
- **Check that a chosen option is something the human can use** (an account, a contract, a console) before choosing it.
