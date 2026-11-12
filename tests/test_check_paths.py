"""check-paths.py: the Tester's/Coder's output files must be exactly the declared ones."""
import json
import shutil

from conftest import FIXTURES

RUN = "run_cp"
TEST_FILE = "apps/web/src/components/sets/set-progress.test.tsx"
CODE_FILES = ["apps/web/src/components/sets/set-progress.tsx", "apps/web/src/components/sets/index.ts",
              "apps/web/src/components/layout/site-nav.tsx"]


def make_run(orch, tests=(), code=(), with_contract=False):
    run_dir = orch.run_dir(RUN)
    shutil.copy(FIXTURES / "plan.md", run_dir / "plan.md")
    if with_contract:
        shutil.copy(FIXTURES / "contract.json", run_dir / "contract.json")
    for sub, files in (("tests", tests), ("code", code)):
        for rel in files:
            p = run_dir / sub / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// {}\n".format(rel))
    return run_dir


def test_tests_mode_happy(orch):
    make_run(orch, tests=[TEST_FILE])
    r = orch.run("check-paths.py", RUN, "tests")
    assert r.returncode == 0 and r.stdout.strip() == "OK: 1 tests file(s) match plan.md"


def test_tests_written_flat_instead_of_mirroring_repo(orch):
    """The main failure: tests/ must mirror the repo root."""
    make_run(orch, tests=["set-progress.test.tsx"])
    r = orch.run("check-paths.py", RUN, "tests")
    assert r.returncode == 1
    assert r.stdout.splitlines() == [
        "UNDECLARED_FILE tests/set-progress.test.tsx: not in plan.md's Files changed table. Did you mean {0}? "
        "Write it to runs/{1}/tests/{0} (tests/ mirrors the repo root).".format(TEST_FILE, RUN),
        "MISSING_FILE {}: declared as a test file in Files changed but not under runs/{}/tests/".format(TEST_FILE, RUN),
    ]


def test_prune_moves_leftover_twin(orch):
    run_dir = make_run(orch, tests=[TEST_FILE, "set-progress.test.tsx"])
    r = orch.run("check-paths.py", RUN, "tests", "--prune")
    assert r.returncode == 0
    assert r.stdout.splitlines()[0] == "PRUNED set-progress.test.tsx (leftover; the declared file is {})".format(TEST_FILE)
    assert not (run_dir / "tests" / "set-progress.test.tsx").exists()
    assert list((run_dir / "archive").glob("strays-*/set-progress.test.tsx"))


def test_code_mode_happy_with_contract_owners(orch):
    make_run(orch, tests=[TEST_FILE], code=CODE_FILES, with_contract=True)
    r = orch.run("check-paths.py", RUN, "code")
    assert r.returncode == 0, r.stdout


def test_code_mode_coder_wrote_test_and_missed_a_file(orch):
    make_run(orch, tests=[TEST_FILE], code=CODE_FILES[:2] + [TEST_FILE], with_contract=True)
    r = orch.run("check-paths.py", RUN, "code")
    assert r.returncode == 1
    assert sorted(l.split(" ", 1)[0] for l in r.stdout.splitlines()) == ["MISSING_FILE", "TEST_FILE_IN_CODE"]


def test_plan_without_table_is_setup_error(orch):
    run_dir = orch.run_dir(RUN)
    (run_dir / "plan.md").write_text("# Plan\n")
    r = orch.run("check-paths.py", RUN, "tests")
    assert r.returncode == 3 and "no `## Files changed` table" in r.stdout
