#!/usr/bin/env bash
# run-tests.sh — sandboxed test runner for the AI orchestration pipeline
#
# Usage:
#   run-tests.sh <repo_path> <run_id> <test_command> [timeout_seconds]
#
# What it does:
#   1. Creates a disposable git worktree at /tmp/orchestrator-sandbox-{run_id}
#   2. Copies generated code + tests from runs/{run_id}/ into the worktree
#   3. Runs the test command with a two-layer timeout (runner + hard subprocess cap)
#   4. Classifies result: PASS | FAIL | TIMEOUT | ERROR
#   5. Appends result + output to runs/{run_id}/report.md
#   6. Cleans up the worktree unconditionally
#
# Exit codes:
#   0 = PASS
#   1 = FAIL
#   2 = TIMEOUT
#   3 = ERROR (setup failed before tests could run, or infrastructure broken — exit 126/127)

set -euo pipefail

# ── Args ─────────────────────────────────────────────────────────────────────
REPO_PATH="${1:?Usage: run-tests.sh <repo_path> <run_id> <test_command> [timeout_seconds]}"
RUN_ID="${2:?Usage: run-tests.sh <repo_path> <run_id> <test_command> [timeout_seconds]}"
TEST_CMD="${3:?Usage: run-tests.sh <repo_path> <run_id> <test_command> [timeout_seconds]}"
TIMEOUT="${4:-120}"

ORCHESTRATOR_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_DIR="$ORCHESTRATOR_ROOT/runs/$RUN_ID"
REPORT="$RUN_DIR/report.md"
SANDBOX="/tmp/orchestrator-sandbox-$RUN_ID"
OUTPUT_FILE="/tmp/orchestrator-output-$RUN_ID.txt"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

log_report() {
  echo "$1" >> "$REPORT"
}

# ── Portable timeout: GNU timeout → gtimeout (brew coreutils) → perl fallback ──
run_with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  else
    # perl is present on every macOS; SIGALRM kills the child, exit 124 mimics GNU timeout
    # setpgrp puts the child in its own process group so kill(-$pid) reaches the
    # whole tree (bash -c + the test runner it spawns), not just the direct child —
    # without it the kill targets a nonexistent group and the hung process leaks.
    perl -e '
      my $t = shift @ARGV;
      my $pid = fork;
      if ($pid == 0) { setpgrp(0, 0); exec @ARGV; exit 127; }
      $SIG{ALRM} = sub { kill "KILL", -$pid; exit 124 };
      alarm $t;
      waitpid $pid, 0;
      exit ($? >> 8);
    ' "$secs" "$@"
  fi
}

cleanup() {
  if [ -d "$SANDBOX" ]; then
    # Remove the worktree (force in case of dirty state)
    git -C "$REPO_PATH" worktree remove "$SANDBOX" --force 2>/dev/null || rm -rf "$SANDBOX"
  fi
  rm -f "$OUTPUT_FILE"
}

# Always clean up, even on unexpected exits
trap cleanup EXIT

echo ""
echo "── Test Sandbox ─────────────────────────────────────────────────────────"
echo "Run:     $RUN_ID"
echo "Repo:    $REPO_PATH"
echo "Command: $TEST_CMD"
echo "Timeout: ${TIMEOUT}s"
echo "─────────────────────────────────────────────────────────────────────────"

log_report ""
log_report "---"
log_report "## Test sandbox run — $TIMESTAMP"
log_report ""
log_report "- Command: \`$TEST_CMD\`"
log_report "- Timeout: ${TIMEOUT}s"
log_report ""

# ── Step 1: Verify inputs ─────────────────────────────────────────────────────
if [ ! -d "$REPO_PATH/.git" ]; then
  echo "ERROR: $REPO_PATH is not a git repository."
  log_report "**ERROR:** repo path is not a git repository: $REPO_PATH"
  exit 3
fi

if [ ! -d "$RUN_DIR/code" ]; then
  echo "ERROR: runs/$RUN_ID/code/ not found — Coder agent has not run yet."
  log_report "**ERROR:** \`runs/$RUN_ID/code/\` not found."
  exit 3
fi

if [ ! -d "$RUN_DIR/tests" ]; then
  echo "ERROR: runs/$RUN_ID/tests/ not found — Tester agent has not run yet."
  log_report "**ERROR:** \`runs/$RUN_ID/tests/\` not found."
  exit 3
fi

# ── Step 2: Create git worktree ───────────────────────────────────────────────
echo "Creating sandbox worktree at $SANDBOX..."

BRANCH="orchestrator-sandbox-$RUN_ID"
git -C "$REPO_PATH" worktree add "$SANDBOX" HEAD --detach 2>&1 \
  || { echo "ERROR: Failed to create git worktree."; log_report "**ERROR:** git worktree creation failed."; exit 3; }

echo "Worktree created."

# ── Step 3: Copy generated code + tests into worktree ────────────────────────
echo "Copying generated code into sandbox..."
# Code files mirror the repo structure from root (e.g. code/src/components/Foo.tsx → sandbox/src/components/Foo.tsx)
cp -r "$RUN_DIR/code/." "$SANDBOX/"

echo "Copying test files into sandbox..."
# Test files also mirror the repo structure
cp -r "$RUN_DIR/tests/." "$SANDBOX/"

# ── Step 4: Install dependencies if node_modules is missing ───────────────────
if [ ! -d "$SANDBOX/node_modules" ] && [ -f "$SANDBOX/package.json" ]; then
  echo "Installing dependencies (node_modules not present in worktree)..."
  npm install --prefix "$SANDBOX" --silent 2>&1 | tail -5 \
    || { echo "ERROR: npm install failed."; log_report "**ERROR:** npm install failed in sandbox."; exit 3; }
  echo "Dependencies installed."
