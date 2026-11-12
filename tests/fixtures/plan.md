# Plan: shared set progress block

## Files changed

| File | Action | Purpose |
|------|--------|---------|
| apps/web/src/components/sets/set-progress.tsx | CREATE | Shared `SetProgress` block |
| `apps/web/src/components/sets/index.ts` | CREATE | Barrel: re-exports `SetProgress` and its props type |
| ./apps/web/src/components/layout/site-nav.tsx | MODIFY | Link to the progress page |
| apps/web/src/components/sets/set-progress.test.tsx | CREATE | Unit tests |

## Interface Contract

See contract.json.
