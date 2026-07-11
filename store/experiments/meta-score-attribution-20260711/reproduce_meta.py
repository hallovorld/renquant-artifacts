#!/usr/bin/env python3
"""Reproduce live XGB panel raw scores for 2026-07-06/07/10 and run SHAP attribution.

READ-ONLY on all production paths. All outputs land in the scratchpad.
Replicates the live serving path in
backtesting/renquant_104/kernel/panel_pipeline/job_panel_scoring.py (ApplyScoresTask):
  alpha158 (online from OHLCV cache) + 5 fund (sec_fundamentals_daily.parquet, as-of)
  + 3 PEAD + 3 SUE (earnings_surprise/{T}.parquet, 60d decay, context-median fill)
  + 3 sentiment (zeroed: BULL_CALM regime gate, artifact trained_zeroing)
  -> reindex to feature_cols -> transform_feature_frame(raw clip + global_z)
  -> booster.predict
"""
import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

UMB = Path("/Users/renhao/git/github/RenQuant")
K104 = UMB / "backtesting/renquant_104"
SC = Path("/private/tmp/claude-502/-Users-renhao-git-github-renquant-orchestrator/2244bd05-9699-4a07-8836-2b6d9e43ca5f/scratchpad")
ART = K104 / "artifacts/prod/panel-ltr.alpha158_fund.json"

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

a158 = load_module("a158", K104 / "kernel/panel_pipeline/alpha158_features.py")
ftr = load_module("ftr", K104 / "kernel/panel_pipeline/feature_transform.py")

artifact = json.loads(ART.read_text())
feature_cols = artifact["feature_cols"]
booster_path = SC / "booster.json"
booster_path.write_text(artifact["booster_raw_json"])
booster = xgb.Booster()
booster.load_model(str(booster_path))

cfg = json.loads((SC / "s104-pin/configs/strategy_config.json").read_text())
watchlist = [str(t) for t in cfg.get("watchlist", [])]

recorded = pd.DataFrame(json.loads((SC / "recorded_scores.json").read_text()))
runs = {
    "2026-07-06": "2026-07-06-live-ebb9c2ca",
    "2026-07-07": "2026-07-07-live-dc2a3247",
    "2026-07-10": "2026-07-10-live-6f9d5284",
}

FUND_COLS = ["earnings_yield", "book_to_price", "gross_profitability", "roe", "asset_growth"]
PEAD_COLS = ["days_since_earnings", "pead_signal", "pead_quintile_rank"]
SUE_COLS = ["sue_signal", "surprise_momentum", "surprise_streak"]
SENT_COLS = ["sentiment_pos_share", "mean_sentiment", "n_articles_log"]

fund_panel = pd.read_parquet(UMB / "data/sec_fundamentals_daily.parquet")
fund_panel["date"] = pd.to_datetime(fund_panel["date"])
earn_dir = UMB / "data/earnings_surprise"

_ohlcv_cache: dict[str, pd.DataFrame] = {}
def get_ohlcv(t):
    if t not in _ohlcv_cache:
        p = UMB / f"data/ohlcv/{t}/1d.parquet"
        _ohlcv_cache[t] = pd.read_parquet(p) if p.exists() else None
    return _ohlcv_cache[t]

