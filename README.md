# AI Orchestrator

A spec-driven, test-first multi-agent pipeline for Claude Code. Hand it an idea — it produces a tested, validated branch.

## How it works

```
/run-orchestration "add a Budget Summary card component"
```

The pipeline runs sequentially, with a review gate after each stage:

```
Product agent    → prd.md
Architect agent  → plan.md + Interface Contract
Tester agent     → test files (written before any code)
Test-Reviewer    → gate: are the tests any good?
Coder agent      → implementation files
Test sandbox     → ground truth: exit code only
```

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
  commands/        # slash command definitions (agent prompts)
  scripts/         # repo-digest.sh, run-tests.sh, log-cost.sh
runs/              # one folder per run, excluded from git
memory.md          # persistent conventions and decisions across runs
```

## Status

🚧 Prototype in progress — see [`prototype-plan.md`](./prototype-plan.md) for the build roadmap.

## License

MIT
