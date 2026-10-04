# Troubleshooting

Start with `python main.py status`. The database keeps completed work; you normally do not need to delete anything or start over.

| Problem | Meaning | What to do |
|---|---|---|
| `YOUTUBE_API_KEY is missing` | Discovery cannot call YouTube | Copy `.env.example` to `.env`, add the API key, and rerun `python main.py discover` |
| HTTP 403 from YouTube | API disabled, invalid/restricted key, or quota exhausted | Check the Google Cloud project's enabled API, key restrictions and quota page; retry after quota returns if needed |
| Fewer than 50 discovered | Search results did not produce enough unique channels | Add relevant Technology search queries to `config.json`, increase `max_candidates` carefully, rerun `discover --refresh`; do not add fabricated rows |
| Many channels fail size range | Search queries reach large or tiny channels | Adjust query specificity; keep the assignment's 5,000–100,000 rule |
| Engagement unavailable | Fewer than three recent longer videos have complete public metrics | Keep the failure reason; consider more recent uploads per channel if quota allows, but never fill unknown statistics with zero |
| `REVIEW_REQUIRED` after assessment | Selected classifier request or response failed validation | Verify `CLASSIFIER_PROVIDER` and its key/model; rerun `python main.py assess` to retry review-required rows |
| `Not Found` email | No accessible published business email was located | Keep `Not Found`; manually review the channel's public business-contact area if desired, recording the source if found |
| No generated draft | Gemini failed or its output could not pass validators | Verify key/model, read the error printed by `personalize`, then rerun with `--refresh` |
| `NOT_APPROVED` | Draft has not passed human review | Use the React workbench or `python main.py approve CHANNEL_ID` after checking evidence |
| `DUPLICATE_BLOCKED` | This campaign already has a simulated email for that recipient | This is expected on the second attempt; inspect `outreach_log.csv` |
| Local interface does not open | FastAPI is stopped, the port is occupied, or `web/dist` has not been built | Run `npm ci` and `npm run build` in `web`; start `python -m uvicorn server:app --host 127.0.0.1 --port 8000` from the project root, choosing another port if needed |
| Interface says keys are missing | `.env` is absent from the project root or a required variable is empty | Add YouTube and Gemini keys to the project-root `.env`; Groq is optional with `CLASSIFIER_PROVIDER=gemini`. Never put keys in `web/` or browser storage |
| Groq returns HTTP 403 while Gemini works | This network may be blocked before the Groq API accepts the request | Use `CLASSIFIER_PROVIDER=gemini`; do not assume the Groq key is invalid from this response alone |
| Certificate verification fails on Python.org macOS Python | Its certificate bundle may not be configured | Set `SSL_CERT_FILE=/etc/ssl/cert.pem` if that file is present; do not disable TLS verification |
| `Daily YouTube quota budget reached` | The local ledger stopped discovery before YouTube would refuse calls | Progress is kept (every search is recorded). Rerun after the Pacific-midnight reset, or raise `youtube_daily_quota_budget` toward 10,000. `python main.py quota` shows usage |
| `HTTP 429 ... retry in 12h` from Gemini | A free-tier **daily** request limit for that model is spent; retrying cannot help | The fallback chain (`GEMINI_PERSONALIZATION_FALLBACK_MODELS`, default the classifier model) takes over automatically. Otherwise rerun tomorrow or enable billing |
| `personalize` says `stopped: rate limited` | Three creators in a row were rate limited, so the batch stopped on purpose | Finished drafts are kept. Rerun later; only creators without a draft are processed |
| Draft fails with `email_body has 9x words` | The model overshot the length limit three times | Rerun `personalize` (it targets ~75 words); a smaller model overshoots more often. Edit manually in the workbench if needed |
| Everything shows `DISCOVERED` after `rescore` | Eligible profiles have no stored model label yet (rescore never calls the model) | Run `python main.py assess` to label them |
| Many failures `LOW_REACH` / `LOW_ENGAGEMENT` | Videos get very few views, so rates are statistical noise | Intended. Thresholds live under `filters` in `config.json`; apply changes with `python main.py rescore` |
| `shared_address` contact | One address is published by several creators (an agency inbox) | Verify before sending; only one creator can be emailed at that address |
| `SEND_UNCERTAIN` | A send may or may not have been delivered (timeout after hand-off, or the process died mid-send) | Check the mailbox's Sent folder. It is never retried automatically, which is deliberate |
| `CAMPAIGN_NOT_APPROVED` | Live sending to creators needs `campaign.live_outreach_approved: true` | Only set it once the campaign brief is genuinely approved. Use `send --live --to you@example.com` to test |

Official setup and API references: [YouTube Data API getting started](https://developers.google.com/youtube/v3/getting-started), [Gemini API models](https://ai.google.dev/gemini-api/docs/models), [Groq supported models](https://console.groq.com/docs/models).
