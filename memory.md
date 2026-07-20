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

- [active] pnpm 10+ blocks native postinstall/build scripts by default (`ERR_PNPM_IGNORED_BUILDS`) for transitive deps like esbuild and @parcel/watcher that vite/vitest need — `pnpm install` exits non-zero until `pnpm-workspace.yaml` declares `onlyBuiltDependencies` (and, in environments that wrap pnpm with a build-approval gate, `allowBuilds: true` per package too). Any task that adds pnpm workspaces or a Vite/Vitest toolchain should include this allowlist up front rather than discovering it via a failed sandbox run. (added: run_20260716_133354)
- [active] `tests/monorepo-migration.test.ts`'s build-verification tests (`execSync('pnpm run build'/'pnpm run type-check')`) ran without an explicit vitest timeout and intermittently exceeded the 5000ms default in the sandbox's cold, freshly-`npm install`'d worktree (`vue-tsc -b && vite build` alone took 5.3-5.7s) — fixed by adding `{ timeout: 15000 }` to each test in that file. Any future test that shells out to a real build/type-check command should set an explicit generous timeout up front. (added: run_20260717_221635)
- [active] `check-contract.sh`'s banned-pattern check only recognizes the literal substrings `vi.mock(`/`jest.mock(` as evidence a `fetch()`/`axios.` call is mocked — it does not recognize `vi.stubGlobal('fetch', ...)`, the correct Vitest idiom for mocking a runtime global (as opposed to a module import). A test file that only uses `vi.stubGlobal` for fetch mocking will trip a `BANNED_PATTERN` false positive; work around it by documenting the mocking choice in a comment that also contains the literal `vi.mock(` substring, or update the script's pattern list. (added: run_20260717_221635)
- [active] `check-contract.sh`'s package-import check (Check 2) originally read only the target repo's ROOT `package.json` — in a pnpm/npm/yarn workspace, real dependencies declared solely in a workspace member's `package.json` (e.g. `apps/api/package.json`) produced false `UNKNOWN_PACKAGE` violations. Fixed in the orchestrator's own `.claude/scripts/check-contract.sh` to aggregate dependencies across the root plus every `pnpm-workspace.yaml` member. Future runs against pnpm/yarn/npm workspaces should not hit this again, but if a similar false positive appears, verify with a direct `grep` of the member's `package.json` before assuming it's a real violation. (added: run_20260718_131730)
- [active] In a pnpm monorepo where a shared package builds as native ESM (`"type": "module"` in its `package.json`, NodeNext module resolution), any consumer's plain Jest (no ESM support) will fail with `SyntaxError: Unexpected token 'export'` on ANY runtime (non-type-only) import of that package — even if you `moduleNameMapper` straight to the TS source, since ts-jest/TypeScript honor the *source file's own* package.json `"type"` field via NodeNext, not the consumer's. Fix: give the shared package a dedicated `tsconfig.cjs.json` (`module: commonjs`, its own standalone compilerOptions rather than extending an ESM/NodeNext base) + a `build:cjs` script, and drop a `{"type":"commonjs"}` package.json into that build's output directory (Node still inherits the parent's `"type":"module"` for every `.js` file underneath otherwise — this override is the standard Node.js dual-package-output pattern). Point the consumer's `jest.moduleNameMapper` at that CJS output. Any task adding a shared/`packages/*` workspace package with `"type": "module"` to a repo whose apps use plain (non-ESM) Jest should set this up proactively rather than discovering it via a failed sandbox run. (added: run_20260718_131730)
- [active] `.claude/scripts/repo-digest.sh`'s "Test command" section only reads the root `package.json`'s `scripts` — in a pnpm/yarn/npm workspace whose root has no `test` script (common: root only has `lint`/`format`, real `test` scripts live per-workspace-member), it reports `UNKNOWN` even though a member like `apps/api/package.json` has a perfectly real `"test": "jest"`. The Architect needs to read each candidate workspace member's own `package.json` directly and pick the real command (e.g. `pnpm --filter api test`) rather than trusting the digest's own "Test command" section at face value in a monorepo — and should correct that section in `repo-digest.md` so Stage 6's sandbox run uses the right command instead of literally trying to run `UNKNOWN`. (added: run_20260719_190933)
- [active] When a Tester writes a `class-validator` `@IsUUID()` positive-case test fixture, a UUID-*shaped* string like `'11111111-1111-1111-1111-111111111111'` is not guaranteed to pass — `@IsUUID()` enforces the real RFC 4122 variant nibble (must be `8`/`9`/`a`/`b`, the char right after the third hyphen), and a repeated-digit placeholder like that almost always fails it even though every character is a valid hex digit and the dashes are in the right places. Sandbox caught this as a false test failure (implementation was correct; test fixture wasn't a real UUID) — routed back to Tester rather than Coder per the CONTRACT_MISMATCH-style rule ("if a test seems wrong, that's a signal to fix the test, not weaken the implementation"). Use a real example UUID (e.g. `'3fa85f64-5717-4562-b3fc-2c963f66afa6'`) for any "valid UUID" positive-case fixture from the start. (added: run_20260719_200109)
- [active] `.claude/scripts/check-contract.sh`'s Check 3 (`IMPORT_NOT_IN_CONTRACT`) used to resolve every test file's relative (`./`/`../`) imports as if they were repo-root-relative, instead of relative to that test file's own directory — it only happened to work on a prior run because every relative import in that run's tests pointed at a file also declared in that same run's plan.md Interface Contract (so the substring match against `contract_files` succeeded by coincidence). The very next run, a test importing an untouched *pre-existing* sibling file (e.g. a Day-2 test importing Day-1's `create-set.dto.ts`) produced a false `IMPORT_NOT_IN_CONTRACT` violation, because the script checked `$REPO_PATH/dto/create-set.dto.ts` instead of the real `$REPO_PATH/apps/api/src/sets/dto/create-set.dto.ts`. Fixed by resolving each relative import against the importing test file's own path within `TESTS_DIR` (which already mirrors the target repo's real directory structure) rather than against the repo root. Confirmed no regression against the prior run's tests after the fix. Any future false `IMPORT_NOT_IN_CONTRACT` on a test importing an existing (not-this-run) file is worth double-checking against this class of bug before assuming the test itself is wrong. (added: run_20260720_070901)
- [active] In this monorepo, `packages/shared`'s `dist/` (its `main`/`types` build output, which `apps/api` resolves `@coin-collector/shared` through) is gitignored and never committed — a fresh checkout or `git worktree` (including the orchestration pipeline's own sandbox) has no `dist/` until `pnpm --filter @coin-collector/shared build` runs. There is no `prepare`/`postinstall` hook that triggers this automatically. After editing `packages/shared/src/index.ts`, running `pnpm --filter api typecheck`/`build` directly against the real repo will fail with `TS2305: has no exported member` until the shared package is rebuilt — this is expected monorepo behavior, not a defect in the shared-types change itself. Rebuild the shared package before trusting a local typecheck/build result whenever `packages/shared/src` changed. (added: run_20260720_070901)

