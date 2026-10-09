import { atom, read, update } from 'claude-code'
import type { EngineInterface, PluginOptions, Register } from 'claude-code'

import type { Card, Conflict, Detail, Filters, Snapshot, TaskEvent, View, Where } from '../types'

// The tracker is the Python CLI shipped in this plugin (scripts/task.py); every live read is one `snapshot` call.
const PANE = 'task-tracker'
const TITLE = 'Tasks'

const where = atom({ plugin: 'task-tracker', key: 'where' } as const, null)
const snap = atom({ plugin: 'task-tracker', key: 'snap' } as const, null)
const error = atom({ plugin: 'task-tracker', key: 'error' } as const, null)
const filters = atom({ plugin: 'task-tracker', key: 'filters' } as const, { area: '', priority: '', label: '' })
const view = atom({ plugin: 'task-tracker', key: 'view' } as const, 'board')
const back = atom({ plugin: 'task-tracker', key: 'back' } as const, 'board')
const detail = atom({ plugin: 'task-tracker', key: 'detail' } as const, null)
const page = atom({ plugin: 'task-tracker', key: 'page' } as const, 0)

const COLUMNS = ['todo', 'in_progress', 'in_review', 'blocked', 'done'] as const
const PRIORITIES = ['urgent', 'high', 'med', 'low'] as const
const STATUS_LABEL: Record<string, string> = {
  backlog: 'Backlog', todo: 'Todo', in_progress: 'In progress', in_review: 'In review', blocked: 'Blocked',
  done: 'Done today', cancelled: 'Cancelled',
}
const STATUS_ICON: Record<string, string> = {
  backlog: '◌', todo: '○', in_progress: '◐', in_review: '◑', blocked: '⊘', done: '●', cancelled: '✕',
}
const STATUS_COLOR: Record<string, string> = {
  backlog: 'inactive', todo: 'text', in_progress: 'warning', in_review: 'suggestion', blocked: 'error',
  done: 'success', cancelled: 'inactive',
}
const PRIO_ICON: Record<string, string> = { urgent: '‼', high: '▲', med: '■', low: '▽' }
const PRIO_COLOR: Record<string, string> = { urgent: 'error', high: 'warning', med: 'subtle', low: 'inactive' }

/** The manifest's userConfig defaults. */
const DEFAULTS: Record<string, string> = {
  python: 'python',
  identity: 'user',
  idPrefix: 'TASK',
  dbPath: '.claude/tasks/tasks.db',
  rolesFile: '.claude/tasks/roles.toml',
  labels: 'bug,feature,chore,docs,test,blocked-external',
  importers: '',
  notifyCommand: '',
}

type $T = EngineInterface
type Run = { ok: boolean; out: string; err: string; code: number }

/** This load's options; set once by register. Plain values only, nothing per session. */
const cfg = {
  python: 'python',
  identity: 'user',
  refreshMs: 3000,
  env: {} as Record<string, string>,
}
/** The interpreter that started last time (python, python3 or py). */
let found: string | null = null

function opt(options: PluginOptions, key: string): string {
  const v = options[key]
  return v === undefined || v === null ? (DEFAULTS[key] ?? '') : String(v)
}

function quote(s: string): string {
  return /^[\w./:\\@=,+-]+$/.test(s) ? s : `"${s.replace(/"/g, '\\"')}"`
}

function short(s: string | null | undefined, n: number): string {
  const t = (s ?? '').replace(/\s+/g, ' ')
  return t.length <= n ? t : t.slice(0, Math.max(0, n - 1)) + '…'
}

function filterArgs(f: Filters): string[] {
  const out: string[] = []
  if (f.area) out.push('--area', f.area)
  if (f.priority) out.push('--priority', f.priority)
  if (f.label) out.push('--label', f.label)
  return out
}

function scriptPath($: $T): string {
  return `${$.plugin.root}/scripts/task.py`
}

/**
 * The options reach the CLI as environment variables, which rank below the project's own
 * .claude/claude-mods.json: a project's settings win over one person's plugin options. Every process this
 * session starts later (the model's Bash, shell workers) inherits them, and TASKS_CLI names the CLI.
 */
