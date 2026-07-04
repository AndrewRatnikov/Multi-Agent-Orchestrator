# Critical Fixes Plan

Fixes the four critical issues found in the prototype review (2026-07-04):

1. Sandbox never ran mechanically (`timeout` missing on macOS → exit 127 → LLM self-declared PASS)
2. Reviewer gate gives false confidence (B5 checked but jest-dom slipped through; tests edited after the gate)
3. TS pre-flight broken (`npx --prefix` installed bogus `tsc@2.0.4`; misleading FAIL entries)
4. Cost tracking fiction (chars/4 estimate presented as $; broken sed → duplicate TOTAL rows)

Order matters: F1 → F2 are the trust anchor; F3 removes the false-confidence gate; F4 is independent; F5 proves it all end-to-end. Estimated total effort: ~1 day.

---

## Phase F1 — Portable timeout + honest sandbox failure handling

**Goal:** `run-tests.sh` produces a real exit code on macOS and Linux, and the orchestrator NEVER works around a broken sandbox.

### F1.1 Add a portable timeout wrapper to `run-tests.sh`

Replace the bare `timeout "$TIMEOUT" bash -c "$TEST_CMD"` line with:

```bash
# ── Portable timeout: GNU timeout → gtimeout (brew coreutils) → perl fallback ──
run_with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  else
    # perl is present on every macOS; SIGALRM kills the child, exit 124 mimics GNU timeout
    perl -e '
      my $t = shift @ARGV;
      my $pid = fork;
      if ($pid == 0) { exec @ARGV; exit 127; }
      $SIG{ALRM} = sub { kill "KILL", -$pid; exit 124 };
      alarm $t;
      waitpid $pid, 0;
      exit ($? >> 8);
    ' "$secs" "$@"
  fi
}
```

And call it:

```bash
set +e
run_with_timeout "$TIMEOUT" bash -c "$TEST_CMD" > "$OUTPUT_FILE" 2>&1
EXIT_CODE=$?
set -e
```

Note the `set +e` guard — the script has `set -euo pipefail`, so today a non-zero test exit would kill the script before classification. This is a latent bug; fix it in the same pass. (For the perl fallback, `fork` needs the child in its own process group for `kill -$pid` to work — use `setpgrp` in the child, or accept killing only the direct child for MVP.)

### F1.2 Classify unknown exit codes as ERROR, loudly

After classification, add a fourth branch:

```bash
elif [ "$EXIT_CODE" -eq 127 ] || [ "$EXIT_CODE" -eq 126 ]; then
  RESULT="ERROR"
  log_report "### Result: ERROR (infrastructure)"
  log_report "Exit $EXIT_CODE — command not found / not executable. The sandbox itself is broken."
  log_report "**This is NOT a test result. Do not retry the Coder. Fix the environment.**"
```

### F1.3 Forbid the orchestrator from bypassing the sandbox

Add to `run-orchestration.md` Stage 6 and `resume-orchestration.md` (`--from sandbox`):

```
**If exit code is 3 or any other unlisted code (ERROR):**
STOP immediately. Update state.md: status: failed, pause_reason: sandbox-infrastructure-error.
Tell the user the sandbox environment is broken and show the report entry.
You must NOT manually replicate the sandbox, run tests yourself, or declare
PASS/FAIL by any other means. The sandbox exit code is the only accepted verdict.
```

This single rule prevents the failure mode observed in both real runs.

### F1.4 Manual test matrix (the unticked Phase 4 checklist — actually do it)

Run each case on the real machine and record results in `runs/sandbox-selftest/report.md`:

| Case | Setup | Expected |
|------|-------|----------|
| PASS | trivially passing test | exit 0, `### Result: PASS` |
| FAIL | failing assertion | exit 1, output captured in report |
| TIMEOUT | test with `await new Promise(()=>{})` | exit 2, worktree removed |
| ERROR | temporarily `PATH=/usr/bin` without node | exit 3, "infrastructure" wording |
| No GNU timeout | rename check: force perl fallback branch | same classifications as above |

**Done when:** all five rows verified on macOS, worktree absent after every case (`git worktree list` clean).

---

## Phase F2 — Fix the TypeScript pre-flight

