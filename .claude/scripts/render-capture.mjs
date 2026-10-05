#!/usr/bin/env node
/**
 * render-capture.mjs: mechanical half of the pipeline's Render review stage (Stage 6c).
 *
 * Opens the pages a run changed in a real browser and saves screenshots, visible text,
 * console errors and a few mechanical facts, for the branch and for its original branch,
 * so the render-reviewer gets before/after pairs. It judges nothing.
 *
 * Usage:
 *   node render-capture.mjs <repo_path> <run_id> [--config-from REF] [--only after|before]
 *
 * What it reads:
 *   runs/{RUN_ID}/contract.json   render_targets (declared by the Architect)
 *   runs/{RUN_ID}/state.md        target_branch, original_branch, optional render_config_ref
 *   {REF}:.claude/verify.json     "render" section, via `git show` (REF = --config-from, else
 *                                 state.md render_config_ref, else original_branch), so a run
 *                                 can't edit its own render config
 *   {REF}:.claude/launch.json     the dev-server command and port
 *   {REF}:<render.setup>          repo-owned hook module (sign-in stub, unlock, seed); its
 *                                 whole directory is extracted from REF, never from the branch
 *
 * Setup hook contract (ES module, all optional except where noted):
 *   start({port})            -> {env, cookies, stop}   before the dev server starts
 *   seed({page, fixture, now}) fresh context at "/" before any data exists
 *   unlock({page})           idempotent; called after every navigation
 *
 * Everything runs in throwaway git worktrees under $TMPDIR; the user's checkout is never
 * touched. Dev servers and stubs run in their own process group and are killed on success,
 * failure, SIGINT/SIGTERM and a hard timeout. Playwright comes from the pinned
 * .claude/render-tools/ directory (orchestrator side only; never a target-repo dependency).
 *
 * Exit codes:
 *   0 CAPTURED    every target was reached on the branch; artifacts are ready for the reviewer
 *   1 INCOMPLETE  at least one target could not be reached (redirect, ready element never
 *                 appeared, unknown fixture); the stage must not be reported as PASS
 *   2 SKIPPED     no render config in the repo, or no targets declared
 *   3 ERROR       infrastructure (worktree, install, dev server, browser)
 */

import { spawn, spawnSync } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ORCH_ROOT = path.resolve(HERE, '..', '..')
const TOOLS_DIR = path.join(ORCH_ROOT, '.claude', 'render-tools')

const DEFAULT_VIEWPORTS = { desktop: [1280, 800], mobile: [390, 844] }
const MAX_SHOT_HEIGHT = 5000

// ── args ─────────────────────────────────────────────────────────────────────

const argv = process.argv.slice(2)
const flag = (name) => {
  const i = argv.indexOf(name)
  return i >= 0 ? argv[i + 1] : undefined
}
const positional = argv.filter((a, i) => !a.startsWith('--') && !(i > 0 && argv[i - 1].startsWith('--')))
if (positional.length < 2) {
  console.error('usage: render-capture.mjs <repo_path> <run_id> [--config-from REF] [--only after|before]')
  process.exit(3)
}
const REPO = path.resolve(positional[0])
const RUN_ID = positional[1]
const RUN_DIR = path.join(ORCH_ROOT, 'runs', RUN_ID)
const OUT = path.join(RUN_DIR, 'render')
const ONLY = flag('--only')

// ── helpers ──────────────────────────────────────────────────────────────────

class Skip extends Error {}
class Infra extends Error {}

const log = (...a) => console.log('[render]', ...a)