async function exportEnv($: $T): Promise<void> {
  const v = cfg.env
  await $.env.set('TASKS_CLI', scriptPath($))
  await $.env.set('TASKS_PREFIX', v.TASKS_PREFIX)
  await $.env.set('TASKS_DB', v.TASKS_DB)
  await $.env.set('TASKS_ROLES', v.TASKS_ROLES)
  await $.env.set('TASKS_LABELS', v.TASKS_LABELS)
  await $.env.set('TASKS_IMPORTERS', v.TASKS_IMPORTERS)
  await $.env.set('TASKS_NOTIFY_CMD', v.TASKS_NOTIFY_CMD)
}

async function exec($: $T, exe: string, cwd: string, args: string[]): Promise<Run> {
  const r = await $.process.run([exe, scriptPath($), '--as', cfg.identity, ...args], {
    cwd,
    env: { ...cfg.env, NO_COLOR: '1', PYTHONIOENCODING: 'utf-8', TASKS_WIDTH: '110' },
    timeoutMs: 20000,
  })
  return { ok: r.exitCode === 0, out: r.stdout, err: r.stderr.trim(), code: r.exitCode }
}

/** Runs the CLI; the first interpreter that starts is kept (the python option, then python, python3, py). */
async function runTask($: $T, args: string[]): Promise<Run> {
  const order = found ? [found] : [cfg.python, ...['python', 'python3', 'py'].filter(p => p !== cfg.python)]
  const cwd = await $.session.root()
  let last = ''
  for (const exe of order) {
    try {
      const r = await exec($, exe, cwd, args)
      // 9009 / "Python was not found": the Windows store alias, not a real interpreter.
      if (r.code === 9009 || /Python was not found/i.test(r.err)) {
        last = `${exe}: not a Python interpreter`
        continue
      }
      found = exe
      return r
    } catch (err) {
      last = `${exe}: ${String(err)}`
    }
  }
  return { ok: false, out: '', err: `no Python found (tried ${order.join(', ')}; set the python option). ${last}`, code: -1 }
}

async function loadWhere($: $T): Promise<Where | null> {
  const r = await runTask($, ['where', '--json'])
  if (!r.ok) {
    await update($, error, () => short(r.err || 'task.py where failed', 300))
    return null
  }
  try {
    const w = JSON.parse(r.out) as Where
    await update($, where, () => w)
    return w
  } catch {
    await update($, error, () => 'task.py where printed no JSON')
    return null
  }
}

async function refresh($: $T, isFirst = false): Promise<void> {
  const w = (await read($, where)) ?? (await loadWhere($))
  if (!w || !w.db_exists) return
  const f = await read($, filters)
  const storeKey = `lastEvent:${w.db}`
  const since = (await $.store.get(storeKey)) as number | undefined
  const args = ['snapshot', ...filterArgs(f)]
  if (since !== undefined && !isFirst) args.push('--since', String(since))
  const r = await runTask($, args)
  if (!r.ok) {
    await update($, error, () => short(r.err || 'task snapshot failed', 300))
    $.ui.status('tasks: tracker unreadable')
    return
  }
  let s: Snapshot
  try {
    s = JSON.parse(r.out) as Snapshot
  } catch {
    await update($, error, () => 'task snapshot printed no JSON')
    return
  }
  const prev = await read($, snap)
  await update($, error, () => null)
  await update($, snap, () => s)
  await $.store.set(storeKey, s.stats.last_event)
  statusLine($, s)
  if (!isFirst && since !== undefined) announce($, s.events, prev, s)
}

async function openDetail($: $T, key: string): Promise<string | null> {
  const r = await runTask($, ['show', key, '--json'])
  if (!r.ok) return r.err || `could not read ${key}`
  const d = JSON.parse(r.out) as Detail
  const cur = await read($, view)
  if (cur !== 'detail') await update($, back, () => cur)
  await update($, detail, () => d)
  await update($, view, () => 'detail' as View)
  return null
}