**Goal:** pre-flight uses the repo's own `tsc`, runs at the right time, and never masquerades as a test result.

### F2.1 Move it AFTER `npm install` (Step 5), not before

It cannot work without `node_modules`. Reorder Steps 4 and 5.

### F2.2 Use the sandbox's local tsc, never npx package resolution

```bash
if [ -f "$SANDBOX/node_modules/.bin/tsc" ] && [ -f "$SANDBOX/tsconfig.json" ]; then
  echo "Pre-flight: TypeScript check..."
  if ! (cd "$SANDBOX" && ./node_modules/.bin/tsc --noEmit --skipLibCheck) >"$OUTPUT_FILE" 2>&1; then
    log_report "### Pre-flight: tsc errors (informational — not the test verdict)"
    log_report '```'
    log_report "$(head -40 "$OUTPUT_FILE")"
    log_report '```'
  fi
else
  echo "Pre-flight: skipped (no local tsc)."
fi
```

Key changes: `./node_modules/.bin/tsc` (never `npx --prefix`, which resolved the abandoned `tsc` npm package), `cd` into the sandbox so tsconfig paths resolve, and the report heading says **Pre-flight … informational**, never `### Result: FAIL`. Both existing run reports contain false "Result: FAIL (TypeScript syntax errors)" entries with empty bodies — this eliminates that class of noise.

**Done when:** a run against minima-spend shows either no pre-flight entry or a populated informational one, and the only `### Result:` lines in report.md come from the actual test run.

---

## Phase F3 — Mechanical contract-compliance gate

**Goal:** the checks that are mechanically verifiable stop being LLM checklist items. A script either passes or fails; the LLM reviewer keeps only judgment calls.

### F3.1 Write `.claude/scripts/check-contract.sh`

`Usage: check-contract.sh <run_id> <repo_path>` — exit 0 = clean, exit 1 = violations (printed one per line, machine-readable).

Checks, in order:

1. **data-testid parity (tests → contract).** Extract every `data-testid` queried in `runs/{run}/tests/` (`getByTestId('x')`, `data-testid="x"`), extract every testid declared in `plan.md` § Interface Contract, diff. Any testid used in tests but absent from the contract → violation.
   ```bash
   grep -rhoE "getByTestId\(['\"][^'\"]+" tests/ | sed "s/.*['\"]//" | sort -u > /tmp/used
   grep -oE 'data-testid="[^"]+"' plan.md | sed 's/.*="//;s/"//' | sort -u > /tmp/declared
   comm -23 /tmp/used /tmp/declared   # anything here is a violation
   ```
2. **Package imports exist in the repo (the jest-dom check that failed as B5).** Extract every bare-package import from test files (`import ... from 'pkg'`, ignore relative/alias paths), check each against `dependencies` + `devDependencies` of the **target repo's** `package.json` (read it directly — the digest strips devDependencies, which is exactly why B5 missed jest-dom). Unknown package → violation.
3. **Contract file paths.** Every relative/alias import in tests that points at implementation code must match a `File:` path in the Interface Contract (after alias expansion from tsconfig `paths`). Mismatch → violation.
4. **Banned patterns.** `while\s*\(\s*true`, `fetch(`/`axios.` without a `vi.mock`/`jest.mock` of that module in the same file. Match → violation.
5. **Coder-side check (second mode: `--code`).** Every testid declared in the contract appears in `runs/{run}/code/`; no test file under `tests/` has an mtime newer than the test-reviewer PASS timestamp (detects post-gate test edits — the run 1 violation).

Keep it ~100 lines of grep/sed/comm. Do not try to parse TypeScript properly — false negatives are fine; the LLM reviewer still runs after.

### F3.2 Wire into the pipeline

- `run-orchestration.md` Stage 4: run `check-contract.sh` **first**. If it fails, skip the LLM reviewer entirely, write violations to report.md, route back to Tester with the violation list as feedback (same retry cap). Only if it passes does the LLM reviewer run — its checklist shrinks to B1–B3 (assertion quality) and C (coverage), which genuinely need judgment.
- Stage 5→6 boundary: run `check-contract.sh --code` after the Coder. Testid missing in code → feedback to Coder without burning a sandbox run; post-gate test edit → hard stop, `pause_reason: tests-modified-after-review`.
- Same wiring in `resume-orchestration.md`.
- Update `_test-reviewer-agent.md`: remove A1–A3/B4/B5 (now mechanical), state explicitly "contract compliance was verified by script before you ran."