function git(...args) {
  const r = spawnSync('git', ['-C', REPO, ...args], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
  return { code: r.status, out: (r.stdout || '').trim(), err: (r.stderr || '').trim() }
}

function gitShow(ref, file) {
  const r = git('show', `${ref}:${file}`)
  return r.code === 0 ? r.out : null
}

function sh(cmd, opts = {}) {
  const r = spawnSync('bash', ['-c', cmd], { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024, ...opts })
  return { code: r.status, out: (r.stdout || '') + (r.stderr || '') }
}

function readState() {
  const state = {}
  const p = path.join(RUN_DIR, 'state.md')
  if (fs.existsSync(p)) {
    for (const line of fs.readFileSync(p, 'utf8').split('\n')) {
      const i = line.indexOf(':')
      if (i > 0) state[line.slice(0, i).trim()] = line.slice(i + 1).trim()
    }
  }
  return state
}

const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'x'
const sha1 = (s) => crypto.createHash('sha1').update(s).digest('hex')

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer()
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address()
      s.close(() => resolve(port))
    })
    s.on('error', reject)
  })
}

// ── cleanup registry: everything started is stopped, whatever happens ────────

const cleanups = []
// Runs (and removes) cleanups registered after `downTo`, newest first. A side (branch or
// original) uses its own scope so its server, stub, browser and worktree are gone before
// the next side starts; the final call with 0 tears down everything that is left.
async function runCleanups(downTo = 0) {
  while (cleanups.length > downTo) {
    const fn = cleanups.pop()
    try {
      await fn()
    } catch (e) {
      console.error('[render] cleanup error:', e.message)
    }
  }
}
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => {
    await runCleanups()
    process.exit(3)
  })
}
process.on('exit', () => {
  // Synchronous last resort: kill any process group still registered.
  for (const pid of killOnExit) {
    try {
      process.kill(-pid, 'SIGKILL')
    } catch {}
  }
})
const killOnExit = new Set()

// ── worktree + install + dev server ──────────────────────────────────────────

function addWorktree(ref, label) {
  const wt = path.join(os.tmpdir(), `orchestrator-render-${slug(RUN_ID)}-${label}`)
  if (fs.existsSync(wt)) {
    git('worktree', 'remove', '--force', wt)
    fs.rmSync(wt, { recursive: true, force: true })
  }
  const r = git('worktree', 'add', '--detach', wt, ref)
  if (r.code !== 0) throw new Infra(`git worktree add ${ref} failed: ${r.err}`)
  cleanups.push(() => {
    git('worktree', 'remove', '--force', wt)
    fs.rmSync(wt, { recursive: true, force: true })
    git('worktree', 'prune')
  })
  return wt
}

function installCommand(wt, renderCfg, verifyCfg) {
  if (renderCfg.install) return renderCfg.install
  if (verifyCfg.install) return verifyCfg.install
  if (fs.existsSync(path.join(wt, 'pnpm-lock.yaml'))) return 'pnpm install --frozen-lockfile'
  if (fs.existsSync(path.join(wt, 'yarn.lock'))) return 'yarn install --frozen-lockfile'
  return 'npm ci'
}

function prepareDeps(wt, renderCfg, verifyCfg) {
  const cmd = installCommand(wt, renderCfg, verifyCfg)
  log('install:', cmd)
  const env = { ...process.env, CI: 'true' }
  const r = sh(cmd, { cwd: wt, env, timeout: (renderCfg.installTimeoutSec || 900) * 1000 })
  if (r.code !== 0) throw new Infra(`install failed (${cmd}):\n${r.out.split('\n').slice(-25).join('\n')}`)
}

async function waitForHttp(port, timeoutMs, child) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Infra('dev server exited before it was ready')
    try {
      const r = await fetch(`http://localhost:${port}/`, { signal: AbortSignal.timeout(5000) })
      if (r.status) return
    } catch {}
    await new Promise((r) => setTimeout(r, 1000))
  }
  throw new Infra(`dev server not ready after ${timeoutMs / 1000}s`)
}