async function initTracker($: $T): Promise<string> {
  const r = await runTask($, ['init'])
  await update($, where, () => null)
  await loadWhere($)
  await refresh($, true)
  return r.ok ? r.out.trim() : `task.py init failed: ${r.err}`
}

/** The command an agent types; the options already sit in its environment. */
function commandPrefix($: $T): string {
  return `${found ?? cfg.python} ${quote(scriptPath($))}`
}

async function poll($: $T, state: { stamp: string }): Promise<void> {
  const cur = (await read($, where)) ?? (await loadWhere($))
  if (!cur) return
  const a = await $.fs.stat(cur.db).catch(() => null)
  const wal = await $.fs.stat(`${cur.db}-wal`).catch(() => null)
  const now = `${a?.mtimeMs}:${wal?.mtimeMs}:${wal?.size}`
  if (now === state.stamp) return
  state.stamp = now
  if (a && !cur.db_exists) await update($, where, () => ({ ...cur, db_exists: true }))
  if (a) await refresh($)
}

// ------------------------------------------------------------------------------------------- register

export const register: Register = (on, options) => {
  cfg.python = opt(options, 'python').trim() || 'python'
  cfg.identity = opt(options, 'identity').trim() || 'user'
  cfg.refreshMs = Math.max(0, Number(options.refreshSeconds ?? 3)) * 1000
  cfg.env = {
    TASKS_PREFIX: opt(options, 'idPrefix'),
    TASKS_DB: opt(options, 'dbPath'),
    TASKS_ROLES: opt(options, 'rolesFile'),
    TASKS_LABELS: opt(options, 'labels'),
    TASKS_IMPORTERS: opt(options, 'importers'),
    TASKS_NOTIFY_CMD: opt(options, 'notifyCommand'),
  }
  found = null

  // ----------------------------------------------------------------------------------------- session

  on('session.start', async ($, e, next) => {
    await exportEnv($)
    await $.command.register({
      name: 'tasks',
      description: 'Task tracker: board, list, progress, conflicts; /tasks TASK-12 for one task; /tasks init',
      argumentHint: '[<id> | board | list | progress | conflicts | init | area=x prio=high label=bug | clear]',
    })
    await update($, where, () => null)
    const w = await loadWhere($)
    if (w?.db_exists) await refresh($, true)
    if (cfg.refreshMs > 0) {
      const state = { stamp: '' }
      $.clock.every(cfg.refreshMs, () => poll($, state))
    }
    return next(e)
  })

  on('classic.SessionStart', async ($, e, next) => {
    const r = await next(e)
    const w = (await read($, where)) ?? (await loadWhere($))
    if (!w) return r
    const cmd = commandPrefix($)
    const lines = w.db_exists
      ? [
          `This project has a task tracker. Use it with: ${cmd} <command> ($TASKS_CLI holds the script path; add ` +
            `--as <name> or set TASKS_AS when you were given a role). \`${cmd} --help\` lists every command.`,
          `Loop: \`mine\` between steps; \`claim ${w.prefix}-<n>\` before editing; \`touch ${w.prefix}-<n> "<glob>"\` ` +
            `for the files you edit (overlaps with other in-progress tasks are refused); \`move ${w.prefix}-<n> ` +
            `in_review|done|blocked -m "why"\` when finished or stuck; \`new "title" --touches "<glob>"\` for untracked work.`,
        ]
      : [`No task tracker in this project yet; \`/tasks init\` or \`${cmd} init\` creates one.`]
    return { ...r, additionalContext: [...(r.additionalContext ?? []), lines.join('\n')] }
  }).catch(($, e, next) => next(e))

  // ----------------------------------------------------------------------------------------- /tasks

  on('command.run', { command: 'tasks' }, async ($, e) => {
    const toks = (e.args ?? '').trim().split(/\s+/).filter(Boolean)
    let text = ''
    let w = await read($, where)
    if (!w?.db_exists) w = await loadWhere($)
    const keyRe = new RegExp(`^(?:${(w?.prefix ?? 'TASK').replace(/[^\w]/g, '\\$&')}-?)?(\\d+)$`, 'i')
    for (const t of toks) {
      const m = keyRe.exec(t)
      if (t === 'init') {
        text = await initTracker($)
        w = await read($, where)
      } else if (m && w?.db_exists) {
        const key = `${w.prefix}-${m[1]}`
        const err = await openDetail($, key)
        const r = await runTask($, ['show', key, '--last', '8'])
        text = err ?? r.out
      } else if (['board', 'list', 'progress', 'conflicts'].includes(t)) {
        await update($, view, () => t as View)
      } else if (t === 'clear') {
        await update($, filters, () => ({ area: '', priority: '', label: '' }))
      } else {
        const kv = /^(area|prio|priority|label)=(.*)$/.exec(t)
        if (kv) {
          const field = kv[1] === 'prio' ? 'priority' : (kv[1] ?? 'area')
          await update($, filters, f => ({ ...f, [field]: kv[2] ?? '' }))
        } else if (!m) {
          text = `unknown argument ${t}. Try: /tasks ${w?.prefix ?? 'TASK'}-12 | board | list | progress | conflicts | init | area=api prio=high label=bug`
        }
      }
    }
    await update($, page, () => 0)
    await refresh($, true)
    const opened = await $.ui.open({ id: PANE, title: TITLE })
    if (!text) {
      const s = await read($, snap)
      const b = s?.stats.by_status
      if (!w?.db_exists) {
        text = `No task tracker in ${w?.root ?? 'this project'} yet. \`/tasks init\` creates ${w?.db ?? 'it'}.`
      } else if (s && b) {
        text =
          `Tasks: ${b.in_progress} in progress, ${b.in_review} in review, ${b.blocked} blocked, ` +
          `${s.stats.done_today} done today, ${s.stats.conflicts} file conflicts.` +
          (opened.isPlaced ? ' Board open in the pane.' : ' Widen the terminal to see the board pane.')
      } else {
        text = `The tracker could not be read: ${(await read($, error)) ?? 'unknown error'}`
      }
    }
    return { text }
  }).catch(() => ({ text: 'The task tracker failed. Check its setup with: python <plugin>/scripts/task.py where' }))

  // ----------------------------------------------------------------------------------------- the band

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, snap)
    const blocked = s?.stats.by_status.blocked ?? 0
    if (!s || e.props.hasSurvey || (s.stats.conflicts === 0 && s.review_for_me.length === 0 && blocked === 0)) {
      return next(e)
    }
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box gap={2}>
        <Text bold>tasks</Text>
        {s.stats.conflicts > 0 && <Text color="error">⚠ {s.stats.conflicts} file conflict{s.stats.conflicts > 1 ? 's' : ''}</Text>}
        {blocked > 0 && <Text color="error">⊘ {blocked} blocked</Text>}
        {s.review_for_me.length > 0 && <Text color="suggestion">◑ {s.review_for_me.length} review{s.review_for_me.length > 1 ? 's' : ''} for you</Text>}
        <Button
          key="open"
          label="Open board"
          onPress={async () => {
            await update($, view, () => (s.stats.conflicts > 0 ? 'conflicts' : 'board') as View)
            await $.ui.open({ id: PANE, title: TITLE })
          }}
        />
      </Box>
    )
  })

  // ----------------------------------------------------------------------------------------- the pane

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text, Button, Markdown } = els
    const Select = 'Select' in els ? els.Select : null
    const s = await read($, snap)
    const w = await read($, where)
    const err = await read($, error)
    const v = await read($, view)
    const f = await read($, filters)
    const pg = await read($, page)
    const cols = e.props.bodyColumns
    const rows = Math.max(12, (e.viewport?.rows ?? 30) - 2)

    if (w && !w.db_exists) {
      return (
        <Box flexDirection="column" gap={1}>
          <Text bold>{TITLE}</Text>
          <Text>No task tracker in this project yet.</Text>
          <Text dimColor>It will live at {w.db}</Text>
          <Button key="init" label="Create the tracker" onPress={async () => { await initTracker($) }} />
        </Box>
      )
    }
    if (!s) {
      return (
        <Box flexDirection="column">
          <Text bold>{TITLE}</Text>
          <Text color="error">{err ?? 'loading…'}</Text>
          <Button key="retry" label="Retry" onPress={async () => { await update($, where, () => null); await refresh($, true) }} />
        </Box>
      )
    }

    const setView = (to: View) => async () => {
      await update($, view, () => to)
      await update($, page, () => 0)
    }
    const setFilter = (field: keyof Filters) => async (value: string) => {
      await update($, filters, cur => ({ ...cur, [field]: value === '*' ? '' : value }))
      await update($, page, () => 0)
      await refresh($, true)
    }
    const press = (key: string) => async () => {
      await openDetail($, key)
    }

    const b = s.stats.by_status
    const header = (
      <Box flexDirection="column">
        <Box gap={2} flexWrap="wrap">
          <Text bold>▤ {TITLE}</Text>
          <Text color="warning">◐ {b.in_progress ?? 0} active</Text>
          <Text color="suggestion">◑ {b.in_review ?? 0} review</Text>
          <Text color="error">⊘ {b.blocked ?? 0} blocked</Text>
          <Text color="success">● {s.stats.done_today} today</Text>
          <Text dimColor>{b.todo ?? 0} todo · {b.backlog ?? 0} backlog</Text>
          {s.stats.conflicts > 0 && <Text color="error" bold>⚠ {s.stats.conflicts} conflicts</Text>}
          {s.stats.proposed > 0 && <Text color="suggestion">? {s.stats.proposed} proposed</Text>}
          <Text dimColor>you: {s.me.name} ({s.me.level})</Text>
        </Box>
        <Box gap={1} flexWrap="wrap">
          <Button key="v-board" hotkey="b" label="Board" variant={v === 'board' ? 'primary' : undefined} onPress={setView('board')} />
          <Button key="v-list" hotkey="l" label="List" variant={v === 'list' ? 'primary' : undefined} onPress={setView('list')} />
          <Button key="v-progress" hotkey="p" label="Progress" variant={v === 'progress' ? 'primary' : undefined} onPress={setView('progress')} />
          <Button key="v-conflicts" hotkey="c" label={`Conflicts ${s.stats.conflicts}`} variant={v === 'conflicts' ? 'primary' : undefined} onPress={setView('conflicts')} />
          {Select && s.areas.length > 0 && (
            <Select
              key="f-area"
              label="area"
              value={f.area || '*'}
              options={[{ label: 'all areas', value: '*' }, ...s.areas.map(a => ({ label: a, value: a }))]}
              onSelect={setFilter('area')}
            />
          )}
          {Select && (
            <Select
              key="f-prio"
              label="priority"
              value={f.priority || '*'}
              options={[{ label: 'any priority', value: '*' }, ...PRIORITIES.map(p => ({ label: `${PRIO_ICON[p]} ${p}`, value: p }))]}
              onSelect={setFilter('priority')}
            />
          )}
          {Select && s.labels.length > 0 && (
            <Select
              key="f-label"
              label="label"
              value={f.label || '*'}
              options={[{ label: 'any label', value: '*' }, ...s.labels.map(l => ({ label: l, value: l }))]}
              onSelect={setFilter('label')}
            />
          )}
          <Button key="refresh" hotkey="r" label="↻" onPress={() => refresh($, true)} />
        </Box>
        {err && <Text color="error">{err}</Text>}
      </Box>
    )

    const cardLine = (c: Card, width: number) => (
      <Button key={`card-${c.key}`} plain onPress={press(c.key)}>
        <Text color={PRIO_COLOR[c.priority]}>{PRIO_ICON[c.priority]}</Text> <Text bold>{c.key}</Text>{' '}
        <Text color="claude">{short(c.assignee ?? (c.proposed ? `?${c.proposed}` : '—'), 12)}</Text>
        {'\n'}
        <Text>{short(c.title, width - 1)}</Text>
      </Button>
    )

    let body
    if (v === 'detail') {
      body = detailView(await read($, detail), cols, { Box, Text, Button, Markdown }, setView(await read($, back)), press)
    } else if (v === 'progress') {
      body = progressView(s, cols, { Box, Text })
    } else if (v === 'conflicts') {
      body = conflictsView(s.conflicts, { Box, Text, Button }, press)
    } else if (v === 'list' || cols < 100) {
      const all: { status: string; card: Card }[] = []
      for (const st of COLUMNS) for (const c of s.board[st] ?? []) all.push({ status: st, card: c })
      const per = Math.max(5, rows - 8)
      const shown = all.slice(pg * per, pg * per + per)
      const kw = Math.max(7, ...all.map(x => x.card.key.length))
      body = (
        <Box flexDirection="column">
          {shown.map(({ status, card }) => (
            <Button key={`row-${card.key}`} plain onPress={press(card.key)}>
              <Text color={STATUS_COLOR[status]}>{STATUS_ICON[status]} {short(STATUS_LABEL[status], 11).padEnd(11)}</Text>{' '}
              <Text color={PRIO_COLOR[card.priority]}>{PRIO_ICON[card.priority]}</Text> <Text bold>{card.key.padEnd(kw)}</Text>{' '}
              <Text color="claude">{short(card.assignee ?? '—', 12).padEnd(12)}</Text>{' '}
              {short(card.title, Math.max(10, cols - 48))}
              <Text dimColor> {card.area ?? ''}</Text>
            </Button>
          ))}
          <Box gap={1}>
            {pg > 0 && <Button key="prev" hotkey="k" label="◀ prev" onPress={() => update($, page, n => n - 1)} />}
            {(pg + 1) * per < all.length && <Button key="next" hotkey="j" label="next ▶" onPress={() => update($, page, n => n + 1)} />}
            <Text dimColor>{all.length === 0 ? 'no tasks match' : `${pg * per + 1}-${Math.min(all.length, (pg + 1) * per)} of ${all.length}`}</Text>
          </Box>
        </Box>
      )
    } else {
      const width = Math.floor((cols - (COLUMNS.length - 1)) / COLUMNS.length)
      const fit = Math.max(2, Math.floor((rows - 7) / 2))
      body = (
        <Box gap={1}>
          {COLUMNS.map(st => {
            const cards = s.board[st] ?? []
            const count = s.counts[st] ?? 0
            return (
              <Box key={`col-${st}`} flexDirection="column" width={width}>
                <Text color={STATUS_COLOR[st]} bold>
                  {STATUS_ICON[st]} {STATUS_LABEL[st]} <Text dimColor>{count}</Text>
                </Text>
                <Text color={STATUS_COLOR[st]} dimColor>{'─'.repeat(Math.max(1, width))}</Text>
                {cards.slice(0, fit).map(c => cardLine(c, width))}
                {count > fit && <Text dimColor>+{count - fit} more (List)</Text>}
              </Box>
            )
          })}
        </Box>
      )
    }

    return (
      <Box flexDirection="column" gap={1}>
        {header}
        {body}
      </Box>
    )
  })
}

