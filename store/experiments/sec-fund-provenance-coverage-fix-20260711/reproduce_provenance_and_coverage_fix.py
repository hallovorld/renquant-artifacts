"""Sealed reproduction for base-data#43 (Codex CHANGES_REQUESTED, 2026-07-11).

Reproduces the two P1 fixes made in response to Codex's review, from
controlled/synthetic fixtures (no live-tree read, no network, no production
path touched) so the numbers below are independently re-runnable:

1. PROVENANCE FRESHNESS: a ratio finite via carry-forward must not look as
   fresh as the newest filing when a DIFFERENT, older filing actually
   supplied its operand (the 10-Q-drops-shares-after-10-K scenario Codex
   specified verbatim in the review).
2. COVERAGE DENOMINATOR: a good priced-relative coverage number must not
   hide a bad declared/scored-universe number (the 131-of-831-priced
   scenario from the original finding, reproduced here at a 1-of-10 scale
   for a self-contained, fast-running bundle).

Run from an isolated renquant-base-data worktree checked out at the
base-data#43 head commit, with that worktree's ``src`` (and a sibling
``renquant-common/src``) on PYTHONPATH, e.g.:

    PYTHONPATH=<renquant-common>/src:<base-data-worktree>/src \
        python3 reproduce_provenance_and_coverage_fix.py <output_dir>

Writes:
  provenance_case_daily_features.parquet  -- full post-fix daily feature
                                              frame for the 10-Q-missing-
                                              shares ticker (SSS)
  provenance_case_summary.json            -- before/after/provenance/
                                              freshness numbers, human-
                                              readable
  coverage_denominator_case.parquet       -- the 10-ticker/1-priced fixture
                                              frame
  coverage_denominator_summary.json       -- legacy vs universe-denominator
                                              coverage numbers, human-readable
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from renquant_base_data.sec_fundamentals import (  # noqa: E402
    DEFAULT_FEATURE_MAX_AGE_DAYS,
    RAW_VALUE_COLS,
    compute_derived_features,
    compute_feature_coverage,
    compute_feature_freshness,
    forward_fill_to_daily,
)

PRICE = 10.0


def _write_ohlcv(data_dir: Path, ticker: str) -> None:
    dates = pd.date_range("2020-05-10", "2021-06-01", freq="D")
    ohlcv_dir = data_dir / "ohlcv" / ticker
    ohlcv_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"close": PRICE}, index=dates).to_parquet(ohlcv_dir / "1d.parquet")


def run_provenance_case(out_dir: Path) -> dict:
    """The exact scenario Codex specified: a 10-Q that lacks a shares tag
    after a 10-K had one."""
    quarterly = pd.DataFrame(
        [
            {"ticker": "SSS", "end": pd.Timestamp("2020-03-31"),
             "available_date": pd.Timestamp("2020-05-01"), "available_source": "sec_filed",
             "NetIncomeLoss": 10.0, "Assets": 200.0, "StockholdersEquity": 80.0,
             "CommonStockSharesOutstanding": 10.0},
            {"ticker": "SSS", "end": pd.Timestamp("2020-06-30"),
             "available_date": pd.Timestamp("2020-08-01"), "available_source": "sec_filed",
             "NetIncomeLoss": 12.0, "Assets": 210.0, "StockholdersEquity": 82.0,
             "CommonStockSharesOutstanding": np.nan},  # 10-Q omits shares
        ]
    )
    _write_ohlcv(out_dir, "SSS")
    index = pd.date_range("2020-08-05", "2020-08-10", freq="D")

    # PRE-FIX: no carry-forward -> the whole-row-wipe leaves ey/b2p NaN
    # forever after the 10-Q, despite NI/assets/equity all refreshing.
    pre = forward_fill_to_daily(quarterly, index, ["SSS"], value_cols=RAW_VALUE_COLS)
    pre_features = compute_derived_features(pre, out_dir / "ohlcv")
    pre_row = pre_features[pre_features["date"] == pd.Timestamp("2020-08-10")].iloc[0]

    # POST-FIX: carry-forward + per-concept provenance tracking.
    post = forward_fill_to_daily(
        quarterly, index, ["SSS"], value_cols=RAW_VALUE_COLS,
        carry_forward_within_ticker=True, track_concept_provenance=True,
    )
    post_features = compute_derived_features(post, out_dir / "ohlcv")
    post_features.to_parquet(out_dir / "provenance_case_daily_features.parquet", index=False)
    row = post_features[post_features["date"] == pd.Timestamp("2020-08-10")].iloc[0]

    tight = {**DEFAULT_FEATURE_MAX_AGE_DAYS, "earnings_yield": 60, "book_to_price": 60, "roe": 60}
    freshness_tight = compute_feature_freshness(post_features, max_age_days=tight)
    freshness_default = compute_feature_freshness(post_features)

    summary = {
        "scenario": (
            "10-K (end=2020-03-31, available=2020-05-01) tags shares=10.0; "
            "10-Q (end=2020-06-30, available=2020-08-01) refreshes "
            "NetIncomeLoss/Assets/StockholdersEquity but OMITS the shares tag "
            "(the dominant pre-fix coverage hole per orchestrator PR #475). "
            "Evaluated on the daily serving row for 2020-08-10 (9 days after "
            "the 10-Q, 101 days after the 10-K)."
        ),
        "a_coverage_increases": {
            "pre_fix_earnings_yield_finite": bool(np.isfinite(pre_row["earnings_yield"])),
            "pre_fix_book_to_price_finite": bool(np.isfinite(pre_row["book_to_price"])),
            "post_fix_earnings_yield_finite": bool(np.isfinite(row["earnings_yield"])),
            "post_fix_earnings_yield_value": float(row["earnings_yield"]),
            "post_fix_book_to_price_finite": bool(np.isfinite(row["book_to_price"])),
            "post_fix_book_to_price_value": float(row["book_to_price"]),
            "note": "coverage goes from NaN forever (pre-fix) to finite (post-fix carry-forward)",
        },
        "b_row_level_provenance_unchanged": {
            "fiscal_period_end": str(row["fiscal_period_end"].date()),
            "available_at": str(row["available_at"].date()),
            "note": "still describes only the LATEST (10-Q) filing -- the insufficient "
                    "signal a naive freshness check keyed on these alone would trust",
        },
        "c_feature_level_provenance_retains_older_source": {
            "earnings_yield_source_available_at": str(row["earnings_yield_source_available_at"].date()),
            "earnings_yield_source_fiscal_period_end": str(row["earnings_yield_source_fiscal_period_end"].date()),
            "earnings_yield_source_age_days": int(row["earnings_yield_source_age_days"]),
            "book_to_price_source_available_at": str(row["book_to_price_source_available_at"].date()),
            "book_to_price_source_age_days": int(row["book_to_price_source_age_days"]),
            "roe_source_available_at": str(row["roe_source_available_at"].date()),
            "roe_source_age_days": int(row["roe_source_age_days"]),
            "note": "ey/b2p correctly attribute to the OLDER 10-K (2020-05-01, age 101d) "
                    "because shares was carried forward from it; roe attributes to the "
                    "fresh 10-Q (2020-08-01, age 9d) because NI+equity both refreshed there",
        },
        "d_freshness_verdict": {
            "tight_60d_bound": {
                "earnings_yield_fresh_ok": freshness_tight["features"]["earnings_yield"]["fresh_ok"],
                "book_to_price_fresh_ok": freshness_tight["features"]["book_to_price"]["fresh_ok"],
                "roe_fresh_ok": freshness_tight["features"]["roe"]["fresh_ok"],
                "overall_freshness_ok": freshness_tight["freshness_ok"],
            },
            "default_150d_bound": {
                "earnings_yield_fresh_ok": freshness_default["features"]["earnings_yield"]["fresh_ok"],
                "overall_freshness_ok": freshness_default["freshness_ok"],
            },
            "note": "a tight 'current-quarter' (60d) assertion correctly FAILS ey/b2p "
                    "(101d > 60d) while PASSING roe (9d <= 60d); the lenient default (150d) "
                    "still passes at 101d -- the mechanism trips on genuine staleness, not jitter",
        },
    }
    (out_dir / "provenance_case_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def run_coverage_denominator_case(out_dir: Path) -> dict:
    """Reproduces the coverage-denominator finding at a fast, self-contained
    1-priced-of-10-declared scale (the real production incident was
    131-of-831; the ratio -- and the fix -- are denominator-invariant)."""
    universe = [f"T{i}" for i in range(10)]
    frame = pd.DataFrame(
        {
            "date": [pd.Timestamp("2020-05-10")] * 10,
            "ticker": universe,
            "price": [10.0] + [np.nan] * 9,
            "earnings_yield": [0.1] + [np.nan] * 9,
            "book_to_price": [0.2] + [np.nan] * 9,
            "gross_profitability": [0.3] * 10,
            "roe": [0.1] * 10,
            "asset_growth": [0.0] * 10,
        }
    )
    frame.to_parquet(out_dir / "coverage_denominator_case.parquet", index=False)
    coverage = compute_feature_coverage(frame, universe=universe)
    ey = coverage["features"]["earnings_yield"]
    gp = coverage["features"]["gross_profitability"]
    price = coverage["prerequisite_price_coverage"]
    summary = {
        "scenario": (
            "10 declared/scored tickers, only 1 priced on the last serving date "
            "(reproduces the real 131-of-831-priced incident at a 1-of-10 scale). "
            "earnings_yield is finite for that 1 priced name."
        ),
        "earnings_yield_legacy_priced_relative": {
            "coverage": ey["coverage"], "n_have": ey["n_have"], "n_expected": ey["n_expected"],
            "denominator": ey["denominator"],
            "note": "1/1 priced -> a PERFECT 1.0 that looks healthy in isolation (the exact "
                    "problem Codex flagged: this number alone cannot see the OHLCV outage)",
        },
        "earnings_yield_universe_denominator_new": {
            "universe_coverage": ey["universe_coverage"], "n_universe_expected": ey["n_universe_expected"],
            "universe_denominator": ey["universe_denominator"], "universe_ok": ey["universe_ok"],
            "note": "SAME 1 finite cell against the FULL declared universe -> 0.10, correctly "
                    "flagged unhealthy (floor 0.30)",
        },
        "prerequisite_price_coverage_new": {
            "coverage": price["coverage"], "n_have": price["n_have"], "n_expected": price["n_expected"],
            "ok": price["ok"],
            "note": "axis-level: 1/10 priced, correctly flagged unhealthy (floor 0.50) "
                    "independent of any ratio's own coverage",
        },
        "gross_profitability_unaffected": {
            "coverage": gp["coverage"], "universe_coverage": gp["universe_coverage"], "universe_ok": gp["universe_ok"],
            "note": "price-INdependent feature stays healthy on BOTH denominators -- only "
                    "the price-dependent features are correctly capped by the OHLCV outage",
        },
        "combined_axis_verdict": {
            "legacy_coverage_ok": coverage["legacy_coverage_ok"],
            "universe_coverage_ok": coverage["universe_coverage_ok"],
            "prerequisite_price_coverage_ok": coverage["prerequisite_price_coverage_ok"],
            "coverage_ok_combined": coverage["coverage_ok"],
            "note": "the legacy number ALONE says healthy; the combined verdict correctly "
                    "says UNHEALTHY -- exactly Codex's ask ('do not publish an apparently "
                    "healthy fundamentals input artifact for a 16% priced universe')",
        },
    }
    (out_dir / "coverage_denominator_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    provenance_summary = run_provenance_case(out_dir)
    coverage_summary = run_coverage_denominator_case(out_dir)
    print(json.dumps({"provenance_case": provenance_summary, "coverage_denominator_case": coverage_summary},
                      indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
