# Evaluation worksheet

Facts below come from the recorded run (`runs` table, `python main.py funnel`). Anything that
needs human judgment is marked **Pending** and has a command that produces the sheet. Nothing
pending is filled in with an estimate.

## Run record

- Run date: 2026-10-04 (discovery 10:38 UTC; classification and enrichment the same day)
- Policy version: `tier-v2` · Config hash: `dff9ccd434e6` (see `runs` table for per-stage hashes)
- Git commit: not a git repository
- Search matrix: 22 queries × strategies `video`, `video_medium`, `channel` × 2 pages; 30 searches
  executed this run (the 16 original baseline searches were already done), 7,063 of 8,000 budgeted
  quota units used that day
- Classifier: Gemini (`gemini-3.5-flash-lite`), one label per profile that passed the objective rules
- Message models: `gemini-3.5-flash` (20 drafts, its free daily limit) then `gemini-3.5-flash-lite` (86 drafts)
- Discovered unique channels: **1,484** (assignment minimum 50)
- Funnel: 1,484 discovered → 333 in follower range → 300 with recent measurable videos → 222 with
  real audience (reach + engagement) → **116 qualified** (3 `PRIORITY`, 113 `STANDARD`)
- Published emails among the 116 qualified: **56** (34 channel description, 14 video description,
  6 shared agency address, 2 creator website); 60 are `Not Found`
- Before this upgrade the same pipeline produced 4 qualified creators, of which 1 had an email, and 3
  of those 4 had a median of 14–92 views per video

### Message generation status

- **106 of 116** qualified creators have a valid draft (email 63–90 words, DM 15–30 words)
- 10 failed validation three times in a row (length, opening duplicates, or topic overlap) and have no
  draft; `python main.py personalize` retries exactly those
- Collaboration angles chosen by rule: 75 sponsored walkthrough, 16 brand ambassador, 12 UGC tutorial,
  2 co-created resource, plus 1 legacy draft
- Review state: 1 approved and simulated-sent; the rest await human review

## Automated checks (reproducible)

| Check | Result |
|---|---|
| Unit and workflow tests | 72 pass (`python -m unittest discover -s tests`) |
| Regression tests catch their bug | Verified by mutation: reintroducing each of 4 bugs made its test fail |
| Mandatory assignment fields on qualified rows | 116/116 non-empty (name, platform, URL, followers, engagement, category, themes, email-or-`Not Found`) |
| Drafts within length limits | email 63–90 words, DM 15–30 words across all 106 valid drafts |
| Duplicate email openings (first 5 words) | rejected by the validator across the whole batch |
| Fabricated or guessed email | None: every email stores its source URL and kind |

## Manual classification audit — Pending

Produce the sheet: `python main.py audit-sample --n 30` (reproducible, stratified: passed,
borderline, clear failures). For each row open `profile_url` and enter `PASS` or `FAIL` in
`human_decision` (would you pitch this creator for a Technology micro-influencer campaign?). Then
`python main.py audit-score`.

| Metric | Formula | Result |
|---|---|---|
| Pass precision | True passes / all predicted passes | Pending your labels |
| Pass recall | True passes / all human passes | Pending your labels |
| False positives | Predicted pass, human fail | Pending your labels |
| False negatives | Predicted fail, human pass | Pending your labels |

The sample is stratified toward the decision boundary, so recall is a stress test, not a population
estimate. Keep the rubric constant while auditing. If a threshold or prompt changes, apply it with
`python main.py rescore`, record the before/after, and resample.

## Message audit — Pending

Review at least 10 randomly selected valid pairs. Check: verified creator name, correct referenced
video, no claim of having watched or enjoyed it, truthful offer, creator/audience value, one clear
call to action. The validators already enforce length, placeholders, cited-video membership, topic
overlap, brand named, no hashtags or clichés, no overclaims, and no duplicate openings. They cannot
prove semantic truth, so a human check is still required before approval.

## Demo script

1. `python main.py funnel`: show the funnel and the reason codes behind every failure.
2. Open one `PRIORITY`, one `STANDARD` and one failed record in the workbench; read the reasons and
   the contact confidence label.
3. Open a `Not Found` email and a `shared_address`; show neither can be sent blindly.
4. Inspect a draft: its collaboration angle, the cited video, word counts.
5. Approve one draft, then `python main.py send --limit 1` (dry run) and repeat it to show
   `DUPLICATE_BLOCKED`.
6. Optional real test: set `SMTP_*` in `.env`, then `python main.py send --live --to you@example.com --limit 1`
   to see a real message delivered to yourself. Real outreach stays blocked while
   `campaign.live_outreach_approved` is `false`.
7. Export the three CSVs (`python main.py export`).

Do not record real API keys, hidden environment files, or creator contact details beyond those
already intentionally present in the submitted dataset.