// ------------------------------------------------------------------------------------------- status + toasts

function statusLine($: $T, s: Snapshot): void {
  const b = s.stats.by_status
  const parts = [`◐ ${b.in_progress ?? 0}`, `◑ ${b.in_review ?? 0}`, `⊘ ${b.blocked ?? 0}`, `● ${s.stats.done_today} today`]
  if (s.stats.conflicts) parts.push(`⚠ ${s.stats.conflicts} conflict${s.stats.conflicts > 1 ? 's' : ''}`)
  $.ui.status(`tasks  ${parts.join('  ')}`)
}

function announce($: $T, events: TaskEvent[], prev: Snapshot | null, s: Snapshot): void {
  const lines: string[] = []
  for (const ev of events) {
    const d = ev.detail as Record<string, string | undefined>
    if (ev.action === 'move' && d.to === 'blocked') {
      lines.push(`⊘ ${ev.key} blocked by ${ev.actor}: ${short(ev.title, 50)}${d.note ? ` (${short(d.note, 60)})` : ''}`)
    } else if (ev.action === 'conflict_override') {
      lines.push(`⚠ ${ev.key} started over a file conflict by ${ev.actor}: ${short(String(d.reason ?? ''), 60)}`)
    } else if (ev.action === 'review' && d.verdict === 'reject') {
      lines.push(`✕ ${ev.key} review rejected by ${ev.actor}`)
    }
  }
  const before = new Set(prev?.review_for_me ?? [])
  for (const k of s.review_for_me) if (!before.has(k)) lines.push(`◑ review requested from you: ${k}`)
  if (prev && s.stats.conflicts > prev.stats.conflicts) {
    const c = s.conflicts[s.conflicts.length - 1]
    if (c) lines.push(`⚠ new file conflict: ${c.a} (${c.a_assignee}) × ${c.b} (${c.b_assignee})`)
  }
  if (lines.length > 3) {
    $.ui.toast(`${lines[0]}  (+${lines.length - 1} more: /tasks)`)
  } else {
    for (const l of lines) $.ui.toast(l)
  }
}

