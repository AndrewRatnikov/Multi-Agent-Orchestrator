# Resume Orchestration Pipeline

You are the master orchestrator resuming a paused or failed pipeline run. As in
`/run-orchestration`, the control flow lives in `.claude/scripts/orch.py`: it checks the repo,
archives what will be overwritten, sets the run going again, and from then on tells you what to do
one instruction at a time. You never do a stage's work yourself, never run the pipeline's scripts
or git commands by hand, and never edit `state.md`, `state.json` or `report.md`.

## Arguments

```
$ARGUMENTS
```

Parse `$ARGUMENTS` as `{RUN_ID} [--from {STEP}]`, for example:
- `run_20260626_143022`: resume where the run stopped (the `step` in its state);
- `run_20260626_143022 --from architect`: resume from a specific step.

Valid steps: `product`, `architect`, `render-setup`, `tester`, `test-reviewer`, `coder`, `sandbox`,
`verify`, `render`, `adr`.

If no RUN_ID is given, list the folders in `runs/` with the `step` and `status` lines of each
`state.md`, and ask the user which to resume.

## Resume

From the orchestrator root, with the Bash timeout set to 600000 ms for every `orch.py` call:

```bash
python3 .claude/scripts/orch.py resume {RUN_ID} [--from {STEP}]
```

Its first instruction's `say` lines show the run's state and the last 20 lines of `report.md`.
Show them to the user. Then follow the loop exactly as in `run-orchestration.md` ("The loop"):
`spawn`, `ask`, `do`, `continue` and `stop`, each handed back with the `then` command it gives.

What `resume` does, so you can explain it (you don't do any of it yourself):
- **Repo check.** It refuses a dirty tree in REPO (you get a `stop` saying so: the user commits or
  stashes, then resumes again), and checks out the run's branch if REPO is on another one.
- **Archive.** Before a step rewrites an artifact, it copies or moves the old one to
  `runs/{RUN_ID}/archive/…_before_resume_{timestamp}`: `prd.md` for product, `plan.md` and
  `contract.json` for architect, `tests/` for tester, `code/` for coder, `render-review.md` for
  render, `adr/` for adr.
- **State.** It sets `step`, `status: running`, clears `pause_reason`, and logs
  `[RESUMED] step: {STEP} at {TIMESTAMP}` in `report.md`.
- **Pending questions.** If the run was paused for questions and
  `answers-{STEP}.md` exists (the user may have written it by hand), it uses that; otherwise its
  first instruction is an `ask` with the questions.
- **Run size.** It keeps the run's size. Resuming from architect sizes the run again unless it
  had already escalated. Resuming from tester, test-reviewer or coder after a failure escalates a
  small run to standard first.
- **Coder.** An interrupted sliced first pass continues with its remaining slices. Otherwise a
  coder resume is a fix pass, with the last failure as feedback and one `fix:` commit.
- **Old runs.** A run from before `orch.py` (no `state.json`) is imported from its `state.md`.

## Special cases

- **Render config for a run already past the Tester** (its render review ended SKIPPED): don't
  resume from `render-setup` (that re-runs every stage after it). Run `/setup-render --repo {REPO}`,
  then add `render_config_ref: {the branch it made}` to the run's `state.md` (the one edit to
  `state.md` that's yours to make: `orch.py` picks it up and logs it), then
  `/resume-orchestration {RUN_ID} --from render`.
- **Render review after a fix by hand**, or after a `render-incomplete` pause: `--from render`.
  Everything before render is assumed done; it doesn't re-run Verify.
- **ADRs** for a run that finished before the ADR stage existed, or whose ADRs were `NOT WRITTEN`:
  `--from adr`. If a `docs(adr)` commit for this run is already on the branch, the writer edits
  those files instead of adding new ones.
- **Scope violation** (`pause_reason: scope-violation`): a subagent wrote into REPO. Show the
  user the files; they clean up, then resume.

## Completion

The last instruction is a `stop` with `status: done`. Handle its `followup` exactly as in
`run-orchestration.md` ("Stage 7: gotchas"), then show the user the message.
