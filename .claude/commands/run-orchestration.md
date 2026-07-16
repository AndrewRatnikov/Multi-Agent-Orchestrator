# Run Orchestration Pipeline

You are the master orchestrator for the AI dev pipeline. Your job is to run a spec-driven, test-first pipeline that turns an idea into a tested, validated implementation.

You sequence the following stages in order, reading each agent's instruction file and executing it inline. You do not skip steps, invent shortcuts, or call external services — you follow the stage instructions exactly.

## Arguments

```
$ARGUMENTS
```

Parse `$ARGUMENTS` as:
- Everything before `--repo` is the **task description** (TASK)
- `--repo <path>` is the **target repository path** (REPO), defaults to `$(pwd)`

If TASK is empty, ask the user what they want to build and wait for their answer.

Verify REPO exists and has a `package.json` or `pyproject.toml`. If not, tell the user and stop.

Resolve REPO to an absolute path:
```bash
REPO=$(cd "{REPO}" && pwd)
echo "$REPO"
```

Determine ORCHESTRATOR_ROOT — the absolute path of the directory containing this `.claude/` folder:
```bash
ORCHESTRATOR_ROOT=$(cd "$(dirname "$(dirname "$0")")" && pwd)
echo "$ORCHESTRATOR_ROOT"
```

---

## STAGE 0 — Setup

### Claim a branch in {REPO}

Every commit this pipeline makes from here on lands directly in {REPO}, one commit
per plan step, as the work happens — not copied in at the end. That means {REPO}
must be clean before anything starts.

```bash
cd "{REPO}"
DIRTY=$(git status --porcelain)
```

**If `$DIRTY` is non-empty:** STOP. Tell the user:
"{REPO} has uncommitted changes. This pipeline commits directly to a new branch in
your repo as it works, so it needs a clean tree to start from. Commit or stash your
changes, then re-run."
Do not create a run folder. Do not proceed any further.

**If clean:**
```bash
RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
ORIGINAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
BRANCH="orchestrator/$RUN_ID"
git checkout -B "$BRANCH"
cd "$ORCHESTRATOR_ROOT"
echo "$RUN_ID / $BRANCH (from $ORIGINAL_BRANCH)"
```

{REPO} is now checked out on `{BRANCH}` and stays checked out on it for the rest of
the run — every later stage commits into it directly, in place. Nothing is ever
force-pushed or rewritten upstream, and `{ORIGINAL_BRANCH}` is never touched.

### Create run folder

```bash
mkdir -p "$ORCHESTRATOR_ROOT/runs/$RUN_ID/archive"
echo "$RUN_ID"
```

### Write state.md

Write `runs/{RUN_ID}/state.md`:

```
run_id: {RUN_ID}
task: {TASK}
repo: {REPO}
original_branch: {ORIGINAL_BRANCH}
target_branch: {BRANCH}
step: init
status: running
timestamp: {TIMESTAMP}
last_artifact:
pause_reason:
retry_count: 0
```

### Write report.md

Write `runs/{RUN_ID}/report.md`:

```
# Run Report: {RUN_ID}

**Task:** {TASK}
**Repo:** {REPO}
**Branch:** {BRANCH} (from {ORIGINAL_BRANCH})
**Started:** {TIMESTAMP}

---

## Stage log
```

### Generate repo digest

```bash
bash .claude/scripts/repo-digest.sh "{REPO}" "{RUN_ID}" "{TASK}"
```

If the digest warns it is large (>2000 tokens), trim the file list section before continuing.

Tell the user: "Run `{RUN_ID}` started on branch `{BRANCH}` in {REPO}. Repo digest ready. Starting pipeline..."

---

## STAGE 1 — Product Agent

Update `runs/{RUN_ID}/state.md`: set `step: product`, `status: running`.

