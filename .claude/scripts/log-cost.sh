#!/usr/bin/env bash
# log-cost.sh — appends token usage + estimated cost to a run's report.md
#
# Usage:
#   log-cost.sh <run_id> <stage_name> <input_tokens> <output_tokens>
#
# Pricing (Claude Sonnet 4 — update if model changes):
#   Input:  $3.00 per 1M tokens
#   Output: $15.00 per 1M tokens

set -euo pipefail

RUN_ID="${1:?Usage: log-cost.sh <run_id> <stage_name> <input_tokens> <output_tokens>}"
STAGE="${2:?}"
INPUT_TOKENS="${3:?}"
OUTPUT_TOKENS="${4:?}"

ORCHESTRATOR_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPORT="$ORCHESTRATOR_ROOT/runs/$RUN_ID/report.md"
COST_FILE="$ORCHESTRATOR_ROOT/runs/$RUN_ID/cost.md"

# Pricing per token (in millionths of a dollar for integer arithmetic)
INPUT_PRICE_PER_M=3000    # $3.00 per 1M = 3000 millicents per 1M = 0.000003 per token
OUTPUT_PRICE_PER_M=15000  # $15.00 per 1M

# Calculate cost in millicents (avoid floating point in bash)
INPUT_COST_MC=$(( INPUT_TOKENS * INPUT_PRICE_PER_M / 1000000 ))
OUTPUT_COST_MC=$(( OUTPUT_TOKENS * OUTPUT_PRICE_PER_M / 1000000 ))
TOTAL_COST_MC=$(( INPUT_COST_MC + OUTPUT_COST_MC ))

# Format as dollars with 4 decimal places
format_cost() {
  local mc=$1
  local dollars=$(( mc / 1000 ))
  local cents=$(( mc % 1000 ))
  printf '$%d.%04d' "$dollars" "$cents"
}

INPUT_COST_STR=$(format_cost $INPUT_COST_MC)
OUTPUT_COST_STR=$(format_cost $OUTPUT_COST_MC)
TOTAL_COST_STR=$(format_cost $TOTAL_COST_MC)

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Format token counts with commas
fmt_tokens() {
  printf "%'d" "$1" 2>/dev/null || echo "$1"
}

LINE="| $STAGE | $(fmt_tokens $INPUT_TOKENS) | $INPUT_COST_STR | $(fmt_tokens $OUTPUT_TOKENS) | $OUTPUT_COST_STR | $TOTAL_COST_STR |"

# Append to report.md
{
  echo ""
  echo "**[$STAGE cost — $TIMESTAMP]** in: $(fmt_tokens $INPUT_TOKENS) tokens ($INPUT_COST_STR) · out: $(fmt_tokens $OUTPUT_TOKENS) tokens ($OUTPUT_COST_STR) · stage total: $TOTAL_COST_STR"
} >> "$REPORT"

# Maintain a running cost summary in cost.md
if [ ! -f "$COST_FILE" ]; then
  cat > "$COST_FILE" << 'EOF'
# Cost Summary

| Stage | Input tokens | Input cost | Output tokens | Output cost | Stage total |
|-------|-------------|------------|--------------|-------------|-------------|
EOF
fi

echo "$LINE" >> "$COST_FILE"

# Compute running total from all lines in cost.md
# (Simple approach: re-sum from the file each time)
TOTAL_INPUT=0
TOTAL_OUTPUT=0

# Re-read all previous stage costs from report lines
# (We track raw tokens in a hidden file for accurate summing)
TOKENS_FILE="$ORCHESTRATOR_ROOT/runs/$RUN_ID/.token-log"
echo "$STAGE $INPUT_TOKENS $OUTPUT_TOKENS" >> "$TOKENS_FILE"

while read -r _ in_t out_t; do
  TOTAL_INPUT=$(( TOTAL_INPUT + in_t ))
  TOTAL_OUTPUT=$(( TOTAL_OUTPUT + out_t ))
done < "$TOKENS_FILE"

TOTAL_INPUT_COST_MC=$(( TOTAL_INPUT * INPUT_PRICE_PER_M / 1000000 ))
TOTAL_OUTPUT_COST_MC=$(( TOTAL_OUTPUT * OUTPUT_PRICE_PER_M / 1000000 ))
TOTAL_ALL_MC=$(( TOTAL_INPUT_COST_MC + TOTAL_OUTPUT_COST_MC ))

echo ""
echo "  Stage: $TOTAL_COST_STR | Run total so far: $(format_cost $TOTAL_ALL_MC)"
echo ""

# Update running total line in cost.md (replace or append)
if grep -q "^| **TOTAL**" "$COST_FILE" 2>/dev/null; then
  sed -i.bak "s|^| \*\*TOTAL\*\*.*|$(printf '| **TOTAL** | %s | %s | %s | %s | %s |' "$(fmt_tokens $TOTAL_INPUT)" "$(format_cost $TOTAL_INPUT_COST_MC)" "$(fmt_tokens $TOTAL_OUTPUT)" "$(format_cost $TOTAL_OUTPUT_COST_MC)" "$(format_cost $TOTAL_ALL_MC)")|" "$COST_FILE"
  rm -f "$COST_FILE.bak"
else
  echo "| **TOTAL** | $(fmt_tokens $TOTAL_INPUT) | $(format_cost $TOTAL_INPUT_COST_MC) | $(fmt_tokens $TOTAL_OUTPUT) | $(format_cost $TOTAL_OUTPUT_COST_MC) | $(format_cost $TOTAL_ALL_MC) |" >> "$COST_FILE"
fi