// ------------------------------------------------------------------------------------------- views

type El = ReturnType<EngineInterface['ui']['resolve']>

function progressView(s: Snapshot, cols: number, { Box, Text }: Pick<El, 'Box' | 'Text'>) {
  const barW = Math.max(10, Math.min(30, cols - 60))
  const row = (name: string, d: Record<string, number>, key: string) => {
    const tot = Object.entries(d).reduce((n, [k, x]) => (k === 'cancelled' ? n : n + x), 0)
    const pct = tot ? Math.floor((100 * (d.done ?? 0)) / tot) : 0
    const full = Math.round((pct * barW) / 100)
    return (
      <Box key={key} gap={1}>
        <Text>{short(name, 22).padEnd(22)}</Text>
        <Text color="success">{'█'.repeat(full)}</Text>
        <Text dimColor>{'░'.repeat(barW - full)}</Text>
        <Text bold>{String(pct).padStart(3)}%</Text>
        <Text dimColor>
          {d.done ?? 0}/{tot} done
        </Text>
        <Text color="warning">◐{d.in_progress ?? 0}</Text>
        <Text color="suggestion">◑{d.in_review ?? 0}</Text>
        <Text color="error">⊘{d.blocked ?? 0}</Text>
      </Box>
    )
  }
  const areas = Object.entries(s.stats.areas).filter(([k]) => k !== '-')
  const labels = Object.entries(s.stats.labels)
  return (
    <Box flexDirection="column">
      <Text bold>Overall</Text>
      {row('all tasks', s.stats.by_status, 'all')}
      {areas.length > 0 && <Text bold>Areas</Text>}
      {areas.map(([k, d]) => row(k, d, `a-${k}`))}
      {labels.length > 0 && <Text bold>Labels</Text>}
      {labels.map(([k, d]) => row(k, d, `l-${k}`))}
    </Box>
  )
}