async function startDevServer(wt, launch, extraEnv, renderCfg, logPath) {
  const port = await freePort()
  const portArgs = (renderCfg.portArgs || ['--', '--port', '{port}', '--strictPort']).map((a) => a.replace('{port}', String(port)))
  const args = [...(launch.runtimeArgs || []), ...portArgs]
  log(`dev server: ${launch.runtimeExecutable} ${args.join(' ')} (cwd ${wt})`)
  fs.mkdirSync(path.dirname(logPath), { recursive: true })
  const out = fs.openSync(logPath, 'w')
  const child = spawn(launch.runtimeExecutable, args, {
    cwd: wt,
    detached: true,
    stdio: ['ignore', out, out],
    env: { ...process.env, ...(launch.env || {}), ...extraEnv, PORT: String(port), BROWSER: 'none', CI: '1' },
  })
  killOnExit.add(child.pid)
  cleanups.push(async () => {
    try {
      process.kill(-child.pid, 'SIGTERM')
    } catch {}
    await new Promise((r) => setTimeout(r, 1500))
    try {
      process.kill(-child.pid, 'SIGKILL')
    } catch {}
    killOnExit.delete(child.pid)
  })
  await waitForHttp(port, (renderCfg.readyTimeoutSec || 120) * 1000, child)
  return `http://localhost:${port}`
}

// ── per-target capture ───────────────────────────────────────────────────────

function textFacts(text) {
  const counts = new Map()
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\s+/g, ' ').trim()
    if (line.length < 3) continue
    const k = line.toLowerCase()
    counts.set(k, { line, n: (counts.get(k)?.n || 0) + 1 })
  }
  return [...counts.values()].filter((x) => x.n > 1).sort((a, b) => b.n - a.n).slice(0, 30)
}

async function captureTarget({ browser, baseURL, hook, hookState, target, vpName, vp, renderCfg, side, outDir, fixtures }) {
  const id = `${target.id}.${vpName}`
  const facts = { target: target.id, viewport: vpName, side, route: target.route, state: target.state, fixture: target.fixture || null }
  if (target.fixture && !(target.fixture in fixtures)) {
    return { ...facts, reached: false, reason: `fixture "${target.fixture}" is not defined in the repo's render.fixtures` }
  }
  const ctx = await browser.newContext({
    viewport: { width: vp[0], height: vp[1] },
    serviceWorkers: 'block',
    timezoneId: renderCfg.timezone || 'UTC',
    deviceScaleFactor: 1,
    isMobile: vpName === 'mobile',
    hasTouch: vpName === 'mobile',
  })
  try {
    if (hookState?.cookies?.length) await ctx.addCookies(hookState.cookies)
    if (renderCfg.now) await ctx.clock.setFixedTime(new Date(renderCfg.now))
    const page = await ctx.newPage()
    const consoleErrors = []
    page.on('console', (m) => {
      if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 500))
    })
    page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e.message).slice(0, 500) + ' @ ' + String(e.stack || '').split('\n').slice(1, 3).map((l) => l.trim()).join(' | ').slice(0, 300)))

    await page.goto(baseURL + '/', { waitUntil: 'domcontentloaded', timeout: 60000 })
    if (target.fixture && hook.seed) await hook.seed({ page, fixture: target.fixture, now: renderCfg.now || new Date().toISOString() })
    if (hook.unlock) await hook.unlock({ page })
    const wantPath = new URL(target.route, baseURL)
    if (wantPath.pathname !== '/' || wantPath.search) {
      await page.goto(baseURL + target.route, { waitUntil: 'domcontentloaded', timeout: 60000 })
      if (hook.unlock) await hook.unlock({ page })
    }
    await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {})

    // `ready` is a testid the run may be adding, so it can't exist on the original branch:
    // there it is only waited for briefly and never decides "reached".
    let readyOk = true
    if (target.ready) {
      const seen = await page
        .waitForSelector(`[data-testid="${target.ready}"]`, { timeout: side === 'after' ? 20000 : 3000 })
        .then(() => true)
        .catch(() => false)
      if (side === 'after') readyOk = seen
    }
    // Dev-only overlays (e.g. framework devtools buttons) are not part of the product's UI.
    if (renderCfg.hideSelectors?.length) {
      await page.addStyleTag({ content: renderCfg.hideSelectors.map((sel) => `${sel}{display:none!important}`).join('\n') })
    }
    await page.waitForTimeout(600) // let animations and late queries settle

    const finalUrl = new URL(page.url())
    const samePath = finalUrl.pathname.replace(/\/$/, '') === wantPath.pathname.replace(/\/$/, '')
    const text = await page.evaluate(() => document.body.innerText)
    const overflow = await page.evaluate(() => {
      const w = window.innerWidth
      const wide = []
      for (const el of document.querySelectorAll('body *')) {
        const r = el.getBoundingClientRect()
        if (r.width > 0 && r.right > w + 1) wide.push((el.getAttribute('data-testid') || el.tagName.toLowerCase()) + ` right=${Math.round(r.right)}`)
        if (wide.length >= 8) break
      }
      return { scrollWidth: document.documentElement.scrollWidth, innerWidth: w, overflowsX: document.documentElement.scrollWidth > w + 1, wideElements: wide }
    })
    const height = await page.evaluate(() => document.documentElement.scrollHeight)
    await page.screenshot({
      path: path.join(outDir, `${id}.png`),
      fullPage: true,
      clip: { x: 0, y: 0, width: vp[0], height: Math.min(height, MAX_SHOT_HEIGHT) },
    })
    fs.writeFileSync(path.join(outDir, `${id}.txt`), text)
    fs.writeFileSync(path.join(outDir, `${id}.console.json`), JSON.stringify(consoleErrors, null, 2))
    const reached = readyOk && samePath
    const result = {
      ...facts,
      reached,
      reason: reached ? '' : !samePath ? `redirected to ${finalUrl.pathname}${finalUrl.search}` : `ready element [data-testid="${target.ready}"] never appeared`,
      final_url: finalUrl.pathname + finalUrl.search,
      page_height: height,
      screenshot_truncated: height > MAX_SHOT_HEIGHT,
      overflow,
      console_error_count: consoleErrors.length,
      repeated_text: textFacts(text),
      files: { screenshot: `${side}/${id}.png`, text: `${side}/${id}.txt`, console: `${side}/${id}.console.json` },
    }
    fs.writeFileSync(path.join(outDir, `${id}.json`), JSON.stringify(result, null, 2))
    return result
  } finally {
    await ctx.close().catch(() => {})
  }
}

