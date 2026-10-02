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
```

Verify's checks come from the target repo's `.claude/verify.json` (read from the original
branch, so a run can't weaken them). Database steps need Docker running, or
`VERIFY_DATABASE_URL` pointing at a local throwaway Postgres; without either, the run
stops as INCOMPLETE rather than claiming it's verified. Optional: with `NEON_API_KEY` and
`NEON_PROJECT_ID` (env or `~/.config/ai-orchestrator/neon.env`), new migrations are also
applied to a temporary Neon branch, a copy of the real data.

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
  agents/          # orch-product, orch-architect, orch-tester, orch-test-reviewer, orch-coder
  commands/        # /run-orchestration, /resume-orchestration
  scripts/         # repo-digest.sh, check-contract.sh, check-paths.py, autofix.py, run-tests.sh, verify.py, log-cost.sh, guard-writes.sh
runs/              # one folder per run, excluded from git
memory.md          # pipeline-level lessons only
repo-notes/        # notes for target repos that don't have .claude/rules/ yet
```

Knowledge about a **target repo** lives in that repo's `.claude/rules/*.md` (and CLAUDE.md), not here. Agents read it from there, and plain Claude Code sessions in that repo pick it up too.

## Status

🚧 Prototype in progress — see [`prototype-plan.md`](./prototype-plan.md) for the build roadmap.

## License

MIT
