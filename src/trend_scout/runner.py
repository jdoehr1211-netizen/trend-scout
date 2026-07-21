"""Pipeline runner.

    python -m trend_scout.runner --source all
    python -m trend_scout.runner --source google_trends --mode regions
    python -m trend_scout.runner --source all --dry-run

Each collector runs inside its own try/except: one source failing (bad
credentials, quota exhausted, API change) logs the error and the run
continues. Exit code is 1 only if every requested collector failed.
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import cache, config, storage
from .collectors import SOURCES, build_collectors


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = RotatingFileHandler(
        "logs/trend_scout.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trend-scout")
    parser.add_argument("--source", default="all", choices=SOURCES + ["all"])
    parser.add_argument(
        "--mode",
        default="timeseries",
        choices=["timeseries", "regions", "both"],
        help="google_trends only: which SerpApi data to pull (scheduled separately to fit budget)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the API calls that would be made (incl. quota state) and exit",
    )
    args = parser.parse_args(argv)

    setup_logging()
    log = logging.getLogger("runner")
    cfg = config.load()

    names = SOURCES if args.source == "all" else [args.source]
    collectors = build_collectors(names, cfg.sources, cfg.settings, gt_mode=args.mode)
    if not collectors:
        log.error("no enabled collectors matched %r", args.source)
        return 1

    keywords = cfg.seed_keywords
    regions = cfg.target_geos

    if args.dry_run:
        for c in collectors:
            for line in c.plan(keywords, regions):
                print(line)
        return 0

    ok, failed = [], []
    for c in collectors:
        try:
            raws = c.collect(keywords, regions)
            points = []
            for raw in raws:
                cache.save(raw)
                try:
                    points.extend(c.normalize(raw))
                except Exception:
                    # raw is already cached; a parse bug loses nothing
                    log.exception("%s: normalize failed for %s", c.source, raw.endpoint)
            storage.write(points)
            log.info("%s: %d raw responses -> %d datapoints", c.source, len(raws), len(points))
            ok.append(c.source)
        except Exception:
            log.exception("%s: collector failed; continuing with remaining sources", c.source)
            failed.append(c.source)

    log.info("run complete. ok=%s failed=%s", ok or "-", failed or "-")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