async function captureSide({ side, ref, cfg, targets, launch, setupDir, chromium }) {
  const scope = cleanups.length
  const outDir = path.join(OUT, side)
  fs.rmSync(outDir, { recursive: true, force: true })
  fs.mkdirSync(outDir, { recursive: true })
  log(`── ${side}: ${ref}`)
  const wt = addWorktree(ref, side)
  prepareDeps(wt, cfg.render, cfg.verify)

  let hook = {}
  if (setupDir && cfg.render.setup) {
    hook = await import(pathToFileURL(path.join(setupDir, path.basename(cfg.render.setup))).href + '?s=' + side)
  }
  const hookState = hook.start ? await hook.start({}) : { env: {}, cookies: [] }
  if (hookState.stop) cleanups.push(() => hookState.stop())

  const baseURL = await startDevServer(wt, launch, hookState.env || {}, cfg.render, path.join(OUT, 'logs', `${side}-dev.log`))
  const browser = await chromium.launch()
  cleanups.push(() => browser.close())

  const viewports = cfg.render.viewports || DEFAULT_VIEWPORTS
  const results = []
  try {
    for (const target of targets) {
      for (const [vpName, vp] of Object.entries(viewports)) {
        try {
          const r = await captureTarget({ browser, baseURL, hook, hookState, target, vpName, vp, renderCfg: cfg.render, side, outDir, fixtures: cfg.render.fixtures || {} })
          log(`${side} ${target.id} @${vpName}: ${r.reached ? 'ok' : 'NOT REACHED (' + r.reason + ')'}`)
          results.push(r)
        } catch (e) {
          log(`${side} ${target.id} @${vpName}: ERROR ${e.message.split('\n')[0]}`)
          results.push({ target: target.id, viewport: vpName, side, route: target.route, state: target.state, reached: false, reason: 'capture error: ' + e.message.split('\n')[0] })
        }
      }
    }
  } finally {
    await runCleanups(scope)
  }
  return { results }
}

