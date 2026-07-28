"""Phase 3: the Claude analysis agent.

    python -m trend_scout.agent run              # analyze top candidates, store recommendations
    python -m trend_scout.agent digest           # write/print the weekly top-10 digest
    python -m trend_scout.agent list             # recent recommendations + ids
    python -m trend_scout.agent feedback 12 launched --note "FB ads, US only"

Flow of `run`:
  1. Pull the latest composite scores from Supabase, take the top N.
  2. Assemble the evidence pack per keyword: score components, a compact
     90-day series, regional breakdown, and the user's feedback history
     (launched/skipped/winner/loser) so the model learns the niche.
  3. One Claude call (structured JSON output) analyzes all candidates.
  4. Store one row per keyword in `recommendations` with reasoning attached.

Cost guard: each run consumes 1 unit of the 'anthropic' budget in the shared
api_quota ledger and hard-stops at the configured monthly cap.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, storage
from .quota import QuotaGuard

log = logging.getLogger("agent")

DIGEST_DIR = Path("data") / "digests"

SYSTEM_PROMPT = """\
You are the product-analysis engine of trend-scout, a dropshipping trend pipeline
run by a solo e-commerce seller in the United States. You receive candidate
products with quantitative trend evidence (Google Trends search interest,
regional breakdowns, computed momentum/saturation scores) and the seller's
track record of past launches.

Analyze each candidate honestly. The data is US-summer-seasonal right now:
distinguish "declining because the niche is dying" from "declining because
everything declines in July". Anchor every claim in the supplied numbers —
if the data is too thin to support a judgment, say so and lower your
confidence rather than inventing certainty. Supplier-cost data from
AliExpress is not yet available; base price guidance on typical market
knowledge and mark it as an estimate.

