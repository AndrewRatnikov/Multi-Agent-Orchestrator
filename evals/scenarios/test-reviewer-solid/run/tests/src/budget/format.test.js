import { test } from 'node:test';
import assert from 'node:assert/strict';
import { formatCents } from './format.js';

test('formats cents as euros', () => {
  assert.equal(formatCents(1234), '€12.34');
  assert.equal(formatCents(5), '€0.05');
  assert.equal(formatCents(100000), '€1000.00');
});
