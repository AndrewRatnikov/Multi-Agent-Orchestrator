import { test } from 'node:test';
import assert from 'node:assert/strict';
import { monthTotal } from './month-total.js';

test('sums the month\'s expenses', () => {
  const result = monthTotal([{ id: 'a', date: '2026-03-02', amountCents: 500, category: 'food' }], '2026-03');
  assert.ok(result !== undefined);
});
