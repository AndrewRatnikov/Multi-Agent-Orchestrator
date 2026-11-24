1. Fails the `monthTotal` test because `assert.ok(result !== undefined)` would pass for a wrong total
   (B1 or B2), naming src/budget/month-total.test.js.
2. Flags that criteria 2 (other months) and 3 (empty month) have no test (C1), by criterion number.
3. Flags the single-value `formatCents` test as satisfiable by a constant (B3), or at least notes that
   criterion 4's second example (`formatCents(5)`) is untested.
4. Every failure tells the Tester concretely what to change.
5. Doesn't fail anything for style, naming or test framework choice.
