# TikTok Giveaway Finder

A small Python bot that runs once a day on GitHub Actions. It finds TikTok giveaways (Gewinnspiele, Verlosungen) open to someone living in Germany, estimates how many people compete for each prize, and sends you the ones with the best odds via Telegram and/or e-mail. Each entry links straight to the video.

You enter the giveaways yourself. The bot never comments, follows or likes anything: automated entries break TikTok's rules, get accounts banned, and organizers disqualify them.

```text
🎁 TikTok giveaways - 27.10.2026
41 active giveaways found (118 posts checked). 3 skipped because they're limited to other countries. 12 are at or below 227 competitors per prize - the cheapest 30% of 214 giveaways seen in the last 30 days.

1. 🆕 @kerzenliebe ✔️ · 3 prizes · open to DE ✓
~82 competitors per prize (≈1 in 82) · 140 comments now · ends 02.11. · posted 1d ago
🎉 GEWINNSPIEL 🎉 Wir verlosen 3x unsere neue Duftkerze! Folgt uns, liked & kommentiert…
⚠️ asks you to DM
```

## How it works

Every day at 06:37 UTC (07:37 in Munich in winter, 08:37 in summer), the workflow runs the Apify [TikTok Scraper](https://apify.com/clockworks/tiktok-scraper) with a German proxy. It searches the configured hashtags and keywords, newest first. `giveaway_finder.py` then processes each post:

1. **Is it a giveaway?** The caption needs a giveaway word (Gewinnspiel, verlose, giveaway, …) *and* an entry action (kommentiert, folgt, markiert, …).
2. **Is it still open?** Captions like "beendet" or "Gewinnerin steht fest" are dropped, as are posts with a deadline in the past, posts older than 14 days, and accounts with fewer than 200 followers.
3. **Can you win from Germany?** A post is dropped only when its caption limits the giveaway to other countries ("nur für Österreich", "US only", 🇺🇸 only). Posts that don't mention a region stay in the list, because many German giveaways put their conditions in the bio.
4. **How many people will enter?** Comments are the entry proxy. Young posts are projected forward to their deadline (or 7 days when there is none), since TikTok engagement is front-loaded: the share of lifetime comments after *t* hours is modelled as `1 - exp(-t / 36)`.
5. **Competitors per prize** = projected comments ÷ number of prizes ("3x", "5 Gewinner", …).
6. **Keep the cheapest 30%.** Scores from the last 30 days are stored in `state/history.json`. Only giveaways at or below the 30th percentile are listed, best odds first. With fewer than 10 scores in the history it uses the median instead.

Posts are also flagged (not dropped) when they ask you to click a bio link, DM someone, move to WhatsApp or Telegram, pay fees or shipping, or mention crypto or gift cards. These are common scam patterns.

**The odds are estimates.** Comments are not people: some people comment several times, and some giveaways ask you to tag friends, with one comment per tag. The numbers are good for ranking giveaways against each other, not as exact probabilities. Always read each giveaway's rules (often in the bio) before you enter.

## Setup

### 1. Apify token

1. Create a free account at [apify.com](https://apify.com).
2. Open **Settings → API & Integrations** ([console.apify.com/settings/integrations](https://console.apify.com/settings/integrations)) and copy your personal API token.

### 2. Telegram bot (optional)

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot` and follow the steps. It gives you the **bot token** (`123456789:AA…`).
2. Send any message to your new bot (otherwise it can't write to you).
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find `"chat":{"id":…}`. That number is your **chat id**. For a group, add the bot to the group, post a message there, and use the group's id (it starts with `-`).

### 3. Gmail app password (optional)

1. Turn on 2-Step Verification for your Google account.
2. Create an app password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and copy the 16-character password.
3. Use `smtp.gmail.com`, port `587`, your Gmail address as user, and the app password (not your normal password).

Other providers work too, as long as they support SMTP with STARTTLS.

### 4. GitHub secrets

In the repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
| --- | --- |
| `APIFY_TOKEN` | Apify API token (required) |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat id from `getUpdates` |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | Optional, default `587` |
| `SMTP_USER` | Your e-mail address |
| `SMTP_PASSWORD` | App password |
| `EMAIL_TO` | Recipient(s), comma-separated |

Set up Telegram, e-mail or both. If neither is configured, the list is only printed in the Actions log. Every run also writes `latest_report.md` to the repository.

### 5. First run

Go to **Actions → Daily giveaway run → Run workflow**. After that it runs on its own every day. The workflow commits `state/` and `latest_report.md` back to the repository after each run, so the next run remembers which posts it already sent (🆕 marks new ones) and what the last 30 days looked like.

If the workflow can't push, check **Settings → Actions → General → Workflow permissions**. The workflow asks for `contents: write`, but an organization policy can block that.

If Apify rejects an input field, check its current name on the actor's [Input tab](https://apify.com/clockworks/tiktok-scraper/input-schema) and fix it in `build_run_input()` or `apify_input_extra`. For example, the actor replaced `shouldDownloadSubtitles` with `downloadSubtitlesOptions`.

## Cost

Apify's Free plan includes $5 of usage per month. The actor charges per event. On the Free plan in October 2026 that was:

- $0.0037 per result
- $0.0013 extra per result for the German proxy ("scrape as in country")
- $0.0013 extra per result for newest-first search sorting
- $0.001 per run start

Check the actor's Pricing tab for current prices.

The defaults fetch up to 120 posts a day (6 hashtags/queries × 20). That costs about **$0.45–0.70 per run, or roughly $13–21 a month**, more than the free $5. `max_cost_per_run_usd` (0.50) caps each run; when Apify hits the cap it stops early and you get fewer posts.

To stay within the free credit, fetch fewer posts. For example, set `"hashtags": []` and `"results_per_seed": 5`: 15 newest posts a day, about $0.10 per run (~$3 a month). Search queries sorted newest first are the better source anyway. Hashtag pages show popular posts, which skews toward big giveaways with poor odds.

## Configuration

Everything is tuned in `config.json`. Keys starting with `_` are comments, and missing keys fall back to the defaults below.

| Key | Default | Meaning |
| --- | --- | --- |
| `apify_actor` | `"clockworks/tiktok-scraper"` | Actor to run |
| `hashtags` | `["gewinnspiel", "verlosung", "gewinnspielalarm"]` | Hashtag pages (popular posts, skews to big giveaways) |
| `search_queries` | `["gewinnspiel", "verlosung", "giveaway deutschland"]` | Keyword searches, newest first: the main source |
| `results_per_seed` | `20` | Posts per hashtag or query; main cost driver |
| `proxy_country` | `"DE"` | Country TikTok sees; `null` = Apify default |
| `allowed_countries` | `["DE"]` | Countries you can win in; `[]` turns the region check off |
| `apify_input_extra` | `{"videoSearchSorting": "LATEST"}` | Merged into the actor input last |
| `max_cost_per_run_usd` | `0.5` | Spending cap per run |
| `max_age_days` | `14` | Ignore older posts |
| `assumed_duration_days` | `7` | Run time when no deadline is found |
| `comment_decay_hours` | `36` | τ in the comment projection |
| `percentile_cutoff` | `30` | Keep the cheapest X% |
| `hard_max_per_prize` | `null` | Optional fixed ceiling on competitors per prize |
| `history_days` | `30` | Window of the statistics |
| `min_history_for_percentile` | `10` | Below this many scores, use the median |
| `min_author_followers` | `200` | Skip smaller accounts |
| `max_list_length` | `30` | Maximum entries per list |
| `only_new` | `false` | Only list posts never sent before |

## Running locally

```bash
pip install -r requirements.txt
export APIFY_TOKEN=apify_api_...
python giveaway_finder.py --dry-run --save-raw raw.json   # live fetch, print only, keep a copy
python giveaway_finder.py --sample raw.json               # replay the copy without paying again
python -m unittest discover -s tests -v                   # offline tests
```

- `--dry-run` prints the list and sends nothing. It doesn't change `state/` either.
- `--sample FILE` uses a saved Apify JSON export instead of a live run and never writes state.
- `--save-raw FILE` also saves the raw Apify items.

Without `--dry-run`, the bot sends to every configured channel and updates `state/`. You can also run it from your own `cron` instead of GitHub Actions, but the computer has to be on at that time.

## Files

- `giveaway_finder.py`: the whole bot (standard library plus `apify-client`)
- `config.json`: tuning
- `state/seen.json`: `{post_id: first-listed date}`, pruned after 60 days
- `state/history.json`: `{post_id: {"date", "per_prize"}}` for the rolling 30-day cutoff
- `latest_report.md`: the most recent list
- `.github/workflows/daily.yml`: the daily schedule
- `tests/`: offline tests
