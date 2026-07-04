#!/usr/bin/env bash
# log-cost.sh — appends token usage to a run's .token-log and regenerates
# cost.md WHOLESALE from that log on every call.
#
# Usage:
#   log-cost.sh <run_id> <stage_name> <input_tokens> <output_tokens>
#
# Why regenerate instead of patch: the previous version used `sed -i` with `|`
# as both the delimiter and a literal pattern character to update the TOTAL
# row in place. That sed command was malformed, so it silently failed to
# replace anything and every call appended a NEW "| **TOTAL** |" row instead —
# a 5-stage run ended up with 5 TOTAL rows. Regenerating the whole table from
# the append-only `.token-log` on every call has a single writer and is
# idempotent by construction; there is no in-place edit to get wrong.
#
# These figures are HEURISTIC ESTIMATES derived from artifact file sizes
# (chars/4), not real token counts reported by the model. Every number in
# cost.md is labeled "(est)" for exactly this reason — see also memory.md's
# `est-cost:` field (not `cost:`).
#
# Pricing used for the estimate (Claude Sonnet 4 — update if the model changes):
#   Input:  $3.00  per 1M tokens
#   Output: $15.00 per 1M tokens

set -euo pipefail

RUN_ID="${1:?Usage: log-cost.sh <run_id> <stage_name> <input_tokens> <output_tokens>}"
STAGE="${2:?}"
INPUT_TOKENS="${3:?}"
OUTPUT_TOKENS="${4:?}"

ORCHESTRATOR_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_DIR="$ORCHESTRATOR_ROOT/runs/$RUN_ID"
REPORT="$RUN_DIR/report.md"
COST_FILE="$RUN_DIR/cost.md"
TOKENS_FILE="$RUN_DIR/.token-log"

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

INPUT_PRICE_PER_M=3.00
OUTPUT_PRICE_PER_M=15.00

# ── Append this stage's raw estimate to the append-only log ──────────────────
# (Single source of truth — cost.md below is always regenerated from this file,
# never edited in place.)
echo "$STAGE $INPUT_TOKENS $OUTPUT_TOKENS" >> "$TOKENS_FILE"

# ── Note in report.md (a log entry, not a table — no arithmetic to get wrong) ─
STAGE_COST=$(awk -v i="$INPUT_TOKENS" -v o="$OUTPUT_TOKENS" -v ip="$INPUT_PRICE_PER_M" -v op="$OUTPUT_PRICE_PER_M" \
  'BEGIN { printf "%.4f", (i*ip/1000000) + (o*op/1000000) }')
{
  echo ""
  echo "**[$STAGE cost (est) — $TIMESTAMP]** in: ${INPUT_TOKENS} tok (est) · out: ${OUTPUT_TOKENS} tok (est) · stage total (est): \$${STAGE_COST}"
} >> "$REPORT"

# ── Regenerate cost.md wholesale from .token-log (single writer, idempotent) ──
{
  echo "# Cost Summary (heuristic estimate — see note below)"
  echo ""
  echo "| Stage | Input tok (est) | Input \$ (est) | Output tok (est) | Output \$ (est) | Stage total \$ (est) |"
  echo "|-------|------------------|----------------|-------------------|------------------|-----------------------|"
  awk -v ip="$INPUT_PRICE_PER_M" -v op="$OUTPUT_PRICE_PER_M" '
    {
      ti += $2; to += $3;
      ic = $2 * ip / 1000000;
      oc = $3 * op / 1000000;
      printf "| %s | %s | $%.4f | %s | $%.4f | $%.4f |\n", $1, $2, ic, $3, oc, (ic+oc);
    }
    END {
      tic = ti * ip / 1000000;
      toc = to * op / 1000000;
      printf "| **TOTAL** | %s | $%.4f | %s | $%.4f | $%.4f |\n", ti, tic, to, toc, (tic+toc);
    }
  ' "$TOKENS_FILE"
  echo ""
  echo "> Estimated from artifact file sizes (chars/4). Excludes conversation"
  echo "> context, agent reasoning, and retries — real spend is substantially"
  echo "> higher. Use for RELATIVE stage comparison only, never as actual spend."
} > "$COST_FILE"

# ── Console echo for interactive runs ─────────────────────────────────────────
RUN_TOTAL_COST=$(awk -v ip="$INPUT_PRICE_PER_M" -v op="$OUTPUT_PRICE_PER_M" \
  '{ s += ($2*ip/1000000) + ($3*op/1000000) } END { printf "%.4f", s+0 }' "$TOKENS_FILE")
echo ""
echo "  Stage (est): \$${STAGE_COST} | Run total so far (est): \$${RUN_TOTAL_COST}"
echo ""