function conflictsView(cs: Conflict[], { Box, Text, Button }: Pick<El, 'Box' | 'Text' | 'Button'>, press: (k: string) => () => Promise<void>) {
  if (cs.length === 0) return <Text color="success">✓ No file conflicts between in-progress tasks.</Text>
  return (
    <Box flexDirection="column" gap={1}>
      {cs.map((c, i) => (
        <Box key={`c-${i}`} flexDirection="column">
          <Box gap={1}>
            <Text color="error">⚠</Text>
            <Button key={`ca-${i}`} plain onPress={press(c.a)}>
              <Text bold>{c.a}</Text> <Text color="claude">{c.a_assignee ?? '—'}</Text> {short(c.a_title, 40)}
            </Button>
            <Text dimColor>×</Text>
            <Button key={`cb-${i}`} plain onPress={press(c.b)}>
              <Text bold>{c.b}</Text> <Text color="claude">{c.b_assignee ?? '—'}</Text> {short(c.b_title, 40)}
            </Button>
          </Box>
          <Text dimColor>
            {'   '}
            {c.a_glob} overlaps {c.b_glob}
          </Text>
        </Box>
      ))}
    </Box>
  )
}

function detailView(
  d: Detail | null,
  cols: number,
  { Box, Text, Button, Markdown }: Pick<El, 'Box' | 'Text' | 'Button' | 'Markdown'>,
  goBack: () => Promise<void>,
  press: (k: string) => () => Promise<void>,
) {
  if (!d) return <Text dimColor>No task selected.</Text>
  const bodyText = d.body.split('\n').slice(0, 30).join('\n')
  return (
    <Box flexDirection="column" gap={1}>
      <Box gap={2}>
        <Button key="back" hotkey="x" label="◀ Back" onPress={goBack} />
        <Text bold>{d.key}</Text>
        <Text bold>{short(d.title, cols - 20)}</Text>
      </Box>
      <Box gap={2} flexWrap="wrap">
        <Text color={STATUS_COLOR[d.status]}>{`${STATUS_ICON[d.status] ?? ''} ${STATUS_LABEL[d.status] ?? d.status}`}</Text>
        <Text color={PRIO_COLOR[d.priority]}>{`${PRIO_ICON[d.priority] ?? ''} ${d.priority}`}</Text>
        <Text>
          assignee <Text color="claude">{d.assignee ?? '—'}</Text>
        </Text>
        {d.proposed && <Text color="suggestion">proposed {d.proposed}</Text>}
        <Text dimColor>reviewer {d.reviewer ?? '—'}</Text>
        <Text dimColor>area {d.area ?? '—'}</Text>
        <Text dimColor>by {d.creator}</Text>
      </Box>
      {(d.labels.length > 0 || d.links.length > 0) && (
        <Text dimColor>
          {d.labels.map(l => `#${l}`).join(' ')} {d.links.map(l => `${l.kind}:${l.value}`).join('  ')}
        </Text>
      )}
      {d.touches.length > 0 && <Text color="warning">touches {d.touches.join(', ')}</Text>}
      {(d.blocked_by.length > 0 || d.blocks.length > 0) && (
        <Box gap={1}>
          {d.blocked_by.length > 0 && <Text color="error">waits on</Text>}
          {d.blocked_by.map(k => (
            <Button key={`bb-${k}`} plain onPress={press(k)}>
              <Text bold>{k}</Text>
            </Button>
          ))}
          {d.blocks.length > 0 && <Text dimColor>blocks</Text>}
          {d.blocks.map(k => (
            <Button key={`bl-${k}`} plain onPress={press(k)}>
              <Text bold>{k}</Text>
            </Button>
          ))}
        </Box>
      )}
      {bodyText.trim() && <Markdown text={bodyText} />}
      {d.children.length > 0 && (
        <Box flexDirection="column">
          <Text bold>Sub-tasks</Text>
          {d.children.map(ch => (
            <Button key={`ch-${ch.key}`} plain onPress={press(ch.key)}>
              <Text color={STATUS_COLOR[ch.status]}>{STATUS_ICON[ch.status]}</Text> <Text bold>{ch.key}</Text>{' '}
              <Text color="claude">{ch.assignee ?? '—'}</Text> {short(ch.title, cols - 30)}
            </Button>
          ))}
        </Box>
      )}
      {d.comments.length > 0 && (
        <Box flexDirection="column">
          <Text bold>Comments</Text>
          {d.comments.slice(-6).map((c, i) => (
            <Text key={`cm-${i}`}>
              <Text dimColor>{c.ts.slice(5, 16).replace('T', ' ')}</Text> <Text color="claude">{c.author}</Text> {short(c.body, cols * 2)}
            </Text>
          ))}
        </Box>
      )}
      <Box flexDirection="column">
        <Text bold>History</Text>
        {d.history.slice(-10).map((ev, i) => (
          <Text key={`h-${i}`}>
            <Text dimColor>{ev.ts.slice(5, 16).replace('T', ' ')}</Text> <Text color="claude">{ev.actor}</Text> {ev.action}{' '}
            <Text dimColor>{short(describe(ev.detail), cols - 40)}</Text>
          </Text>
        ))}
      </Box>
    </Box>
  )
}

function describe(d: Record<string, unknown>): string {
  return Object.entries(d)
    .filter(([, v]) => v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(' ')
}