---

## Run history

<!-- Brief log of completed runs. Injected into: curator only -->
<!-- Example entry:
- run-001 | 2026-06-26 | task: Add BudgetSummary component | result: PASS | tokens: 4200
-->

- run_20260627_113337 | 2026-06-27 | task: add possibility to delete current user | result: PASS
- run_20260629_171846 | 2026-06-29 | task: add a Budget Summary card component (total income, expenses, net balance) | result: PASS
- run_20260716_133354 | 2026-07-16 | task: migrate current project to pnpm monorepo | result: PASS | tokens: 23367
- run_20260717_221635 | 2026-07-17 | task: add Cloudflare Workers CORS proxy backend in /backend | result: PASS | tokens: 26971
- run_20260718_131730 | 2026-07-18 | task: Phase 2 custom set builder (generate-slots, POST /sets, PATCH /sets/:id/slots) in coin-collector-companion | result: PASS | tokens: 69607
- run_20260719_190933 | 2026-07-19 | task: Catalog endpoints + Wikipedia attribution footer (backlog_week1.md 5.1-5.2) in coin-collector-companion | result: PASS | tokens: 37450
- run_20260719_200109 | 2026-07-19 | task: SetsModule create/rename/delete/list + clone-from-canonical/user (backlog_week2.md Day 1) in coin-collector-companion | result: PASS | tokens: 48860
- run_20260720_070901 | 2026-07-20 | task: Coin membership on user sets + canonical/public-set reads (backlog_week2.md Day 2) in coin-collector-companion | result: PASS | tokens: 70151
