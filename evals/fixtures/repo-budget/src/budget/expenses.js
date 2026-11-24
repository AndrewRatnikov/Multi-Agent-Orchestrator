// Expenses as the app stores them: { id, date: 'YYYY-MM-DD', amountCents: positive integer, category }.
export const sampleExpenses = [
  { id: 'e1', date: '2026-03-02', amountCents: 1250, category: 'food' },
  { id: 'e2', date: '2026-03-15', amountCents: 4999, category: 'transport' },
  { id: 'e3', date: '2026-04-01', amountCents: 800, category: 'food' },
];

export function byCategory(expenses, category) {
  return expenses.filter((e) => e.category === category);
}
