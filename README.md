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

Every stage writes its artifact to disk. A failed stage stops the run and writes a report. You can resume from any step:

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
