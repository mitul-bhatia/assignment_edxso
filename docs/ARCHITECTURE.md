# Architecture and design principles

This document explains *why* the system is built the way it is. Each principle comes from a
problem observed in the real run data, not from preference. Numbers are from the run recorded
in [EVALUATION.md](EVALUATION.md).

## The shape of the problem

The pipeline is a **funnel**: 1,484 discovered → 333 in follower range → 300 with recent
measurable videos → 222 with a real audience → 116 qualified → 56 with a published email.
Final yield is the *product* of every stage's pass rate, so improving it rationally means
(1) seeing where the loss is, (2) deciding whether the loss is deserved, (3) fixing the stage
that loses the most for the wrong reasons. `python main.py funnel` prints exactly this, from
stored reason codes.

## Principles

### 1. Order filters by cost and selectivity
Cheapest, most selective checks go first; paid calls go last. Subscriber range is free and
removed 78% of candidates, so only in-range channels get their videos downloaded, and only
channels that survive every objective rule reach the LLM. An LLM call is never spent on a
channel that already failed a free rule (tested: `test_model_is_never_called_when_objective_rules_fail`).

### 2. Separate signals from decisions
A *signal* is something measured or labelled (subscribers, median views, `tech_match`). A
*decision* is the policy's verdict on signals (PASSED / FAILED, tier, reasons). They are stored
separately and the decision logic is a pure module ([src/policy.py](../src/policy.py)).
Consequence: changing a threshold is `python main.py rescore`, which re-applies the policy to
stored labels with **zero** model or network calls. A label that was paid for is never erased
when a rule starts failing the channel (tested, and mutation-checked).

### 3. Reason *codes*, not only sentences
Every failure carries a machine-readable code (`SUBS_BELOW_MIN`, `LOW_REACH`, `NOT_TECH`...).
Free text is for people; codes are for counting, auditing and dashboards.

### 4. Statistics before thresholds
The original engagement rate was the median of `(likes+comments)/views`. On a video with 7
views, one like is a "14%" rate. Real data showed three of the four original shortlisted
creators had median views of 14, 14 and 92. The fix is statistical, not cosmetic:
videos below a view floor are excluded as noise, reach (`median views / subscribers`) is a
separate signal, zero engagement is treated as "unverifiable" (hidden likes look identical),
and the **basis** (long-form vs all formats) is recorded so Shorts-heavy channels are never
silently compared with long-form ones. Thresholds were set from the observed distribution
(p10/p25/p50 of median views = 52 / 227 / 1,368), then documented, not guessed.

### 5. "Done" must mean *finished*, not *attempted*
A real bug found in this build: a free rescore stamped 146 unlabelled profiles as assessed, so
the paid pass skipped them. A completion marker must only be written for a terminal outcome.
This is a regression test.

### 6. Treat scarce resources as a budget
YouTube quota is in *units*: `search.list` costs 100, nearly everything else costs 1. So search
is the scarce resource. A local ledger ([src/quota.py](../src/quota.py)) charges each call
before it is made and stops cleanly at the budget; every search is recorded
(`search_runs`) so it is never repeated and pagination resumes from its stored token. Search
parameters did **not** reliably improve precision (measured), but different strategies returned
largely different channels, so discovery is a matrix of queries × strategies × pages instead of
one clever query.

### 7. Isolate failures; make retries intelligent
One failing channel must not abort a batch (errors are counted and retried next run; permanent
errors like HTTP 404 are not retried forever). Rate limits honour `Retry-After`, back off
exponentially with jitter, are paced by a client-side throttle, and trip a **circuit breaker**
after repeated 429s so a batch stops cleanly and resumes later instead of silently dropping
every remaining creator.

### 8. Trust no single data source
Mined contact emails go through checks that mirror how they go wrong in practice: no-reply and
platform addresses are rejected; another company's support desk found on a creator's page is
rejected; an address found in a *video* description (which may belong to a guest or sponsor)
must repeat across videos or follow a contact word and is labelled lower confidence; one
address attached to several channels is an agency inbox and is flagged `shared_address`.
Every email stores its source URL and kind. Nothing is ever guessed.

### 9. Code decides, the LLM writes
The collaboration angle, the opening style and the call-to-action are chosen by deterministic
rules (hash/feature based), so they are reproducible and spread across a batch. The model only
writes the language. Validators then check what code *can* check: word counts, cited-video
grounding (lexical), banned claims ("loved your video" implies we watched it, but we only have
titles), clichés, hashtags, and cross-message opening duplicates.

### 10. Irreversible actions are at-most-once
A duplicate email to a real person is worse than a missed one. The sender reserves a unique
idempotency key in the log *before* sending, calls the network outside the DB transaction, then
settles the row. A failure that provably never left (bad credentials, refused recipient)
releases the key for retry; an ambiguous one (timeout after hand-off) is parked as
`SEND_UNCERTAIN` for a human. Real sending needs `--live`, SMTP credentials, **and**
`campaign.live_outreach_approved: true`; `--to you@example.com` test mode routes everything to
the operator. Dry-run, test and live use separate key namespaces. Suppression list and daily
cap are enforced.

### 11. Measure against people
A filter is only "meaningful" if it agrees with human judgment. `audit-sample` draws a
reproducible stratified sample (passed / borderline / clear failures); you label it;
`audit-score` reports precision and recall.

### 12. Provenance everywhere
Every stage execution is recorded in `runs` with the config hash and policy version, and each
profile stores the policy version that decided it, so an old result is detectable as stale.

## Module map

| Module | Responsibility | Pure? |
|---|---|---|
| `src/policy.py` | Metrics, hard rules, scoring, decision | yes |
| `src/assessment.py` | Wire policy to storage and the classifier; `rescore` | no |
| `src/discovery.py` | Search matrix, pagination, pre-filter, video fetch | no |
| `src/quota.py` | Quota ledger and budget | no |
| `src/enrichment.py` | Contact mining, crawl, safeguards | no |
| `src/personalization.py` | Angle choice, prompt, validators | partly |
| `src/mailer.py` | Transport interface: dry-run, SMTP | no |
| `src/outreach.py` | Review, idempotent send state machine, suppression | no |
| `src/funnel.py`, `src/audit.py` | Reporting and human evaluation | no |

## Scaling from 50 to 500+

Measured, not estimated: classification costs ~3.9 s per profile and message generation ~12 s,
both sequential. At 500 candidates that is ~33 minutes and ~100 minutes. The constraint is the
model's requests-per-minute quota, not CPU, so the correct next step is a **rate-limited worker
pool** (concurrency = allowed RPM × latency), not unbounded threads. Others, in order of payoff:

1. Move SQLite to Postgres and the pipeline stages to a job queue (each stage is already
   idempotent and resumable, which is the hard part).
2. Batch several channels per classification call for cheaper models.
3. Add a second discovery source (another platform or directory) behind the same
   `STRATEGIES`-style interface.
4. Replace the fixed fit-score weights with weights fitted to audit labels once there are enough.
5. Add reply tracking (IMAP) to close the loop from `SENT` to outcome.
