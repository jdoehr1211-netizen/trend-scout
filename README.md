# trend-scout

Product trend collection, scoring, and recommendation pipeline for
dropshipping research. Phase 1 (this repo state): compliant data
collectors with rate limiting, retries, raw caching, quota guarding, and
a common normalized schema.

## Data sources (and why these)

| Source | Access path | Signal |
|---|---|---|
| Google Trends | SerpApi (paid-capped free tier) | search interest over time + **by region** |
| Reddit | official OAuth API, free tier | product mentions / upvote velocity in niche subreddits |
| AliExpress | official Open Platform Affiliate API | hot-product rank, supplier price, sales volume |

Deliberately **not** collected: Amazon Best Sellers and TikTok Creative
Center scraping both violate ToS. Amazon signal is deferred to a Keepa
API subscription if backtesting justifies it; TikTok signal is deferred
to an Exploding Topics subscription (their API tier is the first paid
upgrade worth making if recommendations feel late to trends).

## Setup

```powershell
cd trend-scout
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env   # then fill in keys — see comments in the file
```

Key registration:

1. **SerpApi** — sign up at serpapi.com, copy the API key. Free tier = 100 searches/month.
2. **Reddit** — reddit.com/prefs/apps → create app, type **script**. Use the
   id under the app name as `REDDIT_CLIENT_ID`. Set a descriptive
   `REDDIT_USER_AGENT` (Reddit requires the `platform:name:version (by /u/user)` format).
3. **AliExpress** — register at openservice.aliexpress.com, create an app with
   Affiliate API access, and get a tracking ID from the Portals dashboard.
4. **Supabase** — create a project, run `sql/001_quota.sql` in the SQL editor.
   Only the quota table is needed in Phase 1. Optional for laptop runs
   (falls back to `data/quota.json`), **required** for GitHub Actions.

## Running

```powershell
# see exactly which API calls would happen + current SerpApi quota state
python -m trend_scout.runner --source all --dry-run

python -m trend_scout.runner --source all                        # everything
python -m trend_scout.runner --source reddit                     # one source
python -m trend_scout.runner --source google_trends --mode regions
```

Outputs:

- `data/raw/<source>/<date>/*.json` — every raw API response, cached before parsing
- `data/normalized/<date>.jsonl` — normalized `TrendDatapoint` rows (Phase 2 moves these to Supabase)
- `logs/trend_scout.log` — rotating log; collectors fail independently and never kill the run

## The SerpApi budget guard

Regional interest data is the core of the whole system and it's the one
paid-capped source, so it is budget-guarded end to end:

- every SerpApi call increments `api_quota` (Supabase, atomic) or `data/quota.json`
- before a batch starts, the guard checks the **whole batch** fits under
  `monthly_budget - budget_reserve` (100 − 10 by default) and refuses
  up front rather than dying mid-cycle
- CI runs hard-fail if Supabase creds are missing, because a per-run local
  quota file would silently bypass the cap
- `--dry-run` prints planned calls and current usage without spending anything

Budget math for the default config lives in `config/settings.yaml`.

## Scheduling

GitHub Actions (`.github/workflows/collect.yml`): push this repo to
GitHub, add the `.env` values as repo **Secrets**, and the cron schedule
runs collectors at cadences tuned to the SerpApi budget (timeseries
weekly, regions monthly, Reddit 6-hourly, AliExpress daily). Collected
data is uploaded as run artifacts until Phase 2 lands Supabase storage.

To run locally on a schedule instead, point Windows Task Scheduler at
`.venv\Scripts\python.exe -m trend_scout.runner --source all`.

## Adding a new data source

1. Create `src/trend_scout/collectors/<name>.py`, subclass `BaseCollector`,
   implement `collect()` (fetch only, no parsing) and `normalize()`
   (raw → `TrendDatapoint` rows). Override `plan()` for `--dry-run` output.
2. Register it in `collectors/__init__.py` (`SOURCES` + `build_collectors`).
3. Add its section to `config/sources.yaml` and any keys to `.env.example`.
4. If the API is paid or capped, wrap calls in a `QuotaGuard`.

Metric names are free-form strings — no schema migration needed.

## Scoring (Phase 2)

Datapoints land in Supabase (`sql/002_phase2.sql`; JSONL stays as a local
audit trail). The scoring engine computes a 0–100 opportunity score per
keyword — the full formula and every component definition live in the
docstring of `src/trend_scout/scoring.py`; the weights and thresholds are
tunable in `config/settings.yaml` → `scoring`.

```powershell
python -m trend_scout.score_cli score                # rank all keywords now
python -m trend_scout.score_cli score --save         # ...and store in Supabase
python -m trend_scout.score_cli backtest --days-ago 30 60
python -m trend_scout.score_cli import-jsonl data/normalized/*.jsonl
```

Components: **momentum** (tanh-squashed growth vs 7/30/90-day baselines),
**regional strength** (share of `interest_by_region` mass inside your
`target_geos`), **saturation** (distance past historical peak, staleness-
weighted), **seasonality** (flag-only until a keyword has 400+ days of
history), and a **confidence** gate that multiplies down sparse/zero-heavy
series instead of hiding them. Weights renormalize over available
components, so missing regional data never silently deflates a score.

The backtest command truncates each series to a past date, scores it with
only that data, and compares against the realized 30-day change — run it
after tuning weights to check the changes actually help. CI re-scores
weekly after each Google Trends collection.

Retention: `purge_expired_datapoints()` (in `002_phase2.sql`) deletes
AliExpress-sourced rows older than 12 months, per the data-compliance
attestation made during API registration.

## Analysis agent (Phase 3)

The agent pulls the top-scoring keywords, packages their trend evidence
(scores, compact series, regional breakdown, and your feedback history) and
asks Claude for a structured analysis per candidate: verdict
(launch/watch/skip), demographics, ad regions, pricing estimates,
competition, and fad-vs-durable risk. Results land in the
`recommendations` table with full reasoning attached.

```powershell
python -m trend_scout.agent run       # analyze top candidates (1 API call)
python -m trend_scout.agent digest    # top-10 digest -> data/digests/DATE.md
python -m trend_scout.agent list      # recent recommendations with ids
python -m trend_scout.agent feedback 12 winner --note "3x ROAS on FB"
```

The feedback loop is what makes it improve: marking recommendations
launched/skipped/winner/loser feeds that history into every future prompt,
so the agent learns which niches actually work for you. Model, top-N, and
the monthly API-call budget are in `config/settings.yaml` → `agent` (calls
are cost-guarded through the same `api_quota` ledger as SerpApi; a weekly
run costs roughly $0.20-0.35). Requires `ANTHROPIC_API_KEY` in `.env` (and
as a repo secret for the weekly CI run, which skips gracefully without it).

## Roadmap

- **Phase 4** — Streamlit dashboard (feed, region heatmaps, trend detail,
  catalog early-warning).
