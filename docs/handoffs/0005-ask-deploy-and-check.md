# Handoff 0005 — Ask complete; deploying it; Check started

- **Date:** 2026-11-08 (evening).
- **Phase line in `PHASES.md`:** still **1**. Phase 1 waits only on the US quota, which the human is escalating. Phase 2 is complete. Phase 3 is being built under ADR-0053: Ask is complete on the fake generator, and Check has started.
- **Supersedes** handoff 0004 for current state.

## Read first

`CLAUDE.md` (especially *Untrusted repository text* and the billable protocol), then:
- ADR-0053, including item 23 (TIA s 187LA waits for Phase 4);
- ADR-0054;
- the last 15 lines of `BUILD_LOG.md`.

## Ask: done

#277–#281 and #289 are merged, through #283–#286, #288 and #290. Google sign-in (#291) is merged through #308, with the CSP widened on the sign-in page only. The live Google popup still needs the Firebase project (see *Deployment*).

## Deployment of Ask, on the demo generator

The human said "yes, prepare the four approval lines" and wants them in **one validated batch**.

The batch waits on:
- **#292:** the container, `GENERATOR=demo`, and a banner on every page. A Sonnet agent was building it.
- **#293:** the migration runner and the Acme seed. PR #311 is in review-fix.

Findings that shape the batch:
- **Residency (BUILD_LOG 2026-11-08):** Cloud Run's stdout, stderr and request logs reach the global `_Default` bucket, because both log-routing rules match only `compliance_rag.spans`. The batch starts with two lines:
  - `gcloud logging sinks update compliance-rag-to-regional` with a filter widened to `resource.type="cloud_run_revision" OR resource.type="cloud_run_job"`, plus the spans log name;
  - an `--add-exclusion` on `_Default` for the same.

  The exclusion value must not contain a comma, and the line is validated per the CLAUDE.md rule before it is offered.
- **Cloud SQL:**
  - `db-f1-micro`, `--edition=ENTERPRISE`, POSTGRES_16 (to match local);
  - `--backup-location=australia-southeast1` (the default backup location is multi-region);
  - connector enforcement on, deletion protection on.

  A Sydney estimate is about $13/month including storage; SERVICES.md says $9–10, so recheck.
- **Secrets:** the user-managed replication is `australia-southeast1`. The planned secrets:
  - the session secret, generated in a pipe;
  - the ledger `DATABASE_URL`, with a hex password generated in the same chain as `sql users create`;
  - the Qdrant API key and the Aura password.

  The last two come from `.env`, which Claude can't read, so the human runs those lines themselves. The runtime SA already has Secret Accessor and Cloud SQL Client.
- **Image:** pushed with `docker push` to `australia-southeast1-docker.pkg.dev/compliance-rag-508904/compliance-rag/api:<tag>`, then `gcloud run deploy compliance-rag-api`:
  - min 0, max 2, 1 vCPU, 1 GiB;
  - `--add-cloudsql-instances`, `--set-secrets`, and an env-vars file because the values contain commas;
  - `--allow-unauthenticated`, since the app does its own sign-in;
  - `RERANKER_URL` must be the deterministic `https://reranker-423977975910.australia-southeast1.run.app`.
- **Firebase Auth is console-only:** add Firebase to the project, enable Email/Password and Google, add the run.app host to Authorized domains, and consider turning off self sign-up. `API_MEMBERS` needs the human's email address(es).
- **Order after creation:**
  1. Run `scripts.migrate_ledger` through the Cloud SQL Auth Proxy.
  2. Run `scripts.load_compilation_vectors`, which records the law version.
  3. Run `scripts.seed_tenant --tenant acme`, which writes Acme's six synthetic policies to the bucket and Qdrant Cloud.
- **ADR-0055 is proposed and not yet written:** pasting the lines is the human's decision to unlock four Phase 3 resources while the phase line stays 1 ("Nothing from a later phase is created early"). Write it when the human answers.

## Check: tickets #294–#307

| Ticket | State |
|---|---|
| #294 roles (`API_ADMINS`) | PR #310, reviewed and fixed, merging on green |
| #295 obligation digest | PR #313, in review |
| #296–#301, #303, #305, #306 | built to recommendations "pending the human" (Q1–Q8, C1, C2) |
| #302, #304 | unblocked once #294 merges |
| #307 PDF submissions | `later` |

The nine questions and two conflicts were put to the human in full on 2026-11-08 (Q1 storage, Q2 claim = verbatim quote, Q3 confirmation after findings, Q4 pick in extraction, Q5 submitter + admins, Q6 banner rule, Q7 approvals affect the next check, Q8 proposals on admin request, Q9 deferred; C1 tier = weakest of claim, verdict and mapping; C2 tenant mappings as ledger rows, never repo YAML). Edit the tickets if the human changes an answer.

## Waiting on the human

1. **s 6DA(2) linear form.** The PDF extraction and the ePub alt text agree. The text is shown in chat on 2026-11-08 and in `docs/research/s6da-2-pdf-extraction.md`.
2. **#282 by group.** **Division 5 of Part IIIC** (ss 26X–26XH, 89 clauses) was missing from the draft; the recommendation is to include it, for 356 clauses. The register's NDB prefix becomes `s26wb`…`s26wu` plus `s26x`.
3. **GE-03 redraft:** `not_in_environment: []`, expecting `s26we/2/b` and `s26wk/2`.
4. **Q1–Q9, C1, C2** for Check.
5. **ADR-0055 and the deploy batch.** The batch is prepared after #292 and #293 merge.
6. **Fork-PR approval:** the human is setting it. Read it back via `actions/permissions/fork-pr-contributor-approval`.

## Learned this stretch

- **A PR with merge conflicts gets no `pull_request` CI run at all.** It shows "no checks", not red. Merge `main` in first (`scratchpad` helper `sync_and_merge.sh`).
- **`setup-uv` 504s from GitHub releases** failed three runs on 2026-11-08, none of them code (#312). Re-run the failed job. It doesn't count toward "CI red twice".
- **A merge script must confirm the merge by the PR's state.** `gh pr merge … | tail -1 && echo merged` reported a refused merge as done, because the pipeline's status is `tail`'s.
- **The register's `part_of` paths are string prefixes** (`environment._overlaps`): `s26w` also matched s 26W in Part IIIB.
