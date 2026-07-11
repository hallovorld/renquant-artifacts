#!/usr/bin/env bash
set -uo pipefail
REPO=/Users/renhao/git/github/RenQuant
PY="$REPO/.venv/bin/python"
SCRATCH=/private/tmp/claude-502/-Users-renhao-git-github-renquant-orchestrator/2244bd05-9699-4a07-8836-2b6d9e43ca5f/scratchpad/xstock_pilot
source "$REPO/scripts/subrepo_env.sh"
renquant_load_subrepo_env "$REPO"
SUBREPO_ROOT="$(renquant_subrepo_root "$REPO" "$(dirname "$REPO")")"
export RENQUANT_REPO_ROOT="$REPO" RENQUANT_SUBREPO_ROOT="$SUBREPO_ROOT"
export PYTHONPATH="$(renquant_subrepo_pythonpath "$SUBREPO_ROOT" renquant-orchestrator renquant-common renquant-base-data renquant-artifacts renquant-model renquant-pipeline renquant-execution renquant-strategy-104 renquant-backtesting)"
for arm in base xstock; do
  OUT="$SCRATCH/${arm}_s45"
  rm -rf "$OUT"; mkdir -p "$OUT"
  EXTRA=""
  [ "$arm" = "xstock" ] && EXTRA="--cross-stock-attn"
  echo "=== PILOT RUN arm=$arm seed=45 start $(date -u +%H:%M:%SZ) ==="
  "$PY" -m renquant_model_patchtst.hf_trainer \
    --cut all --train-cutoff 2026-03-30 \
    --epochs 5 --device mps --seed 45 \
    --label fwd_60d_excess \
    --lr 1e-4 --weight-decay 0.3 --seq-len 24 --early-stopping-patience 2 \
    --save-model --output-dir "$OUT" \
    --dataset "$REPO/data/transformer_v4_wl200_clean.parquet" \
    --spy-path "$REPO/data/ohlcv/SPY/1d.parquet" \
    --strategy-config /Users/renhao/git/github/renquant-strategy-104/configs/strategy_config.shadow.json \
    $EXTRA > "$OUT/train.log" 2>&1
  rc=$?
  bvic=$("$PY" -c "import json,glob; f=glob.glob('$OUT/*summary.json'); print(json.load(open(f[0]))['best_val_ic'] if f else 'NO_SUMMARY')" 2>/dev/null || echo ERR)
  echo "=== PILOT RUN arm=$arm seed=45 done rc=$rc best_val_ic=$bvic $(date -u +%H:%M:%SZ) ==="
  [ $rc -ne 0 ] && { echo "PILOT_RUN_FAILED arm=$arm seed=45 rc=$rc"; tail -5 "$OUT/train.log"; }
done
echo "PILOT_S45_DONE"
