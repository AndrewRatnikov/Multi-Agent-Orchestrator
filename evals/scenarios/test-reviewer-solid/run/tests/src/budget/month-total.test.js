import { test } from 'node:test';
import assert from 'node:assert/strict';
import { monthTotal } from './month-total.js';

const expenses = [
  { id: 'a', date: '2026-03-02', amountCents: 500, category: 'food' },
  { id: 'b', date: '2026-03-31', amountCents: 1999, category: 'transport' },
  { id: 'c', date: '2026-04-01', amountCents: 700, category: 'food' },
  { id: 'd', date: '2026-02-28', amountCents: 300, category: 'food' },
];

test('sums the month\'s expenses', () => {
  assert.equal(monthTotal(expenses, '2026-03'), 2499);
  assert.equal(monthTotal(expenses, '2026-04'), 700);
});

test('ignores other months', () => {
  assert.equal(monthTotal(expenses, '2026-02'), 300);
  assert.equal(monthTotal([{ id: 'x', date: '2025-03-10', amountCents: 100, category: 'food' }], '2026-03'), 0);
});

test('empty month is 0', () => {
  assert.equal(monthTotal(expenses, '2026-05'), 0);
  assert.equal(monthTotal([], '2026-03'), 0);
});
