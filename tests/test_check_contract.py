"""check-contract.py: validate, tests and code modes, run as the pipeline runs them (CLI)."""
import copy
import json
import os
import time

import pytest

from conftest import FIXTURES, load_fixture_json

RUN = "run_cc"
WEB = "apps/web/src/components"

REPO_FILES = {
    "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n",
    "package.json": json.dumps({"name": "root", "private": True, "devDependencies": {"typescript": "5.6.0"}}),
    "apps/web/package.json": json.dumps({
        "name": "web",
        "dependencies": {"react": "18.3.1"},
        "devDependencies": {"vitest": "2.1.0", "@testing-library/react": "16.0.0"},
    }),
    # tsconfig with a comment and a trailing comma, as real ones have
    "apps/web/tsconfig.json": '{\n  // path aliases\n  "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]},},\n}\n',
    WEB + "/layout/site-nav.tsx": 'export function SiteNav() {\n  return <nav data-testid="site-nav" />\n}\n',
    WEB + "/catalog/filters.tsx": "export function Filters({ prefix }: { prefix: string }) {\n"
                                  "  return <select data-testid={`${prefix}-filter-country`} />\n}\n",
    "apps/web/src/lib/money.ts": "export const formatMoney = (n: number) => n.toFixed(2)\n",
}

CLEAN_TEST = """\
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SetProgress } from '@/components/sets'
import { SiteNav } from '../layout/site-nav'
import type { Props } from './helpers'

vi.mock('@/lib/money', () => ({ formatMoney: () => <span data-testid="mock-chart" /> }))

describe('SetProgress', () => {
  it('renders', () => {
    render(<SetProgress owned={1} total={2} />)
    expect(screen.getByTestId('set-progress')).toBeTruthy()
    expect(screen.getByTestId("site-nav")).toBeTruthy()
    expect(screen.getByTestId(`catalog-filter-country`)).toBeTruthy()
    const prefix = 'catalog'
    expect(screen.getAllByTestId(`${prefix}-filter-country`)).toHaveLength(1)
    expect(screen.queryByTestId('language-switcher-en')).toBeNull()
    // screen.getByTestId('commented-out') is ignored
  })
})
"""
# Test helper with a type-only re-export: must be parsed as an import and resolved.
HELPERS = "export type { SetProgressProps as Props } from './set-progress'\n"

CLEAN_CODE = {
    WEB + "/sets/set-progress.tsx": (
        "export type SetProgressProps = { owned: number; total: number }\n"
        "export function SetProgress({ owned, total }: SetProgressProps) {\n"
        '  return <div data-testid="set-progress">{owned}/{total}</div>\n}\n'),
    WEB + "/sets/index.ts": "export { SetProgress } from './set-progress'\nexport type { SetProgressProps } from './set-progress'\n",
    WEB + "/layout/site-nav.tsx": 'export function SiteNav() {\n  return <nav data-testid="site-nav"><a href="/sets">Sets</a></nav>\n}\n',
}


@pytest.fixture
def setup(orch, make_repo):
    """setup(contract=None, plan=None, tests=None, code=None, repo_files=None) -> (run_dir, repo)."""
    def make(contract=None, plan=None, tests=None, code=None, repo_files=None):
        repo = make_repo(repo_files or REPO_FILES)
        run_dir = orch.run_dir(RUN)
        (run_dir / "contract.json").write_text(json.dumps(contract or load_fixture_json("contract.json")))
        (run_dir / "plan.md").write_text(plan if plan is not None else (FIXTURES / "plan.md").read_text())
        for sub, files in (("tests", tests), ("code", code)):
            if files is not None:
                for rel, content in files.items():
                    p = run_dir / sub / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content)
        return run_dir, repo
    return make


def run(orch, repo, mode):
    r = orch.run("check-contract.py", RUN, repo, mode)
    return r.returncode, r.stdout.strip().splitlines()


def contract(mutate):
    c = copy.deepcopy(load_fixture_json("contract.json"))
    mutate(c)
    return c


def ids(lines):
    return sorted({l.split(":", 1)[0] for l in lines})


# ── validate ─────────────────────────────────────────────────────────────────