Read the file `.claude/commands/_product-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

After the Product agent finishes, **log cost**:
```bash
# Estimate: input = agent prompt + task description; output = prd.md
INPUT_T=$(( ( $(wc -c < .claude/commands/_product-agent.md) + ${#TASK} ) / 4 ))
OUTPUT_T=$(( $(wc -c < runs/{RUN_ID}/prd.md 2>/dev/null || echo 400) / 4 ))
bash .claude/scripts/log-cost.sh "{RUN_ID}" "product-agent" "$INPUT_T" "$OUTPUT_T"
```

Read `runs/{RUN_ID}/state.md`.

- If `status: paused` → Stop. Tell the user:
  "Pipeline paused at **product** stage. Answer the questions above, then run:
  `/resume-orchestration {RUN_ID} --from product`"
  Do not continue.

- If `status: running` and `prd.md` exists → continue to Stage 2.

---

## STAGE 2 — Architect Agent

Update `runs/{RUN_ID}/state.md`: set `step: architect`, `status: running`.

Read the file `.claude/commands/_architect-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

After the Architect agent finishes, **log cost**:
```bash
# Estimate: input = agent prompt + prd.md + repo-digest.md; output = plan.md
INPUT_T=$(( ( $(wc -c < .claude/commands/_architect-agent.md) + $(wc -c < runs/{RUN_ID}/prd.md) + $(wc -c < runs/{RUN_ID}/repo-digest.md) ) / 4 ))
OUTPUT_T=$(( $(wc -c < runs/{RUN_ID}/plan.md 2>/dev/null || echo 800) / 4 ))
bash .claude/scripts/log-cost.sh "{RUN_ID}" "architect-agent" "$INPUT_T" "$OUTPUT_T"
```

Read `runs/{RUN_ID}/state.md`.

- If `status: paused` → Stop. Tell the user:
  "Pipeline paused at **architect** stage. Answer the questions above, then run:
  `/resume-orchestration {RUN_ID} --from architect`"
  Do not continue.

- If `status: running` and `plan.md` exists → continue to Stage 3.

---

## STAGE 3 — Tester Agent

Update `runs/{RUN_ID}/state.md`: set `step: tester`, `status: running`, `retry_count: 0`.
**Exception:** if you are re-entering this stage as a retry (from Stage 4a or 4b), do NOT reset `retry_count` — it must keep the value the retry logic just incremented, or the retry cap can never trip.

Read the file `.claude/commands/_tester-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

### Commit the tests to {REPO}

Tests are the first thing that lands in the target repo — written and committed
before any implementation exists, directly on `{BRANCH}`:

```bash
cp -r "runs/{RUN_ID}/tests/." "{REPO}/"
cd "{REPO}"
git add -A
```

If nothing is staged (`git diff --cached --quiet` exits 0 — only happens if a
retry produced byte-identical tests), skip the commit. Otherwise, read `retry_count`
from `runs/{RUN_ID}/state.md`: `0` means this is the first attempt, `>0` means a
fix pass triggered by Stage 4a/4b below.

```bash
git commit -m "{MSG}

Run: {RUN_ID}"
cd "$ORCHESTRATOR_ROOT"
```

Where `{MSG}` is:
- `retry_count` is `0`: `test: {TASK}` (plus a short body: "Written test-first, before any implementation. Not yet reviewed — subject to the mechanical contract gate and the test-reviewer.")
- `retry_count` is `>0`: `fix tests: {TASK} (retry {retry_count}/2)`

After the Tester agent finishes, **log cost**:
```bash
# Estimate: input = agent prompt + plan.md; output = all test files combined
INPUT_T=$(( ( $(wc -c < .claude/commands/_tester-agent.md) + $(wc -c < runs/{RUN_ID}/plan.md) ) / 4 ))
OUTPUT_T=$(( $(find runs/{RUN_ID}/tests -type f 2>/dev/null | xargs wc -c 2>/dev/null | tail -1 | awk '{print $1}' || echo 600) / 4 ))
bash .claude/scripts/log-cost.sh "{RUN_ID}" "tester-agent" "$INPUT_T" "$OUTPUT_T"
```

Continue to Stage 4.

---

## STAGE 4 — Contract Gate + Test-Reviewer

Update `runs/{RUN_ID}/state.md`: set `step: test-reviewer`, `status: running`.

### Stage 4a — Mechanical contract check (runs first, no LLM, no cost)

```bash
bash .claude/scripts/check-contract.sh "{RUN_ID}" "{REPO}"
CONTRACT_EXIT=$?
```

**If `CONTRACT_EXIT` is non-zero (violations found):**

Do **not** run the LLM Test-Reviewer — a mechanical violation is not a judgment call. Append the script's output (one violation per line) to `runs/{RUN_ID}/report.md` under:
```
[check-contract] FAIL — {N} violation(s), returning to Tester (retry {N}/2)

{script output, verbatim}
```

Read `retry_count` from state.md.

- If `retry_count >= 2` → Stop. Tell the user:
  "Contract check retry cap reached (2/2). Human intervention required.
  Review `runs/{RUN_ID}/report.md` for the violations, fix the tests manually or run:
  `/resume-orchestration {RUN_ID} --from tester`
  Partial progress is already committed on `{BRANCH}` in {REPO} if you want to inspect it directly."
  Do not continue.

- If `retry_count < 2` → increment `retry_count` in state.md. Tell the user:
  "Mechanical contract check failed. Retrying Tester with violations as feedback (attempt {retry_count}/2)..."

  Archive the current tests:
  ```bash
  cp -r runs/{RUN_ID}/tests runs/{RUN_ID}/archive/tests_v{retry_count}
  ```

  Re-run Stage 3 (Tester) **without resetting retry_count** (see Stage 3 exception), injecting the violation list:
  "The mechanical contract check failed with these violations: {VIOLATIONS}. Fix every one before writing the new tests — these are not style suggestions, they are exact name/path/package mismatches."

  Then re-run Stage 4 from Stage 4a.

**If `CONTRACT_EXIT` is 0 (clean):** continue to Stage 4b.

### Stage 4b — LLM Test-Reviewer (judgment calls only: assertion quality, coverage)

Read the file `.claude/commands/_test-reviewer-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

After the Test-Reviewer finishes, **log cost**:
```bash
INPUT_T=$(( ( $(wc -c < .claude/commands/_test-reviewer-agent.md) + $(find runs/{RUN_ID}/tests -type f 2>/dev/null | xargs wc -c 2>/dev/null | tail -1 | awk '{print $1}' || echo 600) + $(wc -c < runs/{RUN_ID}/plan.md) ) / 4 ))
OUTPUT_T=300  # reviewer output is short (checklist + verdict)
bash .claude/scripts/log-cost.sh "{RUN_ID}" "test-reviewer" "$INPUT_T" "$OUTPUT_T"
```

Read `runs/{RUN_ID}/state.md`.

**If `status: failed` (reviewer rejected the tests):**

Read `retry_count` from state.md.

- If `retry_count >= 2` → Stop. Tell the user:
  "Test-reviewer retry cap reached (2/2). Human intervention required.
  Review `runs/{RUN_ID}/report.md` for the feedback, fix the tests manually or run:
  `/resume-orchestration {RUN_ID} --from tester`
  Partial progress is already committed on `{BRANCH}` in {REPO} if you want to inspect it directly."
  Do not continue.

- If `retry_count < 2` → increment `retry_count` in state.md. Tell the user:
  "Test-reviewer rejected. Retrying Tester with feedback (attempt {retry_count}/2)..."

  Read the FAIL feedback from the last section of `runs/{RUN_ID}/report.md`.
  Archive the current tests:
  ```bash
  cp -r runs/{RUN_ID}/tests runs/{RUN_ID}/archive/tests_v{retry_count}
  ```

  Re-run Stage 3 (Tester) **without resetting retry_count** (see Stage 3 exception), injecting the reviewer feedback:
  "The previous test attempt was rejected by the test-reviewer with this feedback: {FEEDBACK}. Fix the issues listed before writing the new tests."

  Then re-run Stage 4 from Stage 4a (the contract check must pass again against the rewritten tests).

**If `status: running` (reviewer approved):**

Stamp the gate so the post-Coder check can detect any test file edited after this point:
```bash
touch "runs/{RUN_ID}/.test_reviewer_passed_at"
```

Continue to Stage 5.

---

## STAGE 5 — Coder Agent

Update `runs/{RUN_ID}/state.md`: set `step: coder`, `status: running`, `retry_count: 0`.
**Exception:** if you are re-entering this stage as a retry (from Stage 5b or Stage 6), do NOT reset `retry_count` — it must keep the value the retry logic just incremented, or the retry cap can never trip.

Read the file `.claude/commands/_coder-agent.md` in full.
Execute those instructions now with arguments: `--run {RUN_ID} --repo {REPO}`

### Commit each implemented file to {REPO}

Read `retry_count` from `runs/{RUN_ID}/state.md` once — `0` means first pass, `>0`
means this is a fix pass triggered by Stage 5b or Stage 6 below, for every file below.

Read the `## Files changed` table in `runs/{RUN_ID}/plan.md`. Work through it top to
bottom. For each row (`{file}`, `{action}`, `{purpose}`):

```bash
mkdir -p "{REPO}/$(dirname '{file}')"
cp "runs/{RUN_ID}/code/{file}" "{REPO}/{file}"
cd "{REPO}"
git add "{file}"
```

If nothing is staged for `{file}` (`git diff --cached --quiet -- "{file}"` exits 0),
it's unchanged from what's already committed — skip it, no commit. Otherwise
commit it on its own, right now, before moving to the next row:

- `retry_count` is `0`: `git commit -m "{VERB}: {file}\n\n{purpose}\n\nRun: {RUN_ID}"` — `{VERB}` is `create` if the table's Action column says CREATE, `modify` if MODIFY.
- `retry_count` is `>0`: `git commit -m "fix: {file} — address feedback (retry {retry_count}/2)\n\nRun: {RUN_ID}"`

```bash
cd "$ORCHESTRATOR_ROOT"
```

One commit per changed file, in table order — the plan's Files-changed table becomes
the commit history on `{BRANCH}` directly. Never batch multiple files into one
commit, and never commit a file with no staged diff.

After the Coder agent finishes, **log cost**:
```bash
# Estimate: input = agent prompt + plan.md + all tests + repo-digest.md; output = all code files
INPUT_T=$(( ( $(wc -c < .claude/commands/_coder-agent.md) + $(wc -c < runs/{RUN_ID}/plan.md) + $(find runs/{RUN_ID}/tests -type f 2>/dev/null | xargs wc -c 2>/dev/null | tail -1 | awk '{print $1}' || echo 600) + $(wc -c < runs/{RUN_ID}/repo-digest.md) ) / 4 ))
OUTPUT_T=$(( $(find runs/{RUN_ID}/code -type f 2>/dev/null | xargs wc -c 2>/dev/null | tail -1 | awk '{print $1}' || echo 800) / 4 ))
bash .claude/scripts/log-cost.sh "{RUN_ID}" "coder-agent" "$INPUT_T" "$OUTPUT_T"
```

Read `runs/{RUN_ID}/state.md`.

- If `status: paused` and `pause_reason: contract-mismatch` → Stop. Tell the user:
  "Pipeline paused: CONTRACT_MISMATCH detected. The Interface Contract needs correction.
  Run: `/resume-orchestration {RUN_ID} --from architect`
  See `runs/{RUN_ID}/report.md` for details.
  Whatever was already committed this pass is on `{BRANCH}` in {REPO}."
  Do not continue.

- If `status: running` → continue to Stage 5b.

---

## STAGE 5b — Code Contract Gate

Mechanical post-Coder check — catches a testid the Coder dropped, or tests that
were edited after the reviewer already passed them, before spending a sandbox run.

```bash
bash .claude/scripts/check-contract.sh "{RUN_ID}" "{REPO}" --code
CODE_CONTRACT_EXIT=$?
```

**If `CODE_CONTRACT_EXIT` is 0 (clean):** continue to Stage 6.

**If violations include `TESTS_MODIFIED_AFTER_REVIEW`:**

STOP immediately — this means a test file changed after the test-reviewer gate
already approved it, which is exactly the run-1 defect this gate exists to catch.
Update `runs/{RUN_ID}/state.md`: `status: failed`, `pause_reason: tests-modified-after-review`.
Append the violation(s) to `runs/{RUN_ID}/report.md`. Tell the user:
"Tests were modified after the test-reviewer approved them. This requires human review —
run `/resume-orchestration {RUN_ID} --from tester` once you've decided how to proceed.
Whatever was already committed this pass is on `{BRANCH}` in {REPO}."
Do not continue, and do not retry automatically.

**If violations only include `MISSING_TESTID_IN_CODE` (no test-edit violation):**

This is a Coder defect, not a test problem — route back to the Coder without
spending a sandbox run. Append the violations to `runs/{RUN_ID}/report.md` under:
```
[check-contract --code] FAIL — {N} violation(s), returning to Coder (retry {N}/2)

{script output, verbatim}
```

Read `retry_count` from state.md.

- If `retry_count >= 2` → Stop. Tell the user:
  "Code contract check retry cap reached (2/2). Human intervention required.
  Review `runs/{RUN_ID}/report.md` for the violations, fix the code manually or run:
  `/resume-orchestration {RUN_ID} --from coder`
  Partial progress is already committed on `{BRANCH}` in {REPO} if you want to inspect it directly."
  Do not continue.

- If `retry_count < 2` → increment `retry_count`. Tell the user:
  "Coder dropped a contract testid. Retrying Coder with the violation as feedback (attempt {retry_count}/2)..."

  Archive current code:
  ```bash
  cp -r runs/{RUN_ID}/code runs/{RUN_ID}/archive/code_v{retry_count}
  ```

  Re-run Stage 5 (Coder) **without resetting retry_count** (see Stage 5 exception) with `--feedback "{VIOLATIONS}"` injected.
  Then re-run Stage 5b.

---

## STAGE 6 — Test Sandbox

Update `runs/{RUN_ID}/state.md`: set `step: sandbox`, `status: running`.

Read the test command from `runs/{RUN_ID}/repo-digest.md` (look for the `## Test command` section).

Run the sandbox:
```bash
bash .claude/scripts/run-tests.sh "{REPO}" "{RUN_ID}" "{TEST_CMD}" 120
SANDBOX_EXIT=$?
```

**If exit code 0 (PASS):** continue to Stage 7.

**If exit code 2 (TIMEOUT):**
Tell the user: "Tests timed out. Routing back to Test-Reviewer."
Re-run Stage 4 (Test-Reviewer) with this note prepended:
"IMPORTANT: The previous test run timed out after 120s. Check all tests for unresolved promises, missing mocks, or real network calls."
Then re-run Stage 5 and 6 if the reviewer passes.

**If exit code 1 (FAIL):**
Read `retry_count` from state.md.

- If `retry_count >= 2` → Stop. Tell the user:
  "Coder retry cap reached (2/2). Human intervention required.
  Review `runs/{RUN_ID}/report.md` for the test failure output, then run:
  `/resume-orchestration {RUN_ID} --from coder`
  Partial progress is already committed on `{BRANCH}` in {REPO} if you want to inspect it directly."
  Do not continue.

- If `retry_count < 2` → increment `retry_count`. Tell the user:
  "Tests failed. Retrying Coder with failure output as feedback (attempt {retry_count}/2)..."

  Read the failure output from `runs/{RUN_ID}/report.md`.
  Archive current code:
  ```bash
  cp -r runs/{RUN_ID}/code runs/{RUN_ID}/archive/code_v{retry_count}
  ```

  Re-run Stage 5 (Coder) **without resetting retry_count** (see Stage 5 exception) with `--feedback "{FAILURE_OUTPUT}"` injected.
  Then re-run Stage 6.

**If exit code 3 or any other unlisted code (ERROR):**
STOP immediately. Update state.md: status: failed, pause_reason: sandbox-infrastructure-error.
Tell the user the sandbox environment is broken and show the report entry. Mention
that whatever was already committed is on `{BRANCH}` in {REPO}, untouched by this
failure — only the disposable sandbox worktree is affected.
You must NOT manually replicate the sandbox, run tests yourself, or declare
PASS/FAIL by any other means. The sandbox exit code is the only accepted verdict.

---

## STAGE 7 — Done

Update `runs/{RUN_ID}/state.md`:
- Set `step: done`, `status: done`
- Set `last_artifact: runs/{RUN_ID}/code/`
- Update `timestamp`

Append to `runs/{RUN_ID}/report.md`:
```
---
## Result: PASS ✓

All tests passed. Pipeline complete.
Finished: {TIMESTAMP}
```

Every test and code change has already been committed to `{BRANCH}` in {REPO} as the
pipeline worked, one commit per step — there is nothing left to copy or apply.
Collect the commit log for the report:

```bash
cd "{REPO}"
COMMIT_LOG=$(git log --oneline "{ORIGINAL_BRANCH}..{BRANCH}")
COMMIT_COUNT=$(git rev-list --count "{ORIGINAL_BRANCH}..{BRANCH}")
cd "$ORCHESTRATOR_ROOT"
```

Append to `runs/{RUN_ID}/report.md`:
```
### Commits on {BRANCH} ({COMMIT_COUNT} ahead of {ORIGINAL_BRANCH})
{COMMIT_LOG}
```

---

Read the token usage summary from `runs/{RUN_ID}/cost.md` and display the full table to the user.
Every figure in it is a heuristic estimate (chars/4) of token usage, not a count from the model — present it as such. No dollar figures — tokens only.

Append a one-line entry to `memory.md` under `## Run history`:
```
- {RUN_ID} | {DATE} | task: {TASK} | result: PASS | tokens: {TOTAL_FROM_COST_MD}
```

If this run surfaced a gotcha worth remembering for future runs (a naming convention,
a config quirk, a dependency trap — anything a future Architect/Coder/Tester would
otherwise rediscover the hard way), append one line to `memory.md` under `## Known gotchas`:
```
- [active] {gotcha, one sentence}. (added: {RUN_ID})
```
Skip this if nothing new was learned — don't pad the file.

Tell the user:
"✓ Pipeline complete! All tests passed.

**Run:** {RUN_ID}
**Branch:** `{BRANCH}` in {REPO} — {COMMIT_COUNT} commit(s) ahead of `{ORIGINAL_BRANCH}`:
{COMMIT_LOG}

Your previous branch (`{ORIGINAL_BRANCH}`) is untouched — switch back anytime with
`git checkout {ORIGINAL_BRANCH}`. Everything the pipeline did is already committed on
`{BRANCH}`; nothing further needs to be applied.

**Token usage (est.):** (show the cost.md table)

**Orchestrator-side artifacts (for reference/audit):**
- PRD: `runs/{RUN_ID}/prd.md`
- Plan + Interface Contract: `runs/{RUN_ID}/plan.md`
- Full report: `runs/{RUN_ID}/report.md`
- Token usage summary: `runs/{RUN_ID}/cost.md`"
