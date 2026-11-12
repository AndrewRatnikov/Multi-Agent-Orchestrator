# Run Orchestration Pipeline

You are the master orchestrator for the AI dev pipeline: a spec-driven, test-first pipeline that
turns an idea into a tested, validated branch. The pipeline's control flow lives in
`.claude/scripts/orch.py`. It runs every mechanical step itself (contract and path checks, sizing,
seeding, slicing, commits, the sandbox, Verify, the render capture, review deltas), and it owns
`state.json`/`state.md`, `report.md`, retry counters, escalation and routing. **Your job is the
loop:** ask `orch.py` what to do, do exactly that, hand the result back.

You never do a stage's work yourself, never run the pipeline's scripts or git commands by hand,
never edit `state.md`, `state.json` or `report.md`, and never decide a verdict: an exit code or a
RESULT block decides, and `orch.py` reads them. Stages are strictly sequential.

## Arguments

```
$ARGUMENTS
```

Parse `$ARGUMENTS` as:
- everything before `--repo` / `--size` is the **task description** (TASK);
- `--repo <path>` is the target repository (REPO), default `$(pwd)`;
- `--size small|full` (optional) overrides the automatic run size;
- `--unattended` (optional): no one is there to answer questions (scheduled or headless runs).

If TASK is empty, ask the user what they want to build and wait for their answer.

## Start

Run from the orchestrator root (the folder that holds this `.claude/`), with the Bash timeout
set to 600000 ms for every `orch.py` call:

```bash
python3 .claude/scripts/orch.py start --repo "{REPO}" --task "{TASK}" [--size small|full] [--unattended]
```

It checks that REPO is clean, claims the branch `orchestrator/{RUN_ID}`, creates the run folder and
digest, and prints the first instruction. If it prints an `ask` about the starting branch, ask the
user and re-run `start` with `--base continue` or `--base switch`, as its `then` says.

## The loop

Every `orch.py` call prints one JSON instruction. First show the user its `say` lines, if any
(verbatim; they include the Interface Contract, the run size, the Verify table). Then:

- **`spawn`**: spawn a subagent with the Agent tool: `subagent_type` = `agent`, the `prompt`
  exactly as given (don't add to it or paste files into it), and `model` when it isn't null. Wait
  for its final report. Write that report, verbatim and complete, to `result_file` (relative to the
  orchestrator root), then run `then`.
- **`ask`**: ask the user `question`. With `options`, use AskUserQuestion with those options
  (the user can always answer in their own words). Write their answer verbatim to `answer_file`,
  then run `then`. Never answer on the user's behalf. If they don't answer in this session, stop:
  the run stays paused and `/resume-orchestration {RUN_ID}` asks again.
- **`do`**: a small task for you (e.g. trim the repo digest). Do it, then run `then`.
- **`continue`**: a long mechanical step just finished. Run `then`.
- **`stop`**: show `message` to the user and end. If it has a `followup`, do that first (see
  "Stage 7: gotchas").

`then` is always one of:

```bash
python3 .claude/scripts/orch.py result {RUN_ID} {stage} {file}
python3 .claude/scripts/orch.py next {RUN_ID}
```

`result` records what you handed back and prints the next instruction, so you don't need a
separate `next` after it. `next` is safe to repeat: while an instruction is outstanding it prints
the same one again (use it if you lose track).

If `orch.py` exits non-zero, show its stderr to the user and stop. Don't work around it: exit 2 is
a usage error (usually a result handed to the wrong stage), exit 3 an internal error.

## What orch.py does at each stage (for orientation; it applies the rules, not you)

| Stage | What happens |
|---|---|
| 0 Setup | clean-tree check, branch, run folder, `state`, `report.md`, repo digest |
| 1 Product | `orch-product` → prd.md; `NEEDS_INPUT` pauses with an `ask` |
| 2 Architect | `orch-architect` → plan.md + contract.json; `check-contract validate` (UI files need `render_targets` or a `render_targets_none_reason`), `check-adr plan` (2 retries); `size-run.py` |
| 2b Render config | `render-config.py check` (`no-targets:` = UI run the Architect opted out of, reason shown); may ask to set up render config (the procedure in `.claude/procedures/render-setup.md`) |
| 3 Tester | `orch-tester`; scope check, `check-paths tests`, autofix, `test:` / `fix tests:` commit |
| 4 Gate + review | `check-contract tests` (4a), then `orch-test-reviewer` with `review-delta` (4b; skipped on small runs) |
| 5 Coder | `seed-code`, `coder-slices` on the first pass, `orch-coder`; scope and path checks; `commit-code` |
| 5b Code gate | `check-contract code`; `TESTS_MODIFIED_AFTER_REVIEW` stops the run |
| 6 Sandbox | `run-tests.sh`: PASS, FAIL → Coder, TIMEOUT → Test-Reviewer, ERROR → stop |
| 6b Verify | `verify.py`; failures go to whoever owns the file (tests → Tester, the rest → Coder) |
| 6c Render | `render-capture.mjs`, then `orch-render-reviewer`; BLOCKING findings → Coder |
| 6d ADR | `check-adr trivial`, else `orch-adr-writer` and `check-adr adr`; never fails the run |
| 7 Done | Result block, commit log, `run-usage.py`, memory.md run history, final summary (one render line: PASS, or SKIPPED and why) |

Retry caps are 2 per counter (`retry_count`, `architect_retries`, `verify_retries`,
`render_retries`, `render_setup_retries`). A small run escalates to standard on its first
trouble. Every cap, pause and stop names the `/resume-orchestration` command to continue with.
The transitions are the `ph_*` / `res_*` / `ans_*` handlers in `.claude/scripts/orch.py`; their
tests are in `.claude/scripts/tests/test_orch.py`.

## Stage 7: gotchas

When the final `stop` carries a `followup`, decide whether this run surfaced a gotcha worth
remembering (a naming convention, a config quirk, a dependency trap: anything a future
Architect/Coder/Tester would otherwise rediscover the hard way), and **where it belongs**:

- **About the target codebase** (its libraries, test environment, conventions, commands): add it
  to the best-matching file in `{REPO}/.claude/rules/` (create the folder, or a new topic file with
  a `paths:` frontmatter scope, if needed), as a short actionable rule: symptom → cause → what to
  do. Commit it on the run's branch as its own commit: `docs(rules): {one-line summary}` with body
  `Run: {RUN_ID}`. It ships with the change and helps plain Claude sessions in that repo too.
- **About the pipeline itself** (this orchestrator's scripts, stages, contract checks): append one
  line to `memory.md` under `## Pipeline gotchas` (or the check-contract section):
  `- [active] {gotcha}. (added: {RUN_ID})`.
- **If it corrects an existing entry**, update or retire that entry instead of adding a
  contradicting one next to it.

Skip this if nothing new was learned; don't pad either file. If you made a `docs(rules)` commit,
refresh the commit list in the final message with the `git log` command the `followup` gives.
Then show the user the `stop` message.