### F3.3 Regression-test against known history

Recreate run 1's defect: a test importing `@testing-library/jest-dom` against minima-spend's package.json → script must exit 1 naming the package. This exact case slipped through the LLM gate; it's the acceptance test for F3.

**Done when:** the jest-dom regression case fails mechanically, a clean run passes, and the reviewer prompt no longer contains A/B4/B5.

---

## Phase F4 — Honest cost tracking

**Goal:** stop presenting size-based estimates as dollars; fix the broken TOTAL row.

### F4.1 Fix the TOTAL bug by regenerating, not editing

The current `sed -i` uses `|` both as delimiter and inside the pattern → malformed command → cost.md accumulated five TOTAL rows. Don't patch the sed; rewrite `log-cost.sh` to regenerate `cost.md` from `.token-log` on every call:

```bash
{
  echo "# Cost Summary (heuristic estimate — see note)"
  echo ""
  echo "| Stage | Input tok (est) | Output tok (est) | Stage total (est) |"
  echo "|-------|----------------|-----------------|-------------------|"
  awk '{ ti+=$2; to+=$3; printf "| %s | %s | %s | ... |\n", $1, $2, $3 }
       END { printf "| **TOTAL** | %s | %s | ... |\n", ti, to }' "$TOKENS_FILE"
  echo ""
  echo "> Estimated from artifact file sizes (chars/4). Excludes conversation"
  echo "> context, agent reasoning, and retries — real spend is substantially"
  echo "> higher. Use for RELATIVE stage comparison only."
} > "$COST_FILE"
```

Single writer, idempotent, no sed. Keep dollar figures out of the table or mark every one `(est)`.

### F4.2 Relabel everywhere

`run-orchestration.md` Stage 7 and the memory.md run-history line: `cost:` → `est-cost:`. Update the two existing history lines in memory.md.

### F4.3 (Stretch, do only if F1–F3 land) Real usage capture

Claude Code writes session transcripts with per-message `usage` blocks under `~/.claude/projects/<project>/*.jsonl`. Add `log-cost.sh --real <run_id> <stage>`: record the transcript byte-offset at stage start, sum `usage.input_tokens`/`output_tokens` from new lines at stage end. Fragile across CC versions — gate behind a flag, fall back to estimates silently. If this proves painful, skip it; F4.1+F4.2 already make the numbers honest.

**Done when:** cost.md has exactly one TOTAL row after a 5-stage run, and nothing in reports or memory.md presents an estimate as actual spend.

---

## Phase F5 — Zero-intervention validation run

**Goal:** the Phase 7 validation from the original plan, but with a hard rule: if any step requires manual work, the run is a FAIL and the gap becomes a bug.

1. Pick a fresh small task against minima-spend (e.g. "add a MonthSelector dropdown component").
2. Run `/run-orchestration` in a fresh Claude Code session. No manual edits to tests, code, or scripts mid-run.
3. Verify: sandbox exit code is the recorded verdict (grep report.md — no "manually replicated" text allowed); `check-contract.sh` entries appear in the report; cost.md has one TOTAL row.
4. Deliberate failure drills, each as its own resume:
   - inject a wrong assertion into a test → expect mechanical/reviewer catch or sandbox FAIL → Coder retry loop → cap honored
   - inject `await new Promise(()=>{})` → expect TIMEOUT → routed to test-reviewer
5. Record findings in `runs/validation-run-2/report.md`. Any manual intervention = new task, re-run after fixing.

**Done when:** one full PASS run and both failure drills complete with zero human edits, on this machine.

---

## Explicitly out of scope (unchanged from prototype plan)

Plan-reviewer, code-reviewer, memory curator (except: Stage 7 already appends run history — add one line telling the orchestrator to also append any newly discovered gotchas to `## Known gotchas`; it's a one-line change, do it while touching Stage 7 in F4.2), model tiering, CLI wrapper. The CLI wrapper decision should be revisited after F5 with evidence.
