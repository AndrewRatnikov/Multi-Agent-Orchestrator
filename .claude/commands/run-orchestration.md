# Run Orchestration Pipeline

You are the master orchestrator for the AI dev pipeline. Your job is to run a spec-driven, test-first pipeline that turns an idea into a tested, validated implementation.

You sequence the following stages in order. **The LLM stages (Product, Architect, Tester, Test-Reviewer, Coder) each run as their own subagent** (`.claude/agents/orch-*.md`) in a fresh context, with their own model and tool limits. You never do a stage's work yourself, and you never execute an agent's instructions inline. You own everything between stages: `state.md`, `report.md`, `memory.md`, retry counters, the mechanical gates, the sandbox, and every git operation. You do not skip steps, invent shortcuts, or call external services; you follow the stage instructions exactly.

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

## How to run a subagent stage

Use this procedure for every stage below that says **Run subagent `orch-…`**.

1. **Spawn it** with the Agent tool, `subagent_type: "orch-<stage>"`. Use this prompt, filling in only the lines that apply:
   ```
   ORCHESTRATOR_ROOT: {ORCHESTRATOR_ROOT}
   RUN_ID: {RUN_ID}
   REPO: {REPO}
   TASK: {TASK}
   ANSWERS: {the user's answers, if re-running after NEEDS_INPUT}
   FEEDBACK: {violations / reviewer failures / sandbox output, verbatim, if this is a retry}
   NOTE: {extra instruction from the stage, if any}
   Follow your agent instructions. Finish with the RESULT block.
   ```
   Pass file contents only by path, never pasted in: the subagent reads them itself. That keeps your own context small and the stage's context clean.
2. **Wait for its final report.** Stages are strictly sequential: never start the next stage, or a second copy of this one, while it's still running.
3. **Parse the `## RESULT` block** at the end of its report. If the block is missing or malformed, treat that as a stage failure: record it in `report.md` and stop. Don't guess the outcome from the prose.
4. **Record it.** Append a line to `runs/{RUN_ID}/report.md`, e.g. `[{stage}] {status or verdict} — {summary}`, followed by any lists the RESULT block contains (files, failures, gaps). Subagents never write `state.md`, `report.md` or `memory.md`; you do.
5. **Scope check (Tester and Coder only),** before any `cp` into {REPO}:
   ```bash
   git -C "{REPO}" status --porcelain
   ```
   The output must be empty: at this point, anything the pipeline wants in {REPO} is committed. If anything shows up, a subagent wrote outside its folder (its write guard blocks Write/Edit, but not Bash). Show the user the list, set `status: paused`, `pause_reason: scope-violation` in `state.md`, and stop. Don't delete anything yourself.
6. **Questions (Product and Architect only).** If the RESULT says `status: NEEDS_INPUT`:
   - Write the questions to `runs/{RUN_ID}/questions-{stage}.md`, and set `status: paused`, `pause_reason: awaiting_human_input` in `state.md`.
   - Show the questions to the user and ask them to answer here.
   - If they answer in this session: save the answers to `runs/{RUN_ID}/answers-{stage}.md`, set `status: running`, and run the same subagent again with `ANSWERS`. Repeat if it asks again.
   - If the session ends first, `/resume-orchestration {RUN_ID} --from {stage}` picks it up (it reads the questions file).

---

## STAGE 0 — Setup

### Claim a branch in {REPO}

Every commit this pipeline makes from here on lands directly in {REPO}, one commit
per plan step, as the work happens — not copied in at the end. That means {REPO}
must be clean before anything starts.

```bash
DIRTY=$(git -C "{REPO}" status --porcelain)
```

**If `$DIRTY` is non-empty:** STOP. Tell the user:
"{REPO} has uncommitted changes. This pipeline commits directly to a new branch in
your repo as it works, so it needs a clean tree to start from. Commit or stash your
changes, then re-run."
Do not create a run folder. Do not proceed any further.

