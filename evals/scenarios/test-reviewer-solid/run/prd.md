# PRD: Monthly total

**Run:** eval
**Date:** 2026-11-24

## Goal

The user wants to know how much they spent in a given calendar month, so the budget module gets a
function that totals one month's expenses and a formatter that shows a cents amount as euros.

## User stories

- As the user, I want the total of one month's expenses so that I know what I spent that month.
- As the user, I want amounts shown as euros so that I can read them at a glance.

## Acceptance criteria

1. `monthTotal(expenses, "2026-03")` returns the sum of `amountCents` of the expenses whose `date` starts with `2026-03`.
2. Expenses whose `date` is in any other month are not counted.
3. For a month with no expenses, `monthTotal` returns `0`.
4. `formatCents(cents)` returns the amount in euros with two decimals and the `€` sign before it, for example `formatCents(1234)` is `"€12.34"` and `formatCents(5)` is `"€0.05"`.

## Out of scope

- Showing the total on the dashboard (a later task).
- Other currencies and refunds (amounts are positive integers, per CLAUDE.md).
- Per-category totals.

## Assumptions

- `month` is a `YYYY-MM` string; dates are matched on their first 7 characters (CLAUDE.md: no time zones).

## Open questions

None.