def test_validate_clean(orch, setup):
    _, repo = setup()
    code, out = run(orch, repo, "validate")
    assert code == 0, out
    assert out == ["check-contract validate: clean ({})".format(RUN)]


def test_validate_create_file_exists(orch, setup):
    def m(c):
        c["files"][2]["action"] = "create"   # site-nav.tsx is already in the repo
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1
    assert out == ["CREATE_FILE_EXISTS: {}/layout/site-nav.tsx is marked create but already exists "
                   "in the repo (use modify)".format(WEB)]


def test_validate_modify_file_missing(orch, setup):
    def m(c):
        c["files"][0]["action"] = "modify"
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1 and ids(out) == ["MODIFY_FILE_MISSING"]


def test_validate_existing_testid_must_be_in_its_file(orch, setup):
    def m(c):
        c["testids"]["existing"].append({"id": "site-header", "file": WEB + "/layout/site-nav.tsx"})
        c["testids"]["existing"].append({"id": "footer", "file": WEB + "/layout/footer.tsx"})
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1
    assert ids(out) == ["EXISTING_TESTID_FILE_MISSING", "EXISTING_TESTID_NOT_FOUND"]
    assert any("site-header" in l for l in out)


def test_validate_template_file_unknown(orch, setup):
    def m(c):
        c["testids"]["templates"][0]["file"] = WEB + "/catalog/nowhere.tsx"
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1 and ids(out) == ["TEMPLATE_FILE_UNKNOWN"]


def test_validate_plan_table_drift(orch, setup):
    plan = (FIXTURES / "plan.md").read_text().replace(
        "| apps/web/src/components/sets/set-progress.test.tsx |",
        "| apps/web/src/lib/extra.ts | CREATE | not in contract |\n| apps/web/src/components/sets/set-progress.test.tsx |")
    _, repo = setup(plan=plan)
    code, out = run(orch, repo, "validate")
    assert code == 1
    assert out == ["CONTRACT_DRIFT: apps/web/src/lib/extra.ts is in plan.md's Files changed table but not in contract.json files"]


def test_validate_plan_without_table(orch, setup):
    _, repo = setup(plan="# Plan\n\nno table\n")
    code, out = run(orch, repo, "validate")
    assert code == 1 and "PLAN_TABLE_MISSING" in ids(out)


def _with_package_json(c, added):
    c["packages"]["added"] = added
    c["files"].append({"path": "apps/web/package.json", "action": "modify", "owner": "coder"})
    c["commits"][0]["files"].append("apps/web/package.json")


PLAN_WITH_PKG = (FIXTURES / "plan.md").read_text().replace(
    "| apps/web/src/components/sets/set-progress.test.tsx |",
    "| apps/web/package.json | MODIFY | dependency |\n| apps/web/src/components/sets/set-progress.test.tsx |")


def test_validate_packages_added_ok(orch, setup):
    _, repo = setup(contract(lambda c: _with_package_json(c, ["zod"])), plan=PLAN_WITH_PKG)
    code, out = run(orch, repo, "validate")
    assert code == 0, out


def test_validate_packages_added_already_present(orch, setup):
    _, repo = setup(contract(lambda c: _with_package_json(c, ["react", "zod"])), plan=PLAN_WITH_PKG)
    code, out = run(orch, repo, "validate")
    assert code == 1
    assert out == ["PACKAGE_ALREADY_PRESENT: react is listed in packages.added but is already a dependency"]


def test_validate_packages_added_without_package_json(orch, setup):
    def m(c):
        c["packages"]["added"] = ["zod"]
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1 and ids(out) == ["PACKAGE_JSON_NOT_IN_CONTRACT"]


def test_validate_commit_grouping(orch, setup):
    def m(c):
        c["commits"] = [{"message": "add progress", "files": [WEB + "/sets/set-progress.tsx",
                                                              WEB + "/sets/set-progress.test.tsx"]}]
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1
    assert ids(out) == ["COMMIT_FILE_MISSING", "COMMIT_HAS_TEST_FILE", "COMMIT_MESSAGE_FORMAT"]


# ── render targets for UI changes ────────────────────────────────────────────

def _no_render(c):
    c.pop("render_targets_none_reason", None)
    c.pop("render_skip_reason", None)