The seller's feedback history shows what they launched and how it went.
Weight your recommendations toward what has historically worked for them.\
"""

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recommendations"],
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "keyword", "verdict", "viability_summary", "target_demographic",
                    "best_regions", "competition_level", "suggested_retail_usd",
                    "estimated_supplier_cost_usd", "risks", "fad_or_durable",
                    "confidence",
                ],
                "properties": {
                    "keyword": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["launch", "watch", "skip"]},
                    "viability_summary": {"type": "string"},
                    "target_demographic": {"type": "string"},
                    "best_regions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["region", "why"],
                            "properties": {
                                "region": {"type": "string"},
                                "why": {"type": "string"},
                            },
                        },
                    },
                    "competition_level": {"type": "string", "enum": ["low", "medium", "high", "saturated"]},
                    "suggested_retail_usd": {"type": "string"},
                    "estimated_supplier_cost_usd": {"type": "string"},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "fad_or_durable": {"type": "string", "enum": ["fad", "durable", "seasonal", "unclear"]},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
            },
        },
    },
}


# -- data assembly ----------------------------------------------------------

def _latest_scores(client, top_n: int) -> list[dict]:
    """Most recent non-backtest score per keyword, top N by composite."""
    rows = (
        client.table("scores")
        .select("keyword,composite,components,confidence,as_of")
        .eq("backtest", False)
        .order("as_of", desc=True)
        .limit(500)
        .execute()
        .data
    )
    latest: dict[str, dict] = {}
    for r in rows:
        latest.setdefault(r["keyword"], r)      # first seen = newest (desc order)
    return sorted(latest.values(), key=lambda r: -r["composite"])[:top_n]


def _compact_series(series: list[tuple], every: int = 3) -> list[list]:
    """Thin a daily series to every Nth point to keep the prompt lean."""
    pts = [[t.strftime("%m-%d"), v] for t, v in series]
    return pts[::every] + ([pts[-1]] if pts and (len(pts) - 1) % every else [])


def _feedback_history(client, limit: int) -> list[dict]:
    rows = (
        client.table("recommendations")
        .select("keyword,created_at,status,status_note,analysis")
        .neq("status", "new")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
    )
    return [
        {
            "keyword": r["keyword"],
            "when": r["created_at"][:10],
            "outcome": r["status"],
            "note": r.get("status_note"),
            "verdict_was": (r.get("analysis") or {}).get("verdict"),
        }
        for r in rows
    ]


def build_evidence(cfg, client) -> dict:
    agent_cfg = cfg.settings.get("agent", {})
    scores = _latest_scores(client, agent_cfg.get("top_n", 10))
    keywords = [s["keyword"] for s in scores]
    series = storage.load_series(keywords=keywords)
    regions = storage.load_region_interest(keywords=keywords)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "target_geos": cfg.target_geos,
        "candidates": [
            {
                "keyword": s["keyword"],
                "composite_score": s["composite"],
                "score_confidence": s["confidence"],
                "score_components": s["components"],
                "interest_series_90d": _compact_series(series.get(s["keyword"], [])),
                "region_interest": regions.get(s["keyword"], {}),
            }
            for s in scores
        ],
        "seller_feedback_history": _feedback_history(
            client, agent_cfg.get("feedback_history_limit", 30)
        ),
    }


# -- the Claude call --------------------------------------------------------

def analyze(cfg, client) -> list[dict]:
    import anthropic

    agent_cfg = cfg.settings.get("agent", {})
    guard = QuotaGuard(
        "anthropic",
        monthly_budget=agent_cfg.get("monthly_call_budget", 20),
        reserve=agent_cfg.get("budget_reserve", 4),
    )
    guard.check(1)

    evidence = build_evidence(cfg, client)
    if not evidence["candidates"]:
        raise RuntimeError("no scored candidates found — run scoring first")

    api = anthropic.Anthropic()      # reads ANTHROPIC_API_KEY from env
    with api.messages.stream(
        model=agent_cfg.get("model", "claude-opus-4-8"),
        max_tokens=32000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": (
                    "Analyze these candidate products for my store. Evidence pack:\n\n"
                    + json.dumps(evidence, ensure_ascii=False)
                ),
            }
        ],
    ) as stream:
        response = stream.get_final_message()
    guard.record(1)

    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined the request: {response.stop_details}")

    text = "".join(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    usage = response.usage
    log.info(
        "analysis complete: %d recommendations, %d in / %d out tokens",
        len(result["recommendations"]), usage.input_tokens, usage.output_tokens,
    )

    score_by_kw = {c["keyword"]: c["composite_score"] for c in evidence["candidates"]}
    stored = []
    for rec in result["recommendations"]:
        row = {
            "keyword": rec["keyword"],
            "score": score_by_kw.get(rec["keyword"]),
            "analysis": rec,
            "status": "new",
        }
        inserted = client.table("recommendations").insert(row).execute().data
        stored.append(inserted[0] if inserted else row)
    log.info("stored %d recommendations in Supabase", len(stored))
    return stored


# -- digest -----------------------------------------------------------------

def make_digest(client) -> Path:
    rows = (
        client.table("recommendations")
        .select("id,keyword,score,analysis,created_at,status")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
        .data
    )
    latest: dict[str, dict] = {}
    for r in rows:
        latest.setdefault(r["keyword"], r)
    top = sorted(latest.values(), key=lambda r: -(r["score"] or 0))[:10]

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# trend-scout digest — {day}", ""]
    for i, r in enumerate(top, 1):
        a = r["analysis"]
        regions = ", ".join(
            f"{x['region']}" for x in a.get("best_regions", [])[:3]
        ) or "n/a"
        lines += [
            f"## {i}. {r['keyword']}  —  score {r['score']:.0f}, verdict: **{a['verdict']}** ({a['confidence']} confidence)",
            f"- {a['viability_summary']}",
            f"- **Target:** {a['target_demographic']}",
            f"- **Ad regions:** {regions}",
            f"- **Pricing:** retail {a['suggested_retail_usd']} vs supplier ~{a['estimated_supplier_cost_usd']} (estimate)",
            f"- **Competition:** {a['competition_level']}; **type:** {a['fad_or_durable']}",
            f"- **Risks:** {'; '.join(a['risks'][:3])}",
            f"- Feedback: `python -m trend_scout.agent feedback {r['id']} launched|skipped|winner|loser`",
            "",
        ]
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"{day}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return path


# -- CLI --------------------------------------------------------------------

def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="trend-scout agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="analyze top candidates and store recommendations")
    sub.add_parser("digest", help="generate the top-10 digest")
    sub.add_parser("list", help="list recent recommendations")
    p_fb = sub.add_parser("feedback", help="record launched/skipped/winner/loser")
    p_fb.add_argument("rec_id", type=int)
    p_fb.add_argument("status", choices=["launched", "skipped", "winner", "loser"])
    p_fb.add_argument("--note", default=None)
    args = parser.parse_args(argv)

    cfg = config.load()
    client = storage._supabase_client()
    if client is None:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY required", file=sys.stderr)
        return 1

    if args.cmd == "run":
        stored = analyze(cfg, client)
        for r in stored:
            a = r["analysis"]
            print(f"[{r.get('id', '?'):>4}] {a['keyword']:<26} {a['verdict']:<6} ({a['confidence']}) — {a['viability_summary'][:80]}")
        print("\nRun `python -m trend_scout.agent digest` for the full write-up.")
    elif args.cmd == "digest":
        path = make_digest(client)
        print(f"\nsaved -> {path}")
    elif args.cmd == "list":
        rows = (
            client.table("recommendations")
            .select("id,keyword,status,created_at,analysis")
            .order("created_at", desc=True)
            .limit(25)
            .execute()
            .data
        )
        for r in rows:
            print(f"[{r['id']:>4}] {r['created_at'][:10]} {r['keyword']:<26} "
                  f"{(r['analysis'] or {}).get('verdict', '?'):<6} status={r['status']}")
    elif args.cmd == "feedback":
        client.table("recommendations").update(
            {"status": args.status, "status_note": args.note}
        ).eq("id", args.rec_id).execute()
        print(f"recommendation {args.rec_id} -> {args.status}"
              + (f" ({args.note})" if args.note else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
