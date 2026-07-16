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
# `tokens:` field.
#
# No dollar pricing — this tracks token counts only.

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

# ── Append this stage's raw estimate to the append-only log ──────────────────
# (Single source of truth — cost.md below is always regenerated from this file,
# never edited in place.)
echo "$STAGE $INPUT_TOKENS $OUTPUT_TOKENS" >> "$TOKENS_FILE"

STAGE_TOTAL=$(( INPUT_TOKENS + OUTPUT_TOKENS ))

# ── Note in report.md (a log entry, not a table — no arithmetic to get wrong) ─
{
  echo ""
  echo "**[$STAGE tokens (est) — $TIMESTAMP]** in: ${INPUT_TOKENS} tok (est) · out: ${OUTPUT_TOKENS} tok (est) · stage total (est): ${STAGE_TOTAL} tok"
} >> "$REPORT"

# ── Regenerate cost.md wholesale from .token-log (single writer, idempotent) ──
{
  echo "# Token Usage Summary (heuristic estimate — see note below)"
  echo ""
  echo "| Stage | Input tok (est) | Output tok (est) | Stage total tok (est) |"
  echo "|-------|------------------|-------------------|------------------------|"
  awk '
    {
      ti += $2; to += $3;
      printf "| %s | %s | %s | %s |\n", $1, $2, $3, ($2+$3);
    }
    END {
      printf "| **TOTAL** | %s | %s | %s |\n", ti, to, (ti+to);
    }
  ' "$TOKENS_FILE"
  echo ""
  echo "> Estimated from artifact file sizes (chars/4). Excludes conversation"
  echo "> context, agent reasoning, and retries — real usage is substantially"
  echo "> higher. Use for RELATIVE stage comparison only, never as an exact count."
} > "$COST_FILE"

# ── Console echo for interactive runs ─────────────────────────────────────────
RUN_TOTAL_TOKENS=$(awk '{ s += $2 + $3 } END { print s+0 }' "$TOKENS_FILE")
echo ""
echo "  Stage (est): ${STAGE_TOTAL} tok | Run total so far (est): ${RUN_TOTAL_TOKENS} tok"
echo ""
