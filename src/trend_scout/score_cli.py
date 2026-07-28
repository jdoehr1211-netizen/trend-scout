"""Scoring, backtest, and import CLI.

    python -m trend_scout.score_cli score              # score all keywords now
    python -m trend_scout.score_cli score --save       # ...and write to Supabase
    python -m trend_scout.score_cli backtest --days-ago 30 60
    python -m trend_scout.score_cli import-jsonl data/normalized/*.jsonl
"""
from __future__ import annotations

import argparse
import glob
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, storage
from .scoring import Score, score_keyword


def _load_inputs(cfg):
    series = storage.load_series()
    regions = storage.load_region_interest()
    return series, regions


def _score_all(cfg, as_of: datetime) -> list[Score]:
    series, regions = _load_inputs(cfg)
    scoring_cfg = cfg.settings.get("scoring", {})
    out = []
    for kw, s in sorted(series.items()):
        truncated = [(t, v) for t, v in s if t <= as_of]
        if not truncated:
            continue
        out.append(
            score_keyword(kw, truncated, regions.get(kw), cfg.target_geos, as_of, scoring_cfg)
        )
    out.sort(key=lambda sc: -sc.composite)
    return out


def _print_scores(scores: list[Score], title: str):
    print(f"\n{title}")
    print(f"{'keyword':<28} {'score':>6} {'conf':<7} {'mom':>6} {'reg':>6} {'sat':>6}  detail")
    for sc in scores:
        c = sc.components
        mom = c["momentum"].get("score")
        reg = c["regional"].get("score")
        sat = c["saturation"].get("score")
        growth = c["momentum"].get("growth_pct", {})
        print(
            f"{sc.keyword:<28} {sc.composite:>6.1f} {sc.confidence:<7} "
            f"{mom if mom is not None else '-':>6} "
            f"{reg if reg is not None else '-':>6} "
            f"{sat if sat is not None else '-':>6}  {growth}"
        )


def cmd_score(args, cfg):
    as_of = datetime.now(timezone.utc)
    scores = _score_all(cfg, as_of)
    _print_scores(scores, f"Opportunity scores as of {as_of:%Y-%m-%d} (formula: see scoring.py)")
    if args.save:
        client = storage._supabase_client()
        if client is None:
            print("\nno Supabase credentials; scores not saved", file=sys.stderr)
            return 1
        client.table("scores").insert([s.to_row() for s in scores]).execute()
        print(f"\nsaved {len(scores)} score rows to Supabase")
    return 0


def cmd_backtest(args, cfg):
    """Score with data truncated to N days ago, then compare against what
    actually happened to interest in the following 30 days."""
    now = datetime.now(timezone.utc)
    series, regions = _load_inputs(cfg)
    scoring_cfg = cfg.settings.get("scoring", {})
    from .scoring import _window_avg  # reuse the same window math

    for days_ago in args.days_ago:
        as_of = now - timedelta(days=days_ago)
        rows = []
        for kw, s in sorted(series.items()):
            truncated = [(t, v) for t, v in s if t <= as_of]
            if len(truncated) < 14:
                continue
            sc = score_keyword(kw, truncated, regions.get(kw), cfg.target_geos, as_of, scoring_cfg)
            then_avg = _window_avg(s, as_of)
            future = min(as_of + timedelta(days=30), now)
            future_avg = _window_avg(s, future)
            realized = (
                round((future_avg - then_avg) / then_avg * 100, 1)
                if then_avg and future_avg is not None
                else None
            )
            rows.append((sc, realized))
        rows.sort(key=lambda r: -r[0].composite)
        print(f"\n=== backtest: scores as of {as_of:%Y-%m-%d} vs realized 30d change ===")
        print(f"{'keyword':<28} {'score':>6} {'conf':<7} {'realized 30d':>12}")
        for sc, realized in rows:
            r = f"{realized:+.1f}%" if realized is not None else "n/a"
            print(f"{sc.keyword:<28} {sc.composite:>6.1f} {sc.confidence:<7} {r:>12}")
        ranked = [r for _, r in rows if r is not None]
        if len(ranked) >= 4:
            half = len(ranked) // 2
            top, bottom = ranked[:half], ranked[half:]
            print(
                f"avg realized 30d change — top half by score: "
                f"{sum(top)/len(top):+.1f}%, bottom half: {sum(bottom)/len(bottom):+.1f}%"
            )
        if args.save:
            client = storage._supabase_client()
            if client is not None:
                client.table("scores").insert(
                    [sc.to_row(backtest=True) for sc, _ in rows]
                ).execute()
    return 0


def cmd_import(args, cfg):
    paths = []
    for pattern in args.paths:
        paths.extend(Path(p) for p in glob.glob(pattern, recursive=True))
    if not paths:
        print("no files matched", file=sys.stderr)
        return 1
    n = storage.import_jsonl(paths)
    print(f"imported {n} rows into Supabase")
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="trend-scout scoring")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_score = sub.add_parser("score", help="score all keywords from current data")
    p_score.add_argument("--save", action="store_true", help="write score rows to Supabase")

    p_bt = sub.add_parser("backtest", help="score as of N days ago vs realized outcome")
    p_bt.add_argument("--days-ago", type=int, nargs="+", default=[30])
    p_bt.add_argument("--save", action="store_true")

    p_imp = sub.add_parser("import-jsonl", help="backfill JSONL files into Supabase")
    p_imp.add_argument("paths", nargs="+")

    args = parser.parse_args(argv)
    cfg = config.load()
    return {"score": cmd_score, "backtest": cmd_backtest, "import-jsonl": cmd_import}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