def test_validate_ui_without_targets_or_reason(orch, setup):
    _, repo = setup(contract(_no_render))
    code, out = run(orch, repo, "validate")
    assert code == 1 and ids(out) == ["RENDER_TARGETS_MISSING"]
    # .tsx files listed, the barrel .ts is not a UI file
    assert WEB + "/sets/set-progress.tsx" in out[0] and WEB + "/layout/site-nav.tsx" in out[0]
    assert "sets/index.ts" not in out[0]
    assert "render_targets_none_reason" in out[0]


def test_validate_legacy_skip_reason_is_not_an_opt_out(orch, setup):
    def m(c):
        _no_render(c)
        c["render_skip_reason"] = "the repo has no render config"
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1 and ids(out) == ["RENDER_TARGETS_MISSING"]
    assert "render_skip_reason no longer counts" in out[0] and "no render config" in out[0]


def test_validate_targets_and_none_reason_conflict(orch, setup):
    def m(c):
        c["render_targets"] = [{"id": "sets", "route": "/sets", "state": "typical", "ready": "set-progress"}]
    plan = (FIXTURES / "plan.md").read_text() + "\n## Render targets\n\n| sets | /sets | typical |\n"
    _, repo = setup(contract(m), plan=plan)
    code, out = run(orch, repo, "validate")
    assert code == 1 and ids(out) == ["RENDER_TARGETS_AND_NONE_REASON"]


def test_validate_none_reason_must_be_string(orch, setup):
    def m(c):
        c["render_targets_none_reason"] = True
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1 and out == ["SCHEMA: `render_targets_none_reason` must be a string"]


def _api_only(c):
    """Only a .ts lib file is coder-owned; a .test.tsx and a __tests__ .tsx don't count as UI."""
    _no_render(c)
    c["files"] = [
        {"path": "apps/web/src/lib/progress.ts", "action": "create", "owner": "coder"},
        {"path": "apps/web/src/lib/__tests__/fixture.tsx", "action": "create", "owner": "coder"},
        {"path": WEB + "/sets/set-progress.test.tsx", "action": "create", "owner": "tester"},
    ]
    c["exports"], c["testids"]["new"], c["testids"]["templates"] = [], [], []
    c["commits"] = [{"message": "feat(sets): progress helper",
                     "files": ["apps/web/src/lib/progress.ts", "apps/web/src/lib/__tests__/fixture.tsx"]}]


PLAN_API = (FIXTURES / "plan.md").read_text().split("## Files changed")[0] + (
    "## Files changed\n\n| File | Action | Purpose |\n|---|---|---|\n"
    "| apps/web/src/lib/progress.ts | CREATE | helper |\n"
    "| apps/web/src/lib/__tests__/fixture.tsx | CREATE | fixture |\n"
    "| apps/web/src/components/sets/set-progress.test.tsx | CREATE | tests |\n")


def test_validate_api_only_needs_no_targets(orch, setup):
    _, repo = setup(contract(_api_only), plan=PLAN_API)
    code, out = run(orch, repo, "validate")
    assert code == 0, out


def _verify_json_on_main(repo, section, ui_paths):
    from conftest import commit
    commit(repo, {".claude/verify.json": json.dumps({section: {"ui_paths": ui_paths}})}, "verify config")


@pytest.mark.parametrize("section", ["render", "sizing"])
def test_validate_repo_ui_paths_override(orch, setup, section):
    run_dir, repo = setup(contract(_no_render))
    (run_dir / "state.md").write_text("run_id: {}\noriginal_branch: main\n".format(RUN))
    # Only layout/ counts as UI in this repo: site-nav.tsx is still caught, set-progress.tsx isn't.
    _verify_json_on_main(repo, section, ["apps/web/src/components/layout/**"])
    code, out = run(orch, repo, "validate")
    assert code == 1 and ids(out) == ["RENDER_TARGETS_MISSING"]
    assert "layout/site-nav.tsx" in out[0] and "set-progress.tsx" not in out[0]
    # An empty list: the repo has no UI files.
    _verify_json_on_main(repo, section, [])
    code, out = run(orch, repo, "validate")
    assert code == 0, out