// ── main ─────────────────────────────────────────────────────────────────────

function writeSummary(summary) {
  fs.mkdirSync(OUT, { recursive: true })
  fs.writeFileSync(path.join(OUT, 'summary.json'), JSON.stringify(summary, null, 2))
  const report = path.join(RUN_DIR, 'report.md')
  if (fs.existsSync(report)) {
    const lines = ['', '---', `## Render capture: ${summary.status} (${new Date().toISOString()})`, '']
    if (summary.reason) lines.push(summary.reason, '')
    if (summary.results) {
      lines.push('| target | viewport | before | after |', '|---|---|---|---|')
      const by = new Map()
      for (const r of summary.results) by.set(`${r.target}|${r.viewport}`, { ...(by.get(`${r.target}|${r.viewport}`) || {}), [r.side]: r })
      for (const [k, v] of by) {
        const [t, vp] = k.split('|')
        const cell = (x) => (!x ? 'n/a' : x.reached ? 'ok' : 'not reached: ' + x.reason)
        lines.push(`| ${t} | ${vp} | ${cell(v.before)} | ${cell(v.after)} |`)
      }
      lines.push('')
    }
    lines.push(`Details: \`runs/${RUN_ID}/render/summary.json\``, '')
    fs.appendFileSync(report, lines.join('\n'))
  }
}

