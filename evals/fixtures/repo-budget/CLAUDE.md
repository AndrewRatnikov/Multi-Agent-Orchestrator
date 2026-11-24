# Pocket Budget

A personal expense tracker for one person. Eval fixture for the orchestrator's agent evals.

## Scope rules

- Amounts are stored as integer cents (`amountCents`). There is one currency, EUR, shown as `€12.34`.
- Dates are ISO `YYYY-MM-DD` strings in the user's local calendar. No time zones anywhere.
- One user per install. No accounts, no sharing, no sync.
- Expenses have `id`, `date`, `amountCents` (a positive integer) and `category`.