def test_schema_error_stops_before_checks(orch, setup):
    def m(c):
        c["testids"]["new"].append({"id": "site-nav", "file": WEB + "/sets/set-progress.tsx"})
    _, repo = setup(contract(m))
    code, out = run(orch, repo, "validate")
    assert code == 1
    assert out == ["SCHEMA: testid site-nav is in both testids.new and testids.existing"]


def test_missing_contract_is_setup_error(orch, setup):
    run_dir, repo = setup()
    (run_dir / "contract.json").unlink()
    code, out = run(orch, repo, "validate")
    assert code == 3 and out[0].startswith("SETUP_ERROR")


# ── tests mode ───────────────────────────────────────────────────────────────

TEST_PATH = WEB + "/sets/set-progress.test.tsx"
HELPERS_PATH = WEB + "/sets/helpers.ts"


def test_tests_clean(orch, setup):
    """Declared new/existing ids, a template instance, a runtime template (`${prefix}`),
    a test_only id, a removed id asserted absent, an alias import resolving to a contract
    file, a relative import of a repo file and an `export type { X } from` re-export."""
    _, repo = setup(tests={TEST_PATH: CLEAN_TEST, HELPERS_PATH: HELPERS})
    code, out = run(orch, repo, "tests")
    assert code == 0, out


@pytest.mark.parametrize("snippet, expected", [
    ("screen.getByTestId('set-progress-bar')", 'uses data-testid "set-progress-bar"'),
    # a template instance the contract doesn't list
    ("screen.getByTestId('catalog-filter-region')", 'uses data-testid "catalog-filter-region"'),
    ('render(<div data-testid="undeclared-wrapper" />)', 'uses data-testid "undeclared-wrapper"'),
])
def test_tests_undeclared_testid(orch, setup, snippet, expected):
    src = CLEAN_TEST.replace("    // screen.getByTestId('commented-out') is ignored", "    " + snippet)
    _, repo = setup(tests={TEST_PATH: src, HELPERS_PATH: HELPERS})
    code, out = run(orch, repo, "tests")
    assert code == 1
    assert len(out) == 1 and out[0].startswith("TESTID_NOT_IN_CONTRACT: " + TEST_PATH) and expected in out[0]


def test_tests_type_reexport_from_missing_module(orch, setup):
    helpers = "export type { Gone } from './does-not-exist'\n"
    _, repo = setup(tests={TEST_PATH: CLEAN_TEST, HELPERS_PATH: helpers})
    code, out = run(orch, repo, "tests")
    assert code == 1
    assert len(out) == 1
    assert out[0].startswith("IMPORT_NOT_RESOLVED: {} imports './does-not-exist'".format(HELPERS_PATH))


def test_tests_unknown_package(orch, setup):
    src = "import { http } from 'msw'\n" + CLEAN_TEST
    _, repo = setup(tests={TEST_PATH: src, HELPERS_PATH: HELPERS})
    code, out = run(orch, repo, "tests")
    assert code == 1 and ids(out) == ["UNKNOWN_PACKAGE"]


def test_tests_package_from_packages_added_is_known(orch, setup):
    def m(c):
        c["packages"]["added"] = ["msw"]
    src = "import { http } from 'msw'\nimport { readFile } from 'node:fs/promises'\n" + CLEAN_TEST
    _, repo = setup(contract(m), tests={TEST_PATH: src, HELPERS_PATH: HELPERS})
    code, out = run(orch, repo, "tests")
    assert code == 0, out


def test_tests_unmocked_fetch_is_banned(orch, setup):
    src = CLEAN_TEST.replace("    // screen.getByTestId('commented-out') is ignored", "    await fetch('/api/sets')")
    src = src.replace("vi.mock(", "vi.fn(")   # no module mock in the file any more
    _, repo = setup(tests={TEST_PATH: src, HELPERS_PATH: HELPERS})
    code, out = run(orch, repo, "tests")
    assert code == 1 and ids(out) == ["BANNED_PATTERN"]


# ── code mode ────────────────────────────────────────────────────────────────

def test_code_clean_with_type_reexport(orch, setup):
    _, repo = setup(code=CLEAN_CODE)
    code, out = run(orch, repo, "code")
    assert code == 0, out


