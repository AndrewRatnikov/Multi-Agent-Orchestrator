# Technical Plan: Monthly total

## Summary

Two pure functions in the budget module, each with a test file next to it.

## Files changed

| File | Action | Owner |
|---|---|---|
| src/budget/month-total.js | CREATE | coder |
| src/budget/month-total.test.js | CREATE | tester |
| src/budget/format.js | CREATE | coder |
| src/budget/format.test.js | CREATE | tester |

## Interface Contract

### Module: month-total
- `export function monthTotal(expenses, month)` in `src/budget/month-total.js`. `month` is `YYYY-MM`; returns an integer number of cents.

### Module: format
- `export function formatCents(cents)` in `src/budget/format.js`. Returns a string like `€12.34`.

## Acceptance criteria coverage

| Criterion | Test |
|---|---|
| 1 | month-total.test.js: sums the month's expenses |
| 2 | month-total.test.js: ignores other months |
| 3 | month-total.test.js: empty month is 0 |
| 4 | format.test.js: formats cents as euros |

## Test command

npm test
