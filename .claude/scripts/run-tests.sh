#!/usr/bin/env bash
# run-tests.sh — sandboxed test runner for the AI orchestration pipeline
#
# Usage:
#   run-tests.sh <repo_path> <run_id> [test_command|-] [timeout_seconds]
#
#   test_command "-" (or empty): read it from runs/{run_id}/repo-digest.md's
#   "## Test command" section. This is the normal way to call it: the command is
#   taken from the file mechanically, never copied by hand.
#
# Output: the full test output goes to runs/{run_id}/sandbox-logs/<timestamp>.log.
# report.md only gets a short summary (PASS) or the tail of the output (FAIL),
# plus the path of the full log.
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
TEST_CMD="${3:--}"
TIMEOUT="${4:-120}"

ORCHESTRATOR_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUN_DIR="$ORCHESTRATOR_ROOT/runs/$RUN_ID"
REPORT="$RUN_DIR/report.md"
SANDBOX="/tmp/orchestrator-sandbox-$RUN_ID"
OUTPUT_FILE="/tmp/orchestrator-output-$RUN_ID.txt"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
LOG_DIR="$RUN_DIR/sandbox-logs"
LOG_FILE="$LOG_DIR/$(date -u +"%Y%m%dT%H%M%SZ").log"
LOG_REL="runs/$RUN_ID/sandbox-logs/$(basename "$LOG_FILE")"

# ── Resolve the test command ──────────────────────────────────────────────────
# "-" means: read it from repo-digest.md. The section is a fenced block; take the
# non-empty lines inside it that aren't fence markers, joined with " && ".
if [ -z "$TEST_CMD" ] || [ "$TEST_CMD" = "-" ]; then
  DIGEST="$RUN_DIR/repo-digest.md"
  if [ ! -f "$DIGEST" ]; then
    echo "ERROR: $DIGEST not found, so there is no Test command to run."
    exit 3
  fi
  TEST_CMD=$(awk '/^## Test command/{f=1;next} /^## /{f=0} f' "$DIGEST" \
    | grep -vE '^[[:space:]]*(```|~~~)' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    | grep -v '^$' | awk 'BEGIN{ORS=""} NR>1{print " && "} {print}' || true)
fi
# Refuse anything that is obviously not a command, instead of "running" it and
# reporting a FAIL that would cost the Coder a retry.
case "$TEST_CMD" in
  ""|UNKNOWN|'```'*|'~~~'*)
    echo "ERROR: invalid Test command: '$TEST_CMD'. Fix repo-digest.md's '## Test command' section."
    exit 3 ;;
esac

log_report() {
  echo "$1" >> "$REPORT"
}

# Lines worth keeping in report.md from a passing run: the runners' own summaries.
summary_lines() {
  grep -E '^[[:space:]]*(Test Files|Tests|Test Suites|Snapshots|Ran all test suites|# (pass|fail|tests))' "$1" \
    | grep -v '^[[:space:]]*Tests[[:space:]]*$' | head -20
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
  # Playwright (if present as a devDependency) downloads browser binaries on
  # install by default — irrelevant to most test runs and slow/network-heavy
  # in a disposable sandbox, so skip it.
  export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
  if [ -f "$SANDBOX/pnpm-workspace.yaml" ] || [ -f "$SANDBOX/pnpm-lock.yaml" ]; then
    (cd "$SANDBOX" && pnpm install --silent) \
      || { echo "ERROR: pnpm install failed."; log_report "**ERROR:** pnpm install failed in sandbox."; exit 3; }
  else
    npm install --prefix "$SANDBOX" --silent 2>&1 | tail -5 \
      || { echo "ERROR: npm install failed."; log_report "**ERROR:** npm install failed in sandbox."; exit 3; }
  fi
  echo "Dependencies installed."
fi

# ── Step 4b: Build workspace library packages (e.g. @scope/shared) ───────────
# A workspace member's build output (e.g. packages/shared/dist) is typically
# git-ignored and only ever built locally in the real repo — never committed —
# so a fresh disposable worktree has source but no dist/, and any package.json
# "main"/"exports" pointing at dist/ fails to resolve at test time even though
# nothing about the app code is wrong. Build every workspace member NOT under
# apps/ (pnpm's recursive commands run in topological order by default, so
# libs build before whatever imports them) — apps are excluded because they're
# exercised directly by the test command below, not by a prerequisite build,
# and at least one app in this class of repo (a Nest/Prisma API) can fail a
# full `build` for reasons unrelated to this pipeline (e.g. `prisma generate`
# never having run in the sandbox) without that mattering to the tests we
# actually run. Skipped entirely for non-workspace repos.
if [ -f "$SANDBOX/pnpm-workspace.yaml" ]; then
  echo "Building workspace library packages (topological, --if-present, apps/ excluded)..."
  if ! (cd "$SANDBOX" && pnpm --filter '!./apps/**' -r --if-present run build) >"$OUTPUT_FILE" 2>&1; then
    echo "ERROR: workspace package build failed."
    cat "$OUTPUT_FILE"
    log_report "**ERROR:** workspace package build failed in sandbox (\`pnpm --filter '!./apps/**' -r --if-present run build\`)."
    log_report ""
    log_report '```'
    log_report "$(tail -40 "$OUTPUT_FILE")"
    log_report '```'
    exit 3
  fi
  echo "Workspace library packages built."
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
mkdir -p "$LOG_DIR"
{ echo "\$ $TEST_CMD"; echo; cat "$OUTPUT_FILE"; } > "$LOG_FILE"

# ── Step 7: Classify result ───────────────────────────────────────────────────
if [ "$EXIT_CODE" -eq 124 ]; then
  # timeout(1) exits 124 when the process is killed by the time limit
  RESULT="TIMEOUT"
  echo ""
  echo "✗ TIMEOUT — tests did not complete within ${TIMEOUT}s"
  echo "  Likely cause: a test is awaiting an unresolved promise or hitting real network."
  echo "  Recommend: resume from test-reviewer (not coder) — the test itself may be broken."

  log_report "### Result: TIMEOUT"
  log_report "Full output: \`$LOG_REL\`"
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
  SUMMARY=$(summary_lines "$OUTPUT_FILE")
  [ -z "$SUMMARY" ] && SUMMARY=$(tail -10 "$OUTPUT_FILE")
  log_report '```'
  log_report "$SUMMARY"
  log_report '```'
  log_report "Full output: \`$LOG_REL\`"

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
  log_report "$(tail -40 "$OUTPUT_FILE")"
  log_report '```'
  log_report "Full output: \`$LOG_REL\`"

else
  RESULT="FAIL"
  echo ""
  echo "✗ FAIL — tests failed (exit code $EXIT_CODE)"
  echo "  Likely cause: implementation logic is wrong."
  echo "  Recommend: resume from coder with this output as feedback."

  log_report "### Result: FAIL (exit code $EXIT_CODE)"
  log_report ""
  log_report "**Recommended action:** re-run the Coder with this failure as \`FEEDBACK\`, and point it at the full log."
  log_report ""
  log_report "Summary:"
  log_report '```'
  log_report "$(summary_lines "$OUTPUT_FILE")"
  log_report '```'
  log_report "Last 120 lines (full output: \`$LOG_REL\`):"
  log_report '```'
  log_report "$(tail -120 "$OUTPUT_FILE")"
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