def test_code_multiline_type_reexport(orch, setup):
    files = dict(CLEAN_CODE)
    files[WEB + "/sets/index.ts"] = "export { SetProgress } from './set-progress'\nexport type {\n  SetProgressProps,\n} from './set-progress'\n"
    _, repo = setup(code=files)
    code, out = run(orch, repo, "code")
    assert code == 0, out


def test_code_missing_type_reexport(orch, setup):
    files = dict(CLEAN_CODE)
    files[WEB + "/sets/index.ts"] = "export { SetProgress } from './set-progress'\n"
    _, repo = setup(code=files)
    code, out = run(orch, repo, "code")
    assert code == 1
    assert out == ["MISSING_EXPORT: {}/sets/index.ts doesn't export SetProgressProps".format(WEB)]


@pytest.mark.xfail(strict=True, reason="known gap: `export { X as Y }` is counted as exporting X (not fixed)")
def test_code_renamed_export_does_not_count_as_original_name(orch, setup):
    files = dict(CLEAN_CODE)
    files[WEB + "/sets/index.ts"] = ("export { SetProgress } from './set-progress'\n"
                                     "export type { SetProgressProps as Props } from './set-progress'\n")
    _, repo = setup(code=files)
    code, out = run(orch, repo, "code")
    assert code == 1


def test_code_new_testid_missing(orch, setup):
    files = dict(CLEAN_CODE)
    files[WEB + "/sets/set-progress.tsx"] = files[WEB + "/sets/set-progress.tsx"].replace('"set-progress"', '"progress"')
    _, repo = setup(code=files)
    code, out = run(orch, repo, "code")
    assert code == 1
    assert out == ["MISSING_TESTID_IN_CODE: testid set-progress isn't in {}/sets/set-progress.tsx "
                   "(expected the literal string)".format(WEB)]


def test_code_existing_testid_removed(orch, setup):
    files = dict(CLEAN_CODE)
    files[WEB + "/layout/site-nav.tsx"] = "export function SiteNav() {\n  return <nav />\n}\n"
    _, repo = setup(code=files)
    code, out = run(orch, repo, "code")
    assert code == 1 and ids(out) == ["EXISTING_TESTID_REMOVED"]


def test_code_template_kept_with_other_expression(orch, setup):
    files = dict(CLEAN_CODE)
    files[WEB + "/catalog/filters.tsx"] = "export const F = ({ scope }: { scope: string }) => " \
                                          "<select data-testid={scope + '-filter-country'} />\n"
    _, repo = setup(code=files)
    code, out = run(orch, repo, "code")
    assert code == 0, out


def test_code_template_dropped(orch, setup):
    files = dict(CLEAN_CODE)
    files[WEB + "/catalog/filters.tsx"] = "export const F = () => <select data-testid=\"filter\" />\n"
    _, repo = setup(code=files)
    code, out = run(orch, repo, "code")
    assert code == 1
    assert len(out) == 1 and out[0].startswith("MISSING_TESTID_IN_CODE: template {prefix}-filter-country not found")


def test_code_removed_and_test_only_ids_need_no_code(orch, setup):
    # language-switcher-en (removed) and mock-chart (test_only) appear nowhere in code/.
    _, repo = setup(code=CLEAN_CODE)
    code, out = run(orch, repo, "code")
    assert code == 0, out


def test_tests_modified_after_review(orch, setup):
    run_dir, repo = setup(tests={TEST_PATH: CLEAN_TEST, HELPERS_PATH: HELPERS}, code=CLEAN_CODE)
    sentinel = run_dir / ".test_reviewer_passed_at"
    sentinel.touch()
    stamp = time.time() - 100
    os.utime(sentinel, (stamp, stamp))
    for rel in (TEST_PATH, HELPERS_PATH):
        os.utime(run_dir / "tests" / rel, (stamp - 50, stamp - 50))
    code, out = run(orch, repo, "code")
    assert code == 0, out

    os.utime(run_dir / "tests" / TEST_PATH, (stamp + 50, stamp + 50))
    code, out = run(orch, repo, "code")
    assert code == 1
    assert out == ["TESTS_MODIFIED_AFTER_REVIEW: tests/{} changed after the test-reviewer approved".format(TEST_PATH)]
