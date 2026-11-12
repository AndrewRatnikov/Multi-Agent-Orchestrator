"""review-delta.py: delta vs full re-review of the tests, and the PASS stamp."""
from conftest import commit

RUN = "run_rd"
TEST = "tests/totals.test.ts"
BODY = "".join("it('case {0}', () => expect(total({0})).toBe({0}))\n".format(i) for i in range(30))


def make_run(orch, make_repo):
    repo = make_repo({TEST: BODY, "src/total.ts": "export const total = (n: number) => n\n"})
    run_dir = orch.run_dir(RUN)
    (run_dir / "tests" / "tests").mkdir(parents=True)
    (run_dir / "tests" / TEST).write_text(BODY)
    return run_dir, repo


def rd(orch, repo, *args):
    r = orch.run("review-delta.py", RUN, repo, *args)
    return r.returncode, r.stdout.strip()


def test_first_review_is_full(orch, make_repo):
    _, repo = make_run(orch, make_repo)
    assert rd(orch, repo, "diff") == (1, "review-delta: full review (no earlier review in this run)")


def test_pass_then_no_changes_needs_no_review(orch, make_repo):
    run_dir, repo = make_run(orch, make_repo)
    code, out = rd(orch, repo, "mark", "PASS")
    assert code == 0 and "stamped .test_reviewer_passed_at" in out
    assert (run_dir / ".test_reviewer_passed_at").exists()
    commit(repo, {"src/total.ts": "export const total = (n: number) => n + 0\n"})   # code only
    code, out = rd(orch, repo, "diff")
    assert code == 2 and "no review needed" in out


def test_small_test_change_gets_delta_review(orch, make_repo):
    run_dir, repo = make_run(orch, make_repo)
    rd(orch, repo, "mark", "FAIL")
    assert not (run_dir / ".test_reviewer_passed_at").exists()
    changed = BODY.replace("toBe(3)", "toBe(3) // fixed")
    commit(repo, {TEST: changed})
    (run_dir / "tests" / TEST).write_text(changed)
    code, out = rd(orch, repo, "diff")
    assert code == 0 and "+1 -1 lines since the FAIL review" in out
    assert "toBe(3) // fixed" in (run_dir / "review-delta.diff").read_text()


def test_large_test_rewrite_gets_full_review(orch, make_repo):
    """The main failure path: too much changed to judge from a diff."""
    run_dir, repo = make_run(orch, make_repo)
    rd(orch, repo, "mark", "PASS")
    rewritten = BODY.replace("expect(", "assert.equal(")
    commit(repo, {TEST: rewritten})
    (run_dir / "tests" / TEST).write_text(rewritten)
    code, out = rd(orch, repo, "diff")
    assert code == 1 and out.startswith("review-delta: full review (200% of all test lines changed")
    assert not (run_dir / "review-delta.diff").exists()