async function main() {
  const hard = setTimeout(async () => {
    console.error('[render] hard timeout reached')
    await runCleanups()
    process.exit(3)
  }, 15 * 60 * 1000)
  hard.unref()

  if (!fs.existsSync(RUN_DIR)) throw new Infra(`no such run folder: ${RUN_DIR}`)
  const state = readState()
  const branch = state.target_branch || git('rev-parse', '--abbrev-ref', 'HEAD').out
  const original = state.original_branch || 'main'
  const configRef = flag('--config-from') || state.render_config_ref || original
  for (const ref of [branch, original, configRef]) {
    if (git('rev-parse', '--verify', '--quiet', ref).code !== 0) throw new Infra(`unknown git ref: ${ref}`)
  }

  const base = { run_id: RUN_ID, branch, original_branch: original, config_ref: configRef }

  // targets
  const contractPath = path.join(RUN_DIR, 'contract.json')
  const contract = fs.existsSync(contractPath) ? JSON.parse(fs.readFileSync(contractPath, 'utf8')) : {}
  const targets = Array.isArray(contract.render_targets) ? contract.render_targets : []

  // config from REF
  const rawVerify = gitShow(configRef, '.claude/verify.json')
  const verifyCfg = rawVerify ? JSON.parse(rawVerify) : {}
  const renderCfg = verifyCfg.render
  if (!renderCfg) {
    throw new Skip(`${configRef}:.claude/verify.json has no "render" section: this repo has not configured render review`)
  }
  if (!targets.length) {
    throw new Skip(contract.render_skip_reason ? `Architect declared no render targets: ${contract.render_skip_reason}` : 'contract.json declares no render_targets')
  }

  const rawLaunch = gitShow(configRef, '.claude/launch.json')
  if (!rawLaunch) throw new Infra(`${configRef}:.claude/launch.json is missing (needed for the dev-server command)`)
  const launches = JSON.parse(rawLaunch).configurations || []
  const launch = launches.find((c) => c.name === (renderCfg.server || 'dev')) || launches[0]
  if (!launch || !launch.runtimeExecutable) throw new Infra('launch.json has no usable configuration')

  // repo-owned hook dir, from REF
  let setupDir = null
  if (renderCfg.setup) {
    setupDir = fs.mkdtempSync(path.join(os.tmpdir(), 'orchestrator-render-setup-'))
    cleanups.push(() => fs.rmSync(setupDir, { recursive: true, force: true }))
    const dir = path.dirname(renderCfg.setup)
    const r = sh(`git -C ${JSON.stringify(REPO)} archive ${JSON.stringify(configRef)} ${JSON.stringify(dir)} | tar -x -C ${JSON.stringify(setupDir)} --strip-components=${dir.split('/').length}`)
    if (r.code !== 0 || !fs.existsSync(path.join(setupDir, path.basename(renderCfg.setup)))) {
      throw new Infra(`could not extract ${renderCfg.setup} from ${configRef}`)
    }
  }

  // Playwright from the pinned tool dir
  if (!fs.existsSync(path.join(TOOLS_DIR, 'node_modules', 'playwright'))) {
    log('installing pinned Playwright into .claude/render-tools/ (one time)')
    const r = sh('npm install --no-audit --no-fund', { cwd: TOOLS_DIR, env: { ...process.env, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1' } })
    if (r.code !== 0) throw new Infra('could not install Playwright:\n' + r.out.slice(-800))
  }
  const { chromium } = await import(pathToFileURL(path.join(TOOLS_DIR, 'node_modules', 'playwright', 'index.mjs')).href)

  fs.mkdirSync(path.join(OUT, 'logs'), { recursive: true })
  const cfg = { render: renderCfg, verify: verifyCfg }
  const all = []

  // after = the run's branch
  if (ONLY !== 'before') {
    const a = await captureSide({ side: 'after', ref: branch, cfg, targets, launch, setupDir, chromium })
    all.push(...a.results)
  }

  // before = original branch, cached by commit + config + targets
  if (ONLY !== 'after') {
    const key = sha1(JSON.stringify([git('rev-parse', original).out, rawVerify, rawLaunch, targets]))
    const keyFile = path.join(OUT, 'before', '.cache-key')
    const sumFile = path.join(OUT, 'before', '.results.json')
    if (fs.existsSync(keyFile) && fs.readFileSync(keyFile, 'utf8') === key && fs.existsSync(sumFile)) {
      log('before: reusing cached capture of', original)
      all.push(...JSON.parse(fs.readFileSync(sumFile, 'utf8')))
    } else {
      const b = await captureSide({ side: 'before', ref: original, cfg, targets, launch, setupDir, chromium })
      fs.writeFileSync(keyFile, key)
      fs.writeFileSync(sumFile, JSON.stringify(b.results))
      all.push(...b.results)
    }
  }

  const afterBad = all.filter((r) => r.side === 'after' && !r.reached)
  const summary = {
    ...base,
    status: afterBad.length ? 'INCOMPLETE' : 'CAPTURED',
    reason: afterBad.length ? `${afterBad.length} target(s) could not be reached on the branch: ` + afterBad.map((r) => `${r.target}@${r.viewport} (${r.reason})`).join('; ') : '',
    viewports: renderCfg.viewports || DEFAULT_VIEWPORTS,
    now: renderCfg.now || null,
    images: all.filter((r) => r.files).length,
    results: all,
  }
  writeSummary(summary)
  log(summary.status + (summary.reason ? ': ' + summary.reason : ''))
  return afterBad.length ? 1 : 0
}

let code = 3
try {
  code = await main()
} catch (e) {
  if (e instanceof Skip) {
    writeSummary({ run_id: RUN_ID, status: 'SKIPPED', reason: e.message, results: [] })
    log('SKIPPED:', e.message)
    code = 2
  } else {
    writeSummary({ run_id: RUN_ID, status: 'ERROR', reason: e.message.slice(0, 2000), results: [] })
    console.error('[render] ERROR:', e.message)
    code = 3
  }
} finally {
  await runCleanups()
}
process.exit(code)