**If clean:**
```bash
RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
ORIGINAL_BRANCH=$(git -C "{REPO}" rev-parse --abbrev-ref HEAD)
BRANCH="orchestrator/$RUN_ID"
git -C "{REPO}" checkout -B "$BRANCH"
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

**Run subagent `orch-product`** (see "How to run a subagent stage"). Handle `NEEDS_INPUT` as described there.

On `status: DONE`, confirm `runs/{RUN_ID}/prd.md` exists. Update `state.md`: `step: architect`, `last_artifact: runs/{RUN_ID}/prd.md`, `timestamp`. Tell the user the PRD is written, with the RESULT's one-sentence summary.

After the Product agent finishes, **log cost**:
```bash
# Estimate: input = agent prompt + task description; output = prd.md
INPUT_T=$(( ( $(wc -c < .claude/agents/orch-product.md) + ${#TASK} ) / 4 ))
OUTPUT_T=$(( $(wc -c < runs/{RUN_ID}/prd.md 2>/dev/null || echo 400) / 4 ))
bash .claude/scripts/log-cost.sh "{RUN_ID}" "product-agent" "$INPUT_T" "$OUTPUT_T"
```

- If the run is still paused for questions (the user didn't answer in this session): stop. Tell the user:
  "Pipeline paused at **product** stage. Answer the questions above (here, or later with
  `/resume-orchestration {RUN_ID} --from product`)."
  Do not continue.

- If `prd.md` exists and `status: running`: continue to Stage 2.

---

## STAGE 2 — Architect Agent

Update `runs/{RUN_ID}/state.md`: set `step: architect`, `status: running`.

**Run subagent `orch-architect`** (see "How to run a subagent stage"). Handle `NEEDS_INPUT` as described there.

On `status: DONE`, confirm `runs/{RUN_ID}/plan.md` exists and that `repo-digest.md`'s `## Test command` is not `UNKNOWN`. Update `state.md`: `step: tester`, `last_artifact: runs/{RUN_ID}/plan.md`, `timestamp`. Show the user the Interface Contract the subagent pasted below its RESULT block, and the Test command, so they can spot problems before the Tester runs.

After the Architect agent finishes, **log cost**:
```bash
# Estimate: input = agent prompt + prd.md + repo-digest.md; output = plan.md
INPUT_T=$(( ( $(wc -c < .claude/agents/orch-architect.md) + $(wc -c < runs/{RUN_ID}/prd.md) + $(wc -c < runs/{RUN_ID}/repo-digest.md) ) / 4 ))
OUTPUT_T=$(( $(wc -c < runs/{RUN_ID}/plan.md 2>/dev/null || echo 800) / 4 ))
bash .claude/scripts/log-cost.sh "{RUN_ID}" "architect-agent" "$INPUT_T" "$OUTPUT_T"
```

- If the run is still paused for questions: stop. Tell the user:
  "Pipeline paused at **architect** stage. Answer the questions above (here, or later with
  `/resume-orchestration {RUN_ID} --from architect`)."
  Do not continue.

- If `plan.md` exists and `status: running`: continue to Stage 3.

---

## STAGE 3 — Tester Agent

Update `runs/{RUN_ID}/state.md`: set `step: tester`, `status: running`, `retry_count: 0`.
**Exception:** if you are re-entering this stage as a retry (from Stage 4a or 4b), do NOT reset `retry_count` — it must keep the value the retry logic just incremented, or the retry cap can never trip.

**Run subagent `orch-tester`** (see "How to run a subagent stage"). On a retry, pass the violations or reviewer failures as `FEEDBACK`.

On `status: DONE`: run the scope check (step 5 of the procedure). If the RESULT lists any CONTRACT_GAPs, show them to the user; they may want to resume from architect to fill them. Update `state.md`: `step: test-reviewer`, `last_artifact: runs/{RUN_ID}/tests/`, `timestamp`.

### Commit the tests to {REPO}

Tests are the first thing that lands in the target repo — written and committed
before any implementation exists, directly on `{BRANCH}`:

```bash
cp -r "runs/{RUN_ID}/tests/." "{REPO}/"
git -C "{REPO}" add -A
```

If nothing is staged (`git -C "{REPO}" diff --cached --quiet` exits 0 — only happens if a
retry produced byte-identical tests), skip the commit. Otherwise, read `retry_count`
from `runs/{RUN_ID}/state.md`: `0` means this is the first attempt, `>0` means a
fix pass triggered by Stage 4a/4b below.

```bash
git -C "{REPO}" commit -m "{MSG}

Run: {RUN_ID}"
```

Where `{MSG}` is:
- `retry_count` is `0`: `test: {TASK}` (plus a short body: "Written test-first, before any implementation. Not yet reviewed — subject to the mechanical contract gate and the test-reviewer.")
- `retry_count` is `>0`: `fix tests: {TASK} (retry {retry_count}/2)`

After the Tester agent finishes, **log cost**:
```bash
# Estimate: input = agent prompt + plan.md; output = all test files combined
INPUT_T=$(( ( $(wc -c < .claude/agents/orch-tester.md) + $(wc -c < runs/{RUN_ID}/plan.md) ) / 4 ))
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

  Re-run Stage 3 (Tester) **without resetting retry_count** (see Stage 3 exception), with `FEEDBACK`:
  "The mechanical contract check failed with these violations: {VIOLATIONS}. Fix every one before writing the new tests — these are not style suggestions, they are exact name/path/package mismatches. Check memory.md's check-contract false-positive list first; if a violation is a known false positive, handle it as that list says."

  Then re-run Stage 4 from Stage 4a.

**If `CONTRACT_EXIT` is 0 (clean):** continue to Stage 4b.

### Stage 4b — LLM Test-Reviewer (judgment calls only: assertion quality, coverage)

**Run subagent `orch-test-reviewer`** (see "How to run a subagent stage"). It runs in a fresh context with read-only tools, so it judges tests it did not write. If you came here from a Stage 6 timeout, pass that stage's note as `NOTE`.

Record its RESULT in `report.md` verbatim (verdict, checklist, failures, contract gaps). **Only you change `state.md` and `retry_count`**; the reviewer never does. That keeps each rejection counted exactly once.

After the Test-Reviewer finishes, **log cost**:
```bash
INPUT_T=$(( ( $(wc -c < .claude/agents/orch-test-reviewer.md) + $(find runs/{RUN_ID}/tests -type f 2>/dev/null | xargs wc -c 2>/dev/null | tail -1 | awk '{print $1}' || echo 600) + $(wc -c < runs/{RUN_ID}/plan.md) ) / 4 ))
OUTPUT_T=300  # reviewer output is short (checklist + verdict)
bash .claude/scripts/log-cost.sh "{RUN_ID}" "test-reviewer" "$INPUT_T" "$OUTPUT_T"
```

**If `verdict: FAIL` (reviewer rejected the tests):**

Read `retry_count` from state.md.

- If `retry_count >= 2` → Stop. Tell the user:
  "Test-reviewer retry cap reached (2/2). Human intervention required.
  Review `runs/{RUN_ID}/report.md` for the feedback, fix the tests manually or run:
  `/resume-orchestration {RUN_ID} --from tester`
  Partial progress is already committed on `{BRANCH}` in {REPO} if you want to inspect it directly."
  Do not continue.

- If `retry_count < 2` → increment `retry_count` in state.md. Tell the user:
  "Test-reviewer rejected. Retrying Tester with feedback (attempt {retry_count}/2)..."

  The feedback is the `failures:` list from the reviewer's RESULT block.
  Archive the current tests:
  ```bash
  cp -r runs/{RUN_ID}/tests runs/{RUN_ID}/archive/tests_v{retry_count}
  ```

  Re-run Stage 3 (Tester) **without resetting retry_count** (see Stage 3 exception), with `FEEDBACK`:
  "The previous test attempt was rejected by the test-reviewer with this feedback: {FAILURES}. Fix the issues listed before writing the new tests."

  Then re-run Stage 4 from Stage 4a (the contract check must pass again against the rewritten tests).

**If `verdict: PASS` (reviewer approved):**

Update `state.md`: `step: coder`, `status: running`. Show the user any contract gaps the reviewer listed as advisory items.

Stamp the gate so the post-Coder check can detect any test file edited after this point:
```bash
touch "runs/{RUN_ID}/.test_reviewer_passed_at"
```

Continue to Stage 5.

---

## STAGE 5 — Coder Agent

Update `runs/{RUN_ID}/state.md`: set `step: coder`, `status: running`, `retry_count: 0`.
**Exception:** if you are re-entering this stage as a retry (from Stage 5b or Stage 6), do NOT reset `retry_count` — it must keep the value the retry logic just incremented, or the retry cap can never trip.

**Run subagent `orch-coder`** (see "How to run a subagent stage"). On a retry, pass the sandbox failure output or check-contract violations as `FEEDBACK`.

- `status: CONTRACT_MISMATCH` → append its `detail` to `report.md`, set `status: paused`, `pause_reason: contract-mismatch` in `state.md`, and handle it as described at the end of this stage (no commits for this pass).
- `status: DONE` → run the scope check (step 5 of the procedure), confirm every row of plan.md's Files-changed table has a file under `runs/{RUN_ID}/code/` (missing ones are a Coder failure: append to report and treat like a Stage 5b violation), then commit as below. If its `notes` suspect an environment issue (Test command, generated client, shared build), surface that to the user; the fix may belong in `repo-digest.md`'s Test command, not in the code.

### Commit each implemented file to {REPO}

Read `retry_count` from `runs/{RUN_ID}/state.md` once — `0` means first pass, `>0`
means this is a fix pass triggered by Stage 5b or Stage 6 below, for every file below.

Read the `## Files changed` table in `runs/{RUN_ID}/plan.md`. Work through it top to
bottom. For each row (`{file}`, `{action}`, `{purpose}`):

```bash
mkdir -p "{REPO}/$(dirname '{file}')"
cp "runs/{RUN_ID}/code/{file}" "{REPO}/{file}"
git -C "{REPO}" add "{file}"
```

If nothing is staged for `{file}` (`git -C "{REPO}" diff --cached --quiet -- "{file}"` exits 0),
it's unchanged from what's already committed — skip it, no commit. Otherwise
commit it on its own, right now, before moving to the next row:

- `retry_count` is `0`: `git commit -m "{VERB}: {file}\n\n{purpose}\n\nRun: {RUN_ID}"` — `{VERB}` is `create` if the table's Action column says CREATE, `modify` if MODIFY.
- `retry_count` is `>0`: `git commit -m "fix: {file} — address feedback (retry {retry_count}/2)\n\nRun: {RUN_ID}"`

```

One commit per changed file, in table order — the plan's Files-changed table becomes
the commit history on `{BRANCH}` directly. Never batch multiple files into one
commit, and never commit a file with no staged diff.

**Lockfile.** If any `package.json` changed in this pass, update the lockfile so a
frozen install (CI, the deploy, and Stage 6b) works, and commit it separately:
```bash
(cd "{REPO}" && pnpm install --lockfile-only)   # npm repos: npm install --package-lock-only
git -C "{REPO}" add pnpm-lock.yaml
git -C "{REPO}" diff --cached --quiet || git -C "{REPO}" commit -m "chore: update lockfile

Run: {RUN_ID}"
```

After the Coder agent finishes, **log cost**:
```bash
# Estimate: input = agent prompt + plan.md + all tests + repo-digest.md; output = all code files
INPUT_T=$(( ( $(wc -c < .claude/agents/orch-coder.md) + $(wc -c < runs/{RUN_ID}/plan.md) + $(find runs/{RUN_ID}/tests -type f 2>/dev/null | xargs wc -c 2>/dev/null | tail -1 | awk '{print $1}' || echo 600) + $(wc -c < runs/{RUN_ID}/repo-digest.md) ) / 4 ))
OUTPUT_T=$(( $(find runs/{RUN_ID}/code -type f 2>/dev/null | xargs wc -c 2>/dev/null | tail -1 | awk '{print $1}' || echo 800) / 4 ))
bash .claude/scripts/log-cost.sh "{RUN_ID}" "coder-agent" "$INPUT_T" "$OUTPUT_T"
```

- If the Coder returned `CONTRACT_MISMATCH` (`pause_reason: contract-mismatch`) → Stop. Tell the user:
  "Pipeline paused: CONTRACT_MISMATCH detected. The Interface Contract needs correction.
  Run: `/resume-orchestration {RUN_ID} --from architect`
  See `runs/{RUN_ID}/report.md` for details.
  Whatever was already committed this pass is on `{BRANCH}` in {REPO}."
  Do not continue.

- Otherwise → continue to Stage 5b.

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

  Re-run Stage 5 (Coder) **without resetting retry_count** (see Stage 5 exception) with `FEEDBACK: {VIOLATIONS}`.
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

**If exit code 0 (PASS):** continue to Stage 6b.

**If exit code 2 (TIMEOUT):**
Tell the user: "Tests timed out. Routing back to Test-Reviewer."
Re-run Stage 4 (Test-Reviewer) with `NOTE`:
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

  Re-run Stage 5 (Coder) **without resetting retry_count** (see Stage 5 exception) with `FEEDBACK: {FAILURE_OUTPUT}`.
  Then re-run Stage 6.

**If exit code 3 or any other unlisted code (ERROR):**
STOP immediately. Update state.md: status: failed, pause_reason: sandbox-infrastructure-error.
Tell the user the sandbox environment is broken and show the report entry. Mention
that whatever was already committed is on `{BRANCH}` in {REPO}, untouched by this
failure — only the disposable sandbox worktree is affected.
You must NOT manually replicate the sandbox, run tests yourself, or declare
PASS/FAIL by any other means. The sandbox exit code is the only accepted verdict.

---

## STAGE 6b — Verify

The sandbox proved the new tests pass. Verify checks the **committed branch** the
way CI and the deploy will: a fresh worktree of `{BRANCH}`, a frozen-lockfile
install, and every step in the target repo's `.claude/verify.json` (lint,
typecheck, builds, **all** test suites, migrations against a throwaway database,
e2e), plus the task-specific commands from plan.md's ```` ```verify ```` block. The
config is read from `{ORIGINAL_BRANCH}`, so a run can't weaken its own checks. Any
step that fails is re-run on `{ORIGINAL_BRANCH}`; if it fails there too, it's
reported as PRE-EXISTING and doesn't block. If the branch adds migrations and Neon
credentials are available, it also applies them to a temporary Neon branch (a copy
of the real data), then deletes that branch.

Update `state.md`: `step: verify`, `status: running`.

```bash
python3 .claude/scripts/verify.py "{REPO}" "{RUN_ID}"
VERIFY_EXIT=$?
```

It writes `runs/{RUN_ID}/verify.md` (summary, failures, production follow-ups) and
per-step logs in `runs/{RUN_ID}/verify-logs/`, and appends the summary table to
`report.md`. The exit code is the verdict. As with the sandbox, never run the checks
yourself or override the result.

**0 (PASS):** continue to Stage 7.

**1 (FAIL):** a check fails because of this run. Show the user the table from
`verify.md`, then route by what failed:
- A lockfile problem (install step with the lockfile hint): fix it yourself with the
  Stage 5 lockfile commands, then re-run Stage 6b. This doesn't count as a retry.
- Anything else (lint, typecheck, build, tests, migrations, e2e): it's a Coder
  problem. Use the same retry logic and `retry_count` as Stage 6: if `retry_count >= 2`
  stop and tell the user (`/resume-orchestration {RUN_ID} --from coder` after they
  intervene). Otherwise increment it, archive `code/`, and re-run Stage 5 with
  `FEEDBACK` = the "Failures caused by this run" section of `verify.md`, verbatim.
  Then Stage 5b → 6 → 6b again.

**3 (ERROR):** infrastructure (worktree, Docker start, etc.), not a code verdict.
Set `status: failed`, `pause_reason: verify-infrastructure-error`, show the error, stop.

**4 (INCOMPLETE):** nothing failed, but required checks couldn't run (usually: no
Docker, so the database steps were skipped). Set `status: paused`,
`pause_reason: verify-incomplete`, and show the "Not verified" list. The user can:
- start Docker (or export `VERIFY_DATABASE_URL` pointing at a local throwaway
  Postgres) and run `/resume-orchestration {RUN_ID} --from verify`, or
- explicitly accept the gap by telling you so in this session. Then record
  `[verify] ACCEPTED UNVERIFIED by user: {list}` in `report.md` and continue to
  Stage 7, where the gap is repeated in the final summary. Never accept it on the
  user's behalf.

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
COMMIT_LOG=$(git -C "{REPO}" log --oneline "{ORIGINAL_BRANCH}..{BRANCH}")
COMMIT_COUNT=$(git -C "{REPO}" rev-list --count "{ORIGINAL_BRANCH}..{BRANCH}")
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

If this run surfaced a gotcha worth remembering (a naming convention, a config quirk,
a dependency trap: anything a future Architect/Coder/Tester would otherwise rediscover
the hard way), first decide **where it belongs**:

- **About the target codebase** (its libraries, test environment, conventions, commands):
  add it to the best-matching file in `{REPO}/.claude/rules/` (create the folder, or a new
  topic file with a `paths:` frontmatter scope, if needed). Write it as a short actionable
  rule: symptom → cause → what to do. Commit it on `{BRANCH}` as its own commit:
  `docs(rules): {one-line summary}` with body `Run: {RUN_ID}`. It then ships with the
  change, gets reviewed in the PR, and helps plain Claude sessions in that repo too.
- **About the pipeline itself** (this orchestrator's scripts, stages, contract checks):
  append one line to `memory.md` under `## Pipeline gotchas` (or the check-contract
  section): `- [active] {gotcha}. (added: {RUN_ID})`.
- **If it corrects an existing entry**, update or retire that entry instead of adding
  a contradicting one next to it.

Skip this if nothing new was learned; don't pad either file.

If you made a `docs(rules)` commit, re-run the two commit-log commands above so the summary below includes it.

Build the **handoff checklist** for the final message from:
- `verify.md`: "Production follow-up: database migrations" (migrations that must be
  applied to production as part of the deploy: coin-collector's Render build doesn't
  run `prisma migrate deploy`), "Pre-existing failures", and any accepted "Not verified" items
- plan.md's `## Verification` → "Manual" list (live-DB checks, visual checks, etc.)

Tell the user:
"✓ Pipeline complete! Tests passed and the branch is verified.

**Run:** {RUN_ID}
**Branch:** `{BRANCH}` in {REPO} — {COMMIT_COUNT} commit(s) ahead of `{ORIGINAL_BRANCH}`:
{COMMIT_LOG}

Your previous branch (`{ORIGINAL_BRANCH}`) is untouched — switch back anytime with
`git checkout {ORIGINAL_BRANCH}`. Everything the pipeline did is already committed on
`{BRANCH}`; nothing further needs to be applied.

**Verify:** (the table from `runs/{RUN_ID}/verify.md`)

**Before you merge / deploy:** (the handoff checklist; say "nothing" if empty)

**Token usage (est.):** (show the cost.md table)

**Orchestrator-side artifacts (for reference/audit):**
- PRD: `runs/{RUN_ID}/prd.md`
- Plan + Interface Contract: `runs/{RUN_ID}/plan.md`
- Full report: `runs/{RUN_ID}/report.md`
- Verify details + logs: `runs/{RUN_ID}/verify.md`, `runs/{RUN_ID}/verify-logs/`
- Token usage summary: `runs/{RUN_ID}/cost.md`"