fi

# ── Step 5: Pre-flight — TypeScript syntax check (fast, no execution) ─────────
# Runs AFTER npm install so node_modules exists. Uses the sandbox's own local
# tsc only — never `npx`, which resolves and installs an arbitrary `tsc` package
# from the registry if the repo doesn't have one (silently wrong compiler).
# This is informational only: it is never the test verdict, never `### Result:`.
if [ -f "$SANDBOX/node_modules/.bin/tsc" ] && [ -f "$SANDBOX/tsconfig.json" ]; then
  echo "Pre-flight: TypeScript check..."
  if ! (cd "$SANDBOX" && ./node_modules/.bin/tsc --noEmit --skipLibCheck) >"$OUTPUT_FILE" 2>&1; then
    TS_ERRORS=$(cat "$OUTPUT_FILE")
    echo "Pre-flight: tsc reported errors (informational — not the test verdict)."
    echo "$TS_ERRORS"
    log_report "### Pre-flight: tsc errors (informational — not the test verdict)"
    log_report ""
    log_report '```'
    log_report "$(head -40 "$OUTPUT_FILE")"
    log_report '```'
    # Don't exit — the real test verdict comes only from Step 6 below.
  else
    echo "Pre-flight: TypeScript OK."
  fi
else
  echo "Pre-flight: skipped (no local tsc)."
fi

# ── Step 6: Run the tests with two-layer timeout ──────────────────────────────
echo "Running: $TEST_CMD (timeout: ${TIMEOUT}s)"
echo ""

# Layer 1 (inner): test runner's own timeout flags are set by the test command itself
# Layer 2 (outer): hard subprocess cap — catches hangs the runner doesn't catch
cd "$SANDBOX"
set +e
run_with_timeout "$TIMEOUT" bash -c "$TEST_CMD" > "$OUTPUT_FILE" 2>&1
EXIT_CODE=$?
set -e

TEST_OUTPUT=$(cat "$OUTPUT_FILE")

# ── Step 7: Classify result ───────────────────────────────────────────────────
if [ "$EXIT_CODE" -eq 124 ]; then
  # timeout(1) exits 124 when the process is killed by the time limit
  RESULT="TIMEOUT"
  echo ""
  echo "✗ TIMEOUT — tests did not complete within ${TIMEOUT}s"
  echo "  Likely cause: a test is awaiting an unresolved promise or hitting real network."
  echo "  Recommend: resume from test-reviewer (not coder) — the test itself may be broken."

  log_report "### Result: TIMEOUT"
  log_report ""
  log_report "Tests did not complete within ${TIMEOUT}s."
  log_report "**Recommended action:** resume from \`test-reviewer\` — the hang is likely in the test, not the implementation."
  log_report ""
  log_report "Last output before timeout:"
  log_report '```'
  log_report "$(tail -30 "$OUTPUT_FILE")"
  log_report '```'

elif [ "$EXIT_CODE" -eq 0 ]; then
  RESULT="PASS"
  echo ""
  echo "✓ PASS — all tests passed"

  log_report "### Result: PASS"
  log_report ""
  log_report '```'
  log_report "$TEST_OUTPUT"
  log_report '```'

elif [ "$EXIT_CODE" -eq 127 ] || [ "$EXIT_CODE" -eq 126 ]; then
  RESULT="ERROR"
  echo ""
  echo "✗ ERROR — sandbox infrastructure is broken (exit code $EXIT_CODE)"
  echo "  This is NOT a test result. Do not retry the Coder. Fix the environment."

  log_report "### Result: ERROR (infrastructure)"
  log_report ""
  log_report "Exit $EXIT_CODE — command not found / not executable. The sandbox itself is broken."
  log_report "**This is NOT a test result. Do not retry the Coder. Fix the environment.**"
  log_report ""
  log_report '```'
  log_report "$TEST_OUTPUT"
  log_report '```'

else
  RESULT="FAIL"
  echo ""
  echo "✗ FAIL — tests failed (exit code $EXIT_CODE)"
  echo "  Likely cause: implementation logic is wrong."
  echo "  Recommend: resume from coder with this output as feedback."

  log_report "### Result: FAIL (exit code $EXIT_CODE)"
  log_report ""
  log_report "**Recommended action:** resume from \`coder\` with the output below as \`--feedback\`."
  log_report ""
  log_report '```'
  log_report "$TEST_OUTPUT"
  log_report '```'
fi

echo ""
echo "Result written to $REPORT"

# ── Step 8: Write result tag to state.md ──────────────────────────────────────
STATE_FILE="$RUN_DIR/state.md"
if [ -f "$STATE_FILE" ]; then
  # Update last_artifact and timestamp; caller (run-orchestration) handles step/status update
  sed -i.bak "s|^last_artifact:.*|last_artifact: runs/$RUN_ID/code/|" "$STATE_FILE"
  sed -i.bak "s|^timestamp:.*|timestamp: $TIMESTAMP|" "$STATE_FILE"
  rm -f "$STATE_FILE.bak"
fi

# Cleanup happens via trap
echo "Sandbox cleaned up."

# Exit with the classified code so run-orchestration can branch on it
case "$RESULT" in
  PASS)    exit 0 ;;
  FAIL)    exit 1 ;;
  TIMEOUT) exit 2 ;;
  *)       exit 3 ;;
esac
