# req-lab: notes pending a move into the repo

Target repo: `/Users/andrewratnikov/Projects/req-lab`. Move these into `req-lab/.claude/rules/` next time you work there, then delete this file.

- pnpm 10+ blocks native postinstall/build scripts by default (`ERR_PNPM_IGNORED_BUILDS`) for transitive deps like esbuild and `@parcel/watcher` that vite/vitest need. `pnpm-workspace.yaml` must declare `onlyBuiltDependencies`, and `allowBuilds: true` per package in environments that gate builds. (run_20260716_133354)
- `tests/monorepo-migration.test.ts` build-verification tests (`execSync('pnpm run build' / 'pnpm run type-check')`) need `{ timeout: 15000 }`, because `vue-tsc -b && vite build` alone takes 5.3–5.7s in a cold worktree. (run_20260717_221635)