def finite_or_none(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None

def median_fill(raw_by_ticker, target_tickers, context_tickers, cols):
    medians = {}
    for col in cols:
        vals = [float(raw_by_ticker.get(t, {}).get(col)) for t in context_tickers
                if finite_or_none(raw_by_ticker.get(t, {}).get(col)) is not None]
        medians[col] = float(np.median(vals)) if vals else 0.0
    filled = {}
    for t in target_tickers:
        raw = raw_by_ticker.get(t, {})
        filled[t] = {c: (finite_or_none(raw.get(c)) if finite_or_none(raw.get(c)) is not None
                         else medians[c]) for c in cols}
    return filled, medians

_earn_cache: dict[str, pd.DataFrame] = {}
def get_earn(t):
    if t not in _earn_cache:
        ep = earn_dir / f"{t}.parquet"
        if not ep.exists():
            _earn_cache[t] = None
        else:
            e = pd.read_parquet(ep).reset_index()
            e = e.rename(columns={e.columns[0]: "earnings_date"})
            e["earnings_date"] = pd.to_datetime(e["earnings_date"])
            _earn_cache[t] = e.sort_values("earnings_date").reset_index(drop=True)
    return _earn_cache[t]

def pead_raw_row(t, today_ts):
    earn = get_earn(t)
    if earn is None:
        return {}
    prior = earn[earn["earnings_date"] <= today_ts]
    if len(prior) == 0:
        return {}
    last = prior.iloc[-1]
    days_since = int((today_ts - last["earnings_date"]).days)
    if days_since > 60 or days_since < 0:
        return {}
    decay = max(0.0, 1.0 - days_since / 60)
    surprise = finite_or_none(last.get("surprise_pct")) or 0.0
    return {"days_since_earnings": float(days_since), "pead_signal": surprise * decay,
            "pead_surprise": surprise}

def sue_raw_row(t, today_ts):
    earn = get_earn(t)
    if earn is None:
        return None
    prior = earn[earn["earnings_date"] <= today_ts]
    if len(prior) == 0:
        return None
    last = prior.iloc[-1]
    days_since = int((today_ts - last["earnings_date"]).days)
    if days_since > 60 or days_since < 0:
        return "oow"
    decay = max(0.0, 1.0 - days_since / 60)
    s = prior["surprise_abs"].astype(float).tail(8).reset_index(drop=True)
    if len(s) >= 2:
        denom_window = s.iloc[max(0, len(s) - 1 - 4):len(s) - 1]
        denom = float(denom_window.std()) if len(denom_window) >= 2 else 0.0
        sue = float(s.iloc[-1]) / max(denom, 1e-6)
        sue = max(min(sue, 5.0), -5.0)
    else:
        sue = 0.0
    mom = float(s.iloc[-1] - s.iloc[-2]) if len(s) >= 2 else 0.0
    streak = 0
    cur_sign = 0
    for v in s:
        sign = 1 if v > 0 else (-1 if v < 0 else 0)
        if sign == 0 or sign != cur_sign:
            streak = sign
            cur_sign = sign
        else:
            streak += sign
    return {"sue_signal": sue * decay, "surprise_momentum": mom * decay,
            "surprise_streak": float(streak) * decay}

EXTRA_TICKERS = ["FTNT", "APH", "NFLX", "MSFT", "NVDA", "AAPL"]  # controls scored even if absent that day

results = {}
for day, run_id in runs.items():
    today_ts = pd.Timestamp(day)
    rec = recorded[recorded.run_id == run_id].set_index("ticker")
    targets = sorted(set(rec.index) | set(EXTRA_TICKERS))
    context = list(dict.fromkeys(watchlist + targets))

    rows = {}
    for t in targets:
        df = get_ohlcv(t)
        if df is None or df.empty:
            continue
        d = df[df.index <= today_ts]
        if len(d) < 70:
            continue
        feats = a158.compute_alpha158_at(d, today_ts)
        if feats:
            rows[t] = dict(feats)

    # fund (as-of <= today, context-median fill)
    snap = fund_panel[fund_panel["date"] <= today_ts].sort_values("date").groupby("ticker").tail(1)
    by_ticker = {str(t): g.iloc[-1] for t, g in snap.groupby("ticker", sort=False)}
    raw_fund = {t: {c: (finite_or_none(by_ticker[t][c]) if t in by_ticker and c in by_ticker[t].index else None)
                    for c in FUND_COLS} for t in context}
    filled, fund_medians = median_fill(raw_fund, list(rows.keys()), context, FUND_COLS)
    for t in rows:
        rows[t].update(filled[t])

    # PEAD
    raw_pead = {}
    for t in context:
        r = pead_raw_row(t, today_ts)
        raw_pead[t] = {"days_since_earnings": r.get("days_since_earnings"),
                       "pead_signal": r.get("pead_signal"), "pead_quintile_rank": None}
        if r.get("pead_surprise") is not None:
            raw_pead[t]["pead_surprise"] = r["pead_surprise"]
    surprises = {t: raw_pead[t]["pead_surprise"] for t in context
                 if finite_or_none(raw_pead.get(t, {}).get("pead_surprise")) is not None}
    if surprises:
        ranks = pd.Series(surprises, dtype=float).rank(pct=True)
        for t, rk in ranks.items():
            raw_pead[t]["pead_quintile_rank"] = float(rk)
    filled, pead_medians = median_fill(raw_pead, list(rows.keys()), context, PEAD_COLS)
    for t in rows:
        rows[t].update(filled[t])

    # SUE
    raw_sue = {}
    for t in context:
        r = sue_raw_row(t, today_ts)
        raw_sue[t] = r if isinstance(r, dict) else {c: None for c in SUE_COLS}
    filled, sue_medians = median_fill(raw_sue, list(rows.keys()), context, SUE_COLS)
    for t in rows:
        rows[t].update(filled[t])

    # sentiment: BULL_CALM regime gate + trained_zeroing artifact -> zeroed
    for t in rows:
        for c in SENT_COLS:
            rows[t][c] = 0.0

    X = pd.DataFrame.from_dict(rows, orient="index")
    X_aligned = X.reindex(columns=feature_cols, fill_value=float("nan"))
    X_t = ftr.transform_feature_frame(X_aligned, feature_cols, artifact, source_space="raw")

    dm = xgb.DMatrix(X_t[feature_cols].values, feature_names=feature_cols)
    preds = booster.predict(dm)
    contribs = booster.predict(dm, pred_contribs=True)

    out = pd.DataFrame({"pred": preds}, index=X_t.index)
    out["recorded"] = rec["raw_panel"].reindex(out.index)
    out["diff"] = out["pred"] - out["recorded"]
    results[day] = out

    cdf = pd.DataFrame(contribs, index=X_t.index, columns=feature_cols + ["BIAS"])
    cdf.to_parquet(SC / f"contribs_{day}.parquet")
    X_t.to_parquet(SC / f"features_transformed_{day}.parquet")
    X_aligned.to_parquet(SC / f"features_raw_{day}.parquet")

    both = out.dropna(subset=["recorded"])
    print(f"\n== {day} ({run_id}) ==")
    print(f"  scored {len(out)} tickers; {len(both)} with recorded values")
    print(f"  reproduction: max|diff|={both['diff'].abs().max():.6f}  "
          f"mean|diff|={both['diff'].abs().mean():.6f}  corr={both['pred'].corr(both['recorded']):.6f}")
    for t in ["META", "NFLX", "FTNT", "APH", "MSFT", "NVDA", "AAPL"]:
        if t in out.index:
            r = out.loc[t]
            rc = "n/a" if pd.isna(r["recorded"]) else f"{r['recorded']:+.4f}"
            print(f"  {t:5s} pred={r['pred']:+.4f} recorded={rc}")
    print(f"  fund medians: { {k: round(v,4) for k,v in fund_medians.items()} }")

pd.concat(results, names=["day"]).to_parquet(SC / "repro_summary.parquet")
print("\nSaved contribs/features per day to scratchpad.")
