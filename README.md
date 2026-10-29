# AI Orchestrator

A spec-driven, test-first multi-agent pipeline for Claude Code. Hand it an idea — it produces a tested, validated branch.

## How it works

```
/run-orchestration "add a Budget Summary card component"
```

The pipeline runs sequentially, with a review gate after each stage. Each LLM stage is its own subagent (`.claude/agents/orch-*.md`) with a fresh context, its own model and tool limits, and a hook that only lets it write into its own artifact folder. The main session only orchestrates: it owns state, retries, gates and git.

```
Product agent    → prd.md
Architect agent  → plan.md + Interface Contract
Tester agent     → test files (written before any code)
Test-Reviewer    → gate: are the tests any good?
Coder agent      → implementation files
Test sandbox     → ground truth: exit code only
Verify           → the committed branch as CI/deploy see it: frozen install, lint,
                   typecheck, builds, all tests, migrations + e2e on a throwaway DB
Render review    → opens the pages the run changed in a real browser (before/after,
                   desktop + mobile) and judges them against the PRD
ADR writer       → records the decisions the run shipped as ADRs in the target repo's
                   docs/adr/, committed on the branch with the code
```

Verify's checks come from the target repo's `.claude/verify.json` (read from the original
branch, so a run can't weaken them). Database steps need Docker running, or
`VERIFY_DATABASE_URL` pointing at a local throwaway Postgres; without either, the run
stops as INCOMPLETE rather than claiming it's verified. Optional: with `NEON_API_KEY` and
`NEON_PROJECT_ID` (env or `~/.config/ai-orchestrator/neon.env`), new migrations are also
applied to a temporary Neon branch, a copy of the real data.

Render review closes the gap that unit tests leave: nothing else looks at the *composed*
page. The Architect declares `render_targets` in `contract.json` (routes and the named
states they must be seen in, e.g. "no budget set"). `render-capture.mjs` starts the repo's
dev server in throwaway worktrees of the branch and of its original branch, drives Playwright
(pinned in `.claude/render-tools/`, never added to the target repo) and saves screenshots,
visible text and console errors; `orch-render-reviewer` then writes `render-review.md`.
BLOCKING findings (e.g. the same figures shown twice) go back to the Coder, capped at 2;
NOTEs only go into the report. How to get past sign-in is per-repo: a `render` section in
the target's `.claude/verify.json` plus a hook under `.claude/render/`, both read from the
original branch. A repo without that config ends as **SKIPPED**, never PASS, and the final
summary says so. `/resume-orchestration <id> --from render` re-runs just this stage.

Small tasks don't pay for the full ceremony. Right after the Architect, `size-run.py` sizes the
run from `contract.json`: at most 3 code files, no new package, no sensitive path (schema,
auth, crypto, dependencies, build config, plus the repo's own list in the `sizing` section of
its `.claude/verify.json`), no user answers and no ADR-worthy decision make it **small**. A small
run skips the LLM Test-Reviewer and runs the Tester and Coder on Sonnet; every mechanical gate
(contract checks, sandbox, Verify, render review) still runs. The first sign of trouble (any
retry, a contract mismatch, a failed gate) escalates it to standard, and the skipped review
runs then. `--size small|full` overrides the rules.

Decisions get written down where the code lives. The Architect reads the target repo's
`docs/adr/` before planning (an accepted ADR binds it like a rule) and lists the plan's own
decisions in a `## Decisions` section: options, why, what it supersedes. `check-adr.py plan`
makes sure that section is there. After the render review, `orch-adr-writer` keeps the
decisions that actually shipped and clear the bar (real alternatives, hard to reverse,
a pattern, an accepted trade-off, or a contradiction of an existing doc), writes them as
`docs/adr/YYYY-MM-DD-slug.md`, marks any ADR they supersede, and lists design docs that are
now out of date for the handoff checklist. `check-adr.py adr` checks the files before they
are committed as one `docs(adr)` commit. This stage never fails a run.

The Interface Contract has two parts: the readable explanation in `plan.md`, and
`runs/<id>/contract.json` with every name (files with owners, exports, and testids
categorised as new / existing / template / test-only / removed, plus new packages).
`check-contract.py` validates it right after the Architect, then checks the Tester's
and Coder's output against it exactly (`check-contract.sh` remains only for old runs).

Before anything is committed, `check-paths.py` checks that the Tester's and Coder's files
are exactly the ones the plan's Files changed table declares, and `autofix.py` runs the
repo's auto-fixer (`"autofix"` in verify.json, default `eslint --fix` if the repo has it).
When Verify finds new errors, each one goes back to whoever owns the file: test files to
the Tester (then a quick re-review), everything else to the Coder.

Before any of that runs, the target repo must be clean — the pipeline immediately
checks out a new `orchestrator/{run_id}` branch there and works on it directly for
the rest of the run. Planning docs (`prd.md`, `plan.md`) stay in this orchestrator
project's `runs/{run_id}/`, but tests and code land as real commits in the target
repo as they're produced: one commit for the tests (written before any
implementation), then one commit per file from the plan's Files-changed table as the
Coder implements it. A retry doesn't rewrite history — it adds a `fix:` commit on
top. By the time a run finishes, the change is already fully committed on
`orchestrator/{run_id}`; there's nothing left to copy or apply. Your previous branch
is never touched.

A failed stage stops the run and writes a report, and whatever was committed so far
stays right there on the branch for you to inspect. You can resume from any step:

```
/resume-orchestration run_20260626_143022 --from coder
```

For changes made outside the pipeline (dependency updates, config tweaks, quick fixes),
run only the Verify stage on a branch:

```
/verify-branch chore/deps-2026-10 --repo ~/Projects/coin-collector-companion
```

It compares the committed branch with the default branch (or `--base`) and reports the
same table, failures, pre-existing failures and migration follow-ups as a pipeline run.

## Key design decisions

- **Tests before code** — the coder gets an objective, machine-checkable target instead of reviewer vibes
- **Interface Contract** — the Architect names every file path, export, and test selector before the Tester or Coder touch anything; neither agent invents names independently
- **Repo digest** — the Architect is grounded in the real codebase (directory tree, deps, existing conventions) so the Interface Contract reflects reality
- **Exit code is ground truth** — test results come from the test runner's exit code, never the agent's self-report
- **One pause/resume system** — same mechanism handles question-blocking (Product/Architect ask clarifying questions) and failure handling (bad stage stops the run, human decides where to resume)

Full design rationale: [`agent-orchestrator-overview.md`](./agent-orchestrator-overview.md)

## Project structure

```
.claude/
  agents/          # orch-product, orch-architect, orch-tester, orch-test-reviewer, orch-coder, orch-render-reviewer, orch-adr-writer
  commands/        # /run-orchestration, /resume-orchestration
  render-tools/    # pinned Playwright for render-capture.mjs (orchestrator side only)
  scripts/         # repo-digest.sh, check-contract.sh, check-paths.py, autofix.py, run-tests.sh, sandbox-timeout.py, verify.py, render-capture.mjs, run-usage.py, seed-code.py, coder-slices.py, regen.py, review-delta.py, guard-writes.sh
runs/              # one folder per run, excluded from git
memory.md          # pipeline-level lessons only
repo-notes/        # notes for target repos that don't have .claude/rules/ yet
```

Knowledge about a **target repo** lives in that repo's `.claude/rules/*.md` (and CLAUDE.md), not here. Agents read it from there, and plain Claude Code sessions in that repo pick it up too.

## Status

🚧 Prototype in progress — see [`prototype-plan.md`](./prototype-plan.md) for the build roadmap.

## License

MIT
