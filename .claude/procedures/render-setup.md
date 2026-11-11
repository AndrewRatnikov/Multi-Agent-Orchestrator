# Render setup procedure

Used by `/setup-render` (and, inside a pipeline run, carried out by `orch.py` itself). Moved here
unchanged from `run-orchestration.md` when the pipeline's control flow moved into `.claude/scripts/orch.py`.

To run a subagent here: spawn it with the Agent tool (`subagent_type: "orch-render-setup"`) and this
prompt, filling in only the lines that apply, then parse the `## RESULT` block at the end of its report
(missing or malformed: record it and treat it as a failure):
```
ORCHESTRATOR_ROOT: {ORCHESTRATOR_ROOT}
RUN_ID: {RUN_ID}
STAGE: orch-render-setup
REPO: {REPO}
TASK: {TASK}
ANSWERS: {the user's answers, if re-running after NEEDS_INPUT}
FEEDBACK: {violations / smoke-test failure, verbatim, if this is a retry}
Follow your agent instructions. Finish with the RESULT block.
```
The **scope check** is `git -C "{REPO}" status --porcelain`: it must print nothing.

Used by Stage 2b and by `/setup-render`. Inputs: `SETUP_DIR` (`runs/{RUN_ID}/render-setup`
here), `BASE` (`{ORIGINAL_BRANCH}`), `SETUP_BRANCH` (`chore/render-config`), and `CONTRACT`
(`runs/{RUN_ID}/contract.json` here; none for `/setup-render`). Everything happens in a throwaway
worktree: {REPO}'s checkout and `{BRANCH}` are never touched. **This procedure never fails the
run:** on any failure, record it and carry on without render config.

1. **Folder.** If `{SETUP_DIR}` exists from an earlier attempt, move it to
   `runs/{RUN_ID}/archive/render-setup_v{n}/`. Then `mkdir -p {SETUP_DIR}`. Set
   `render_setup_retries: 0` in `state.md`.
2. **Run subagent `orch-render-setup`** (see above), adding these lines to
   the prompt: `SETUP_DIR: {SETUP_DIR}`, `BASE: {BASE}`, and `CONTRACT: {CONTRACT}` if there is one.
   - `NEEDS_INPUT`: as for the Architect (`questions-render-setup.md`, `answers-render-setup.md`,
     run it again with `ANSWERS`). Unattended: treat it as a failure (step 6).
   - `BLOCKED` (e.g. the repo has no web UI): record `[render-setup] BLOCKED — {blocked_reason}`,
     offer the opt-out (`python3 .claude/scripts/render-config.py optout "{REPO}" --reason "{their words}"`), and stop the procedure without config.
   - `DONE`: run the scope check (above; it only reads, so {REPO}
     must still be clean), and record `[render-setup] DONE — {summary}` with its `signin`,
     `prerequisites` and `fixtures` lines.
3. **Commit it on the separate branch** (mechanical: validates, then commits in a worktree):
   ```bash
   python3 .claude/scripts/render-config.py apply "{SETUP_DIR}" "{REPO}" --base "{BASE}" --branch "{SETUP_BRANCH}" [--contract "{CONTRACT}"] [--reuse]
   ```
   Pass `--reuse` on every retry (the branch exists by then, and the fix lands on top of it).
   - Exit 0: record `[render-setup] committed {commit} on {SETUP_BRANCH}`.
   - Exit 1 (violations): a retry (step 5) with the violations as `FEEDBACK`.
   - Exit 6 (branch exists, first attempt): ask the user: commit on top of the existing
     `{SETUP_BRANCH}` (`--reuse`), or use a new name (`chore/render-config-{date}`). Unattended: the
     new name.
   - Exit 3: failure (step 6).
4. **Smoke test** (mechanical: opens the smoke targets, pages that already exist on `{BASE}`, on
   `{SETUP_BRANCH}` with its own config; reaching their `ready` testid proves sign-in and seeding work):
   ```bash
   python3 .claude/scripts/render-config.py smoke-prep "{SETUP_DIR}" "{REPO}" --branch "{SETUP_BRANCH}"
   node .claude/scripts/render-capture.mjs "{REPO}" render-smoke --run-dir "{SETUP_DIR}/smoke" --only after
   SMOKE_EXIT=$?
   ```
   - Exit 0 (CAPTURED): success (step 7).
   - Exit 1 (INCOMPLETE), 2 or 3 (ERROR): a retry (step 5) with `FEEDBACK` = the `reason` from
     `{SETUP_DIR}/smoke/render/summary.json`, plus: "Screenshots: {SETUP_DIR}/smoke/render/after/,
     dev server log: {SETUP_DIR}/smoke/render/logs/after-dev.log". Don't open the screenshots
     yourself; the subagent does.
   As with the sandbox, the exit code is the verdict: never capture by hand or override it.
5. **Retry.** If `render_setup_retries` is `>= 2`, failure (step 6). Otherwise increment it and
   re-run the subagent with the `FEEDBACK`, then step 3 with `--reuse`, then step 4.
6. **Failure.** Record `[render-setup] FAILED — {reason}` (and the branch and last commit, if one
   was made: it stays for the user to inspect or delete). Tell the user in one line that this run
   continues without render review. Don't set `render_config_ref`.
7. **Success.** Record `[render-setup] PASS — smoke test reached {N} page(s) on {SETUP_BRANCH}`.
   In a run: set `render_config_ref: {SETUP_BRANCH}` in `state.md` (the user chose this) and
   **re-check the contract** (`check-contract.py "{RUN_ID}" "{REPO}" validate`). Stage 7 puts "merge `{SETUP_BRANCH}`" on the handoff checklist.
