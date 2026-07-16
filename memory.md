# Memory Bank

Structured context for the AI Orchestrator pipeline.
Each agent receives only the section(s) relevant to its role — not the full file.
Status values: `active` | `superseded` | `retired`
Curator writes entries at the end of each successful run. Humans may edit directly.

---

## Testing conventions

<!-- Injected into: Tester, Test-Reviewer -->
<!-- Example entry:
- [active] Use data-testid, not class selectors, for Cypress/RTL queries. (added: run-001)
-->

*(empty — populated by curator after first successful run)*

---

## Architecture decisions

<!-- Injected into: Architect, Coder -->
<!-- Example entry:
- [active] Co-locate component tests in __tests__/ next to the component file. (added: run-001)
-->

*(empty — populated by curator after first successful run)*

---

## Known gotchas

<!-- Injected into: all agents -->
<!-- Example entry:
- [active] The repo uses path aliases (@/components) — never write relative ../../ imports. (added: run-002)
-->

*(empty — populated by curator after first successful run)*

---

## Run history

<!-- Brief log of completed runs. Injected into: curator only -->
<!-- Example entry:
- run-001 | 2026-06-26 | task: Add BudgetSummary component | result: PASS | tokens: 4200
-->

- run_20260627_113337 | 2026-06-27 | task: add possibility to delete current user | result: PASS
- run_20260629_171846 | 2026-06-29 | task: add a Budget Summary card component (total income, expenses, net balance) | result: PASS
