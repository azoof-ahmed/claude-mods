import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderChildren } from 'claude-code'

import type { BusMessage, BusMode, BusSnapshot } from '../types'

/** What `bus.py hook` prints. */
type HookOut = {
  bus: boolean
  dir?: string
  role?: string | null
  text?: string | null
  watch?: boolean
  registered?: boolean
  cli?: string
}

type $T = EngineInterface

const REMIND_EVERY_MS = 10 * 60 * 1000
const RECHECK_ROLE_EVERY = 25
const PANE = 'bus'
const ME = 'user'
const PANE_POLL_MS = 2000
const BAND_POLL_MS = 15000
const EMOJI = ['✅', '👀', '👍', '❌', '🙏', '🎉'] as const

const snap = atom({ plugin: 'agent-bus', key: 'snap' } as const, null)
const error = atom({ plugin: 'agent-bus', key: 'error' } as const, null)
const selected = atom({ plugin: 'agent-bus', key: 'selected' } as const, null)
const expanded = atom({ plugin: 'agent-bus', key: 'expanded' } as const, null)
const mode = atom({ plugin: 'agent-bus', key: 'mode' } as const, 'normal')
const draft = atom({ plugin: 'agent-bus', key: 'draft' } as const, '')
const replyTo = atom({ plugin: 'agent-bus', key: 'replyTo' } as const, null)
const notice = atom({ plugin: 'agent-bus', key: 'notice' } as const, null)

type Theme = { accent: string; me: string; dim: string; ok: string; warn: string; err: string; selBg: string }

/** tokyonight-like by default; `claude` swaps the accent for Claude's orange. */
const THEMES: Record<string, Theme> = {
  tokyonight: { accent: '#7aa2f7', me: '#bb9af7', dim: '#565f89', ok: '#9ece6a', warn: '#e0af68', err: '#f7768e', selBg: '#283457' },
  claude: { accent: '#D97757', me: '#D97757', dim: '#8a8580', ok: '#7fb069', warn: '#e5a54b', err: '#e06c75', selBg: '#3b2a23' },
}

/** This load's options, set once by register. */
const cfg = { python: 'python', band: true, theme: THEMES.tokyonight as Theme }

// Module state: a reload starts it over (timers are the engine's and end with the load).
let paneTimer: { cancel: () => void } | undefined
let paneOpen = false
let lastKey = 0
let gPending = false
const toasted = new Set<string>()
let primed = false
let lastBandLook = 0
/** Whether the last snapshot found a bus in this project (undefined before the first). */
let busFound: boolean | undefined

function str(v: unknown, fallback: string): string {
  return typeof v === 'string' && v !== '' ? v : fallback
}

function short(s: string | null | undefined, n: number): string {
  const t = (s ?? '').replace(/\s+/g, ' ')
  return t.length <= n ? t : t.slice(0, Math.max(0, n - 1)) + '…'
}

/** Runs `bus.py <args>` from this plugin's folder. */
async function runBus($: $T, python: string, args: string[], timeoutMs: number) {
  return $.process.run([python, `${$.plugin.root}/scripts/bus.py`, ...args], { timeoutMs })
}

/** Asks bus.py for this session's mail and state at a hook event. */
async function hook($: $T, python: string, event: string): Promise<HookOut> {
  const ran = await runBus($, python, ['hook', '--event', event], 15000)
  if (ran.exitCode !== 0) {
    throw new Error(`bus.py hook exited ${ran.exitCode}: ${ran.stderr.trim().slice(0, 300)}`)
  }
  return JSON.parse(ran.stdout) as HookOut
}

/** What `/bus` says when the configured bus directory does not exist. */
export function noBusText(dir: string | undefined): string {
  return (
    `agent-bus: no bus in this project yet (${dir ?? '.claude/bus'}). \`/bus init\` creates one there. ` +
    'If this project\'s bus lives elsewhere, set "busDir" in the "agent-bus" section of .claude/claude-mods.json ' +
    '(for example {"agent-bus": {"busDir": "tmp/bus"}}).'
  )
}

/** The `/bus` subcommands: the pane's own, then the bus.py verbs. Nothing else ever reaches the CLI. */
export const BUS_VERBS = [
  'pane', 'full', 'tui', 'chat', 'init', 'send', 'digest', 'read', 'inbox', 'ack', 'watch', 'react', 'thread',
  'sub', 'unsub', 'pin', 'unpin', 'edit', 'delete', 'status', 'who', 'register', 'unregister', 'deliver',
  'snapshot', 'whoami', 'watch-live',
] as const

export const BUS_HELP = [
  'Usage: /bus [subcommand]  (no subcommand opens the chat pane)',
  '  @role <text>             send <text> to role as user',
  '  full | tui               the full-screen client in a new terminal',
  '  chat [--stop|--status]   the localhost web chat',
  '  init [--roles a,b]       create this project\'s bus',
  '  send <to> "<text>"       send a message',
  '  digest | who | status [id] | read <id> | thread <id>',
  '  react <id> <emoji> | sub | unsub | pin | unpin | edit | delete <id>',
  '  register <role> ... | whoami | deliver',
].join('\n')

/**
 * `/bus` arguments as bus.py verbs. Nothing (or `pane`) opens the chat pane (null); `full` and `tui` start
 * the standalone full-screen client in a new terminal; `chat` starts the web chat in the background;
 * `@role text` sends text to that role as `user`. Anything else is not a subcommand: the help text
 * (a string) is returned, and nothing is run.
 */
export function busArgs(typed: string): string[] | string | null {
  const trimmed = typed.trim()
  const args = trimmed === '' ? [] : trimmed.split(/\s+/)
  if (args.length === 0 || (args[0] === 'pane' && args.length === 1)) {
    return null
  }
  const lead = /^((?:@[A-Za-z0-9][\w.-]*[\s,]*)+)([\s\S]*)$/.exec(trimmed)
  if (lead) {
    const to = [...(lead[1] ?? '').matchAll(/@([A-Za-z0-9][\w.-]*)/g)].map(x => (x[1] ?? '').replace(/[.-]+$/, ''))
    const text = (lead[2] ?? '').trim()
    return text === '' ? `${BUS_HELP}\n\n/bus @role needs a message after the role.` : ['send', '--from', ME, '--', to.join(','), text]
  }
  if (['help', '-h', '--help'].includes(args[0] ?? '')) {
    return BUS_HELP
  }
  if (!(BUS_VERBS as readonly string[]).includes(args[0] ?? '') || args[0] === 'pane') {
    return `/bus: "${args[0]}" is not a subcommand.\n${BUS_HELP}`
  }
  const flags = new Set(args.slice(1))
  if (args[0] === 'chat' && !flags.has('--stop') && !flags.has('--status')) {
    return [...args, '--detach']
  }
  if (args[0] === 'full') {
    return ['tui', '--open', ...args.slice(1)]
  }
  if (args[0] === 'tui') {
    return [...args, '--open']
  }
  return args
}

/** The Monitor command that keeps an idle agent listening. */
export function watchCommand(out: HookOut): string {
  return `${out.cli} watch --as ${out.role} --for 1790`
}

export function armLine(out: HookOut): string {
  return (
    `agent-bus: no live watch for ${out.role}. Arm one now with the Monitor tool: command ` +
    `\`${watchCommand(out)}\`, timeout 1800000 ms. It prints one line per new non-fyi message, ` +
    'which wakes you when idle; re-arm it whenever it ends. Skip this if one is already running.'
  )
}

/** The one tick a sender sees for a message: the least advanced receipt wins, a failure first. */
export function tickOf(m: BusMessage): string {
  const rs = m.receipts
  if (rs.length === 0) {
    return m.chan || m.to.length === 0 ? '#' : '.'
  }
  for (const bad of ['failed', 'expired']) {
    const r = rs.find(x => x.r === bad)
    if (r) return r.tick
  }
  for (const st of ['queued', 'delivered', 'seen', 'posted']) {
    const r = rs.find(x => x.r === st)
    if (r) return r.tick
  }
  return rs[0]?.tick ?? '.'
}

/** Where a composed line goes: a reply to the parent's author, the leading @roles, else #all. */
export function sendArgs(text: string, parent: BusMessage | undefined, roles: string[]): string[] {
  const args = ['send', '--from', ME]
  let to = '#all'
  if (parent) {
    args.push('--re', parent.id)
    to = parent.from !== ME ? parent.from : (parent.to[0] ?? (parent.chan ? `#${parent.chan}` : '#all'))
  } else {
    const lead = /^((?:@[A-Za-z0-9][\w.-]*[\s,]*)+)/.exec(text.trim())
    const named = lead ? [...(lead[1] ?? '').matchAll(/@([A-Za-z0-9][\w.-]*)/g)].map(x => (x[1] ?? '').replace(/[.-]+$/, '')) : []
    const known = named.filter(n => roles.includes(n) && n !== ME)
    if (known.length > 0) to = known.join(',')
  }
  return [...args, '--', to, text]
}

/** Roles that complete the `@prefix` the draft ends with. */
export function completions(text: string, roles: string[]): string[] {
  const m = /(?:^|\s)@([\w.-]*)$/.exec(text)
  if (!m) return []
  const p = (m[1] ?? '').toLowerCase()
  return roles.filter(r => r !== ME && r.toLowerCase().startsWith(p) && r.toLowerCase() !== p).slice(0, 4)
}

function messages(s: BusSnapshot | null): BusMessage[] {
  return s?.messages ?? []
}

function selIndex(ms: BusMessage[], id: string | null): number {
  const i = id === null ? -1 : ms.findIndex(m => m.id === id)
  return i >= 0 ? i : ms.length - 1
}

// ------------------------------------------------------------------------------------------- data

/** One `snapshot` call: incremental by cursor unless forced; toasts new @mentions and replies to the user. */
async function refresh($: $T, force = false): Promise<void> {
  const prev = await read($, snap)
  const args = ['snapshot', '--as', ME]
  if (!force && prev?.cursor) args.push('--since', prev.cursor)
  let s: BusSnapshot
  try {
    const r = await runBus($, cfg.python, args, 20000)
    if (r.exitCode !== 0) {
      await update($, error, () => short(r.stderr.trim() || `bus.py snapshot exited ${r.exitCode}`, 300))
      return
    }
    s = JSON.parse(r.stdout) as BusSnapshot
  } catch (err) {
    await update($, error, () => short(String(err), 300))
    return
  }
  await update($, error, () => null)
  busFound = s.bus
  if (s.unchanged && prev) {
    await update($, snap, () => ({ ...prev, health: s.health ?? prev.health, cursor: s.cursor ?? prev.cursor }))
    return
  }
  await update($, snap, () => s)
  for (const m of messages(s)) {
    if (toasted.has(m.id) || m.from === ME || !m.unread || !(m.mention || m.reply_to_me)) continue
    toasted.add(m.id)
    if (primed) {
      $.ui.toast(`${m.reply_to_me ? '↳' : '@'} ${m.from}: ${short(m.text, 160)}`)
    }
  }
  primed = true
}

function ensurePolling($: $T): void {
  paneOpen = true
  if (!paneTimer) {
    paneTimer = $.clock.every(PANE_POLL_MS, () => {
      void refresh($)
    })
  }
}

function stopPolling(): void {
  paneOpen = false
  paneTimer?.cancel()
  paneTimer = undefined
}

async function openPane($: $T): Promise<boolean> {
  await refresh($, true)
  await update($, mode, () => 'normal' as BusMode)
  const opened = await $.ui.open({ id: PANE, title: 'Bus', focus: true, closeOnEscape: true, columns: 56 })
  ensurePolling($)
  return opened.isPlaced
}

async function closePane($: $T): Promise<void> {
  stopPolling()
  await $.ui.close({ id: PANE })
}

async function focusComposer($: $T): Promise<void> {
  try {
    await $.ui.focus({ requestId: PANE, key: 'composer' })
  } catch {
    // the site does not hold the keys (a click elsewhere): the person clicks the field
  }
}

async function expand($: $T, m: BusMessage | undefined): Promise<void> {
  if (!m) return
  const cur = await read($, expanded)
  if (cur?.id === m.id) {
    await update($, expanded, () => null)
    return
  }
  await update($, expanded, () => ({ id: m.id, body: null, loading: m.body }))
  if (m.unread && m.rid) {
    await runBus($, cfg.python, ['read', m.rid, '--as', ME], 15000)
  }
  if (m.body) {
    const r = await runBus($, cfg.python, ['snapshot', '--body', m.id], 15000)
    let body: string | null = null
    try {
      body = (JSON.parse(r.stdout) as { body: string | null }).body
    } catch {
      body = null
    }
    await update($, expanded, d => (d?.id === m.id ? { id: m.id, body: body ?? '(body unreadable)', loading: false } : d))
  }
  if (m.unread) await refresh($, true)
}

async function sendDraft($: $T, text: string): Promise<void> {
  const t = text.trim()
  if (t === '') return
  const s = await read($, snap)
  const re = await read($, replyTo)
  const parent = messages(s).find(m => m.id === re)
  const roles = (s?.roles ?? []).map(r => r.name)
  const r = await runBus($, cfg.python, sendArgs(t, parent, roles), 20000)
  if (r.exitCode !== 0) {
    await update($, notice, () => `send failed: ${short(r.stderr.trim() || r.stdout.trim(), 200)}`)
    return
  }
  await update($, draft, () => '')
  await update($, replyTo, () => null)
  await update($, notice, () => `sent ${short(r.stdout.trim().split('\n')[0] ?? '', 120)}`)
  await refresh($, true)
}

async function react($: $T, emoji: string): Promise<void> {
  const s = await read($, snap)
  const ms = messages(s)
  const m = ms[selIndex(ms, await read($, selected))]
  await update($, mode, () => 'normal' as BusMode)
  if (!m) return
  const r = await runBus($, cfg.python, ['react', m.id, emoji, '--as', ME], 15000)
  await update($, notice, () => (r.exitCode === 0 ? `${emoji} on ${m.id}` : `react failed: ${short(r.stderr.trim(), 200)}`))
  await refresh($, true)
}

/** Every key and button lands here, so the vim keys and the hotkeys do the same thing. */
async function act($: $T, action: string): Promise<void> {
  const s = await read($, snap)
  const ms = messages(s)
  const i = selIndex(ms, await read($, selected))
  const move = async (to: number) => {
    const m = ms[Math.max(0, Math.min(ms.length - 1, to))]
    if (m) await update($, selected, () => m.id)
  }
  switch (action) {
    case 'down': return move(i + 1)
    case 'up': return move(i - 1)
    case 'top': return move(0)
    case 'bottom': return move(ms.length - 1)
    case 'pagedown': return move(i + 8)
    case 'pageup': return move(i - 8)
    case 'expand': return expand($, ms[i])
    case 'collapse':
      await update($, expanded, () => null)
      return
    case 'reply':
      if (ms[i]) {
        await update($, replyTo, () => ms[i]?.id ?? null)
        await focusComposer($)
      }
      return
    case 'react':
      await update($, mode, m => (m === 'react' ? 'normal' : 'react') as BusMode)
      return
    case 'compose':
      await focusComposer($)
      return
    case 'cancel':
      await update($, mode, () => 'normal' as BusMode)
      await update($, replyTo, () => null)
      return
    case 'close':
      return closePane($)
    default:
      if (action.startsWith('emoji:')) {
        const e = EMOJI[Number(action.slice(6)) - 1]
        if (e) return react($, e)
      }
  }
}

/** A raw key from the Client region, vim style. */
export function keyAction(k: string, ctrl: boolean, shift: boolean, reacting: boolean, g: boolean): { action?: string; g: boolean } {
  if (ctrl) {
    if (k === 'd') return { action: 'pagedown', g: false }
    if (k === 'u') return { action: 'pageup', g: false }
    return { g: false }
  }
  if (reacting && /^[1-6]$/.test(k)) return { action: `emoji:${k}`, g: false }
  if (k === 'G' || (k === 'g' && shift)) return { action: 'bottom', g: false }
  if (k === 'g') return g ? { action: 'top', g: false } : { g: true }
  const map: Record<string, string> = {
    j: 'down', down: 'down', k: 'up', up: 'up', return: 'expand', o: 'expand', l: 'expand', h: 'collapse',
    r: 'reply', e: 'react', q: 'close', i: 'compose', a: 'compose', x: 'cancel', pagedown: 'pagedown', pageup: 'pageup',
    home: 'top', end: 'bottom',
  }
  return { action: map[k], g: false }
}

// ------------------------------------------------------------------------------------------- register

export const register: Register = (on, options) => {
  const python = str(options.python, 'python')
  const monitorOn = str(options.monitor, 'on') === 'on'
  cfg.python = python
  cfg.band = str(options.band, 'on') !== 'off'
  cfg.theme = THEMES[str(options.theme, 'tokyonight')] ?? (THEMES.tokyonight as Theme)

  // Module state: a reload starts it over, which only costs one extra look at the inbox.
  let known: HookOut | undefined
  let lastSize = -1
  let callsSinceLook = 0
  let lastReminder = 0

  on('session.start', async ($, e, next) => {
    // Every Bash child and every bus.py run inherits these, so the model's own commands
    // and a Monitor's watch use the same configuration as the hooks.
    await $.env.set('AGENT_BUS_DIR', str(options.busDir, '.claude/bus'))
    await $.env.set('AGENT_BUS_BACKEND', str(options.backend, 'none'))
    await $.env.set('AGENT_BUS_NUDGE_AFTER', String(options.nudgeAfter ?? 300))
    await $.env.set('AGENT_BUS_CHAT_PORT', String(options.chatPort ?? 8790))
    await $.env.set('AGENT_BUS_CHAT_BIND', str(options.chatBind, '127.0.0.1'))
    await $.env.set('AGENT_BUS_ROLES', str(options.roles, 'coordinator'))
    await $.env.set('AGENT_BUS_PYTHON', python)
    // Lets other tools (the coordinator plugin, shell workers) find this CLI.
    await $.env.set('AGENT_BUS_CLI', `${$.plugin.root}/scripts/bus.py`)
    await $.env.set('AGENT_BUS_TASK_PREFIX', str(options.taskPrefix, 'TASK'))
    if (str(options.orcaBusPath, '') !== '') {
      await $.env.set('AGENT_BUS_ORCA_BUS_PATH', str(options.orcaBusPath, ''))
    }
    if (str(options.tasksDb, '') !== '') {
      await $.env.set('AGENT_BUS_TASKS_DB', str(options.tasksDb, ''))
    }
    if (str(options.role, '') !== '' && (await $.env.get('AGENT_BUS_ROLE')) === undefined) {
      await $.env.set('AGENT_BUS_ROLE', str(options.role, ''))
    }
    await $.command.register({
      name: 'bus',
      description: 'agent-bus: open the chat pane; `full` opens the full-screen client, `chat` the web chat; or any bus verb.',
      argumentHint: '[@role text|full|chat|digest|who|init|send|status <id>|help]',
    })
    if (cfg.band) {
      // the band's slow look; the pane polls faster while it is open
      $.clock.every(BAND_POLL_MS, () => {
        const now = Date.now()
        // no bus in this project: look again only now and then (one may be created mid-session)
        if (paneOpen || (busFound === false && now - lastBandLook < 8 * BAND_POLL_MS)) return
        lastBandLook = now
        void refresh($)
      })
    }
    return next(e)
  })

  on('command.run', { command: 'bus' }, async ($, e) => {
    const args = busArgs(e.args)
    if (typeof args === 'string') {
      return { text: args }
    }
    if (args === null) {
      const placed = await openPane($)
      const s = await read($, snap)
      if (s && !s.bus) {
        return { text: noBusText(s.dir) }
      }
      return {
        text: placed
          ? `bus: chat open as ${ME} (j/k move, Enter/o expand, r reply, e react, q or Esc close).`
          : 'bus: the chat pane is waiting for room; widen the terminal.',
      }
    }
    const ran = await runBus($, python, args, 30000)
    const text = [ran.stdout.trim(), ran.stderr.trim()].filter(t => t !== '').join('\n')
    return { text: text === '' ? `bus ${args.join(' ')}: done` : text }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) stopPolling()
    return next(e)
  }).catch(($, e, next) => next(e))

  on('ui.message', async ($, e, next) => {
    if (e.requestId !== PANE || e.element !== 'keys') return next(e)
    const d = e.data as { n?: unknown; keys?: unknown }
    const n = typeof d?.n === 'number' ? d.n : 0
    const keys = Array.isArray(d?.keys) ? (d.keys as { i: number; k: string; ctrl?: boolean; shift?: boolean }[]) : []
    if (n < lastKey) lastKey = 0 // a new instance counts from one again
    for (const k of keys) {
      if (typeof k?.i !== 'number' || typeof k.k !== 'string' || k.i <= lastKey) continue
      lastKey = k.i
      const reacting = (await read($, mode)) === 'react'
      const r = keyAction(k.k, k.ctrl === true, k.shift === true, reacting, gPending)
      gPending = r.g
      if (r.action) await act($, r.action)
    }
    return next(e)
  })

  // ----------------------------------------------------------------------------------------- the band

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!cfg.band || paneOpen || e.props.hasSurvey) return next(e)
    const s = await read($, snap)
    const unread = s?.unread ?? 0
    const at = s?.mentions ?? 0
    if (!s?.bus || (unread === 0 && at === 0)) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const t = cfg.theme
    return (
      <Box gap={1}>
        <Text key="bus-band" bold color={t.accent}>bus:</Text>
        <Text key="bus-unread">{unread} unread</Text>
        {at > 0 && <Text key="bus-at" color={t.warn}>· {at} @you</Text>}
        <Button key="bus-open" label="Open" onPress={async () => { await openPane($) }} />
      </Box>
    )
  })

  // ----------------------------------------------------------------------------------------- the pane

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    ensurePolling($)
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const Input = 'Input' in els ? els.Input : null
    const Client = 'Client' in els ? els.Client : null
    const t = cfg.theme
    const s = await read($, snap)
    const err = await read($, error)
    const cols = Math.max(20, e.props.bodyColumns)
    const bodyRows = Math.max(8, e.props.scroll?.bodyRows ?? e.viewport?.rows ?? 24)

    if (!s) {
      return (
        <Box flexDirection="column">
          <Text color={err ? t.err : t.dim}>{err ?? 'loading the bus…'}</Text>
          <Button key="retry" hotkey="g" label="retry" onPress={async () => { await refresh($, true) }} />
        </Box>
      )
    }
    if (!s.bus) {
      return (
        <Box flexDirection="column">
          <Text>No bus in this project.</Text>
          <Text dimColor>`/bus init` creates {s.dir ?? '.claude/bus'}</Text>
          <Text dimColor>A bus elsewhere: set "busDir" under "agent-bus" in .claude/claude-mods.json</Text>
        </Box>
      )
    }

    const ms = messages(s)
    const sel = selIndex(ms, await read($, selected))
    const ex = await read($, expanded)
    const md = await read($, mode)
    const dr = await read($, draft)
    const re = await read($, replyTo)
    const note = await read($, notice)
    const roles = (s.roles ?? []).map(r => r.name)
    const health = s.health
    const nameW = cols < 50 ? 8 : 11

    // header: who, unread, @you, bus health
    const header = (
      <Box key="hdr" gap={1}>
        <Text bold color={t.accent}>bus</Text>
        <Text color={t.me}>{ME}</Text>
        {(s.unread ?? 0) > 0 && <Text color={t.ok}>●{s.unread}</Text>}
        {(s.mentions ?? 0) > 0 && <Text color={t.warn}>@{s.mentions}</Text>}
        <Text color={health?.deliver ? t.ok : t.dim}>{health?.deliver ? 'dlv✓' : 'dlv·'}</Text>
        {health?.last_write_age !== null && health?.last_write_age !== undefined && (
          <Text dimColor>{age(health.last_write_age)}</Text>
        )}
      </Box>
    )

    // the expanded message's lines
    const exMsg = ex ? ms.find(m => m.id === ex.id) : undefined
    const detail: RenderChildren[] = []
    if (exMsg && ex) {
      detail.push(<Text key="d-text" wrap="wrap">{exMsg.deleted ? '(deleted)' : exMsg.text}</Text>)
      const dest = [...exMsg.to, ...exMsg.cc.map(c => `cc ${c}`)].join(', ') || (exMsg.chan ? `#${exMsg.chan}` : '#all')
      detail.push(<Text key="d-to" dimColor wrap="truncate-end">{`${exMsg.id} → ${dest}${exMsg.topic ? ` [${exMsg.topic}]` : ''}`}</Text>)
      if (exMsg.receipts.length > 0) {
        detail.push(
          <Text key="d-rc" wrap="wrap" color={t.dim}>
            {exMsg.receipts.map(r => `${r.to} ${r.tick} ${r.r}`).join(' · ')}
          </Text>,
        )
      }
      const rx = Object.entries(exMsg.reactions).filter(([, by]) => by.length > 0)
      if (rx.length > 0) {
        detail.push(<Text key="d-rx" wrap="wrap">{rx.map(([em, by]) => `${em} ${by.join(',')}`).join('  ')}</Text>)
      }
      if (exMsg.body) {
        const lines = ex.loading ? ['(loading body…)'] : (ex.body ?? '').split('\n').slice(0, 60)
        lines.forEach((l, n) => detail.push(<Text key={`d-b${n}`} wrap="wrap">{l === '' ? ' ' : l}</Text>))
      }
    }

    // rows that fit: the bottom block is the composer, the key bar, the key region and one optional row
    const extra = (md === 'react' ? 1 : 0) + (completions(dr, roles).length > 0 ? 1 : 0) + (note || err ? 1 : 0)
    const fit = Math.max(1, bodyRows - 4 - extra - (exMsg ? Math.min(detail.length, bodyRows - 6) : 0))
    const start = Math.max(0, Math.min(sel - Math.floor(fit / 2), ms.length - fit))
    const shown = ms.slice(start, start + fit)

    const rows = shown.map((m, n) => {
      const idx = start + n
      const isSel = idx === sel
      const mine = m.from === ME
      const who = mine ? `→${m.to[0] ?? (m.chan ? `#${m.chan}` : '#all')}` : m.from
      const marks = `${m.unread ? '●' : ' '}${m.mention || m.reply_to_me ? '@' : ' '}`
      const tick = mine ? ` ${tickOf(m)}` : ''
      const rx = Object.entries(m.reactions).filter(([, by]) => by.length > 0).map(([em]) => em).join('')
      return (
        <Box key={`row-${m.id}`} flexDirection="column">
          <Button key={`msg-${m.id}`} plain onPress={async () => { await update($, selected, () => m.id); await expand($, m) }}>
            <Text color={isSel ? t.accent : undefined} bold={isSel}>{isSel ? '›' : ' '}</Text>
            <Text color={m.mention || m.reply_to_me ? t.warn : t.ok}>{marks}</Text>
            <Text dimColor>{m.time.slice(11, 16)} </Text>
            <Text color={mine ? t.me : t.accent}>{short(who, nameW)}</Text>
            <Text color={t.dim}>{tick} </Text>
            <Text backgroundColor={isSel ? t.selBg : undefined} wrap="truncate-end">
              {`${m.reply_to ? '↳' : ''}${m.deleted ? '(deleted)' : m.text}${m.body ? ' ¶' : ''}${rx ? ` ${rx}` : ''}`}
            </Text>
          </Button>
          {ex && ex.id === m.id && (
            <Box key={`det-${m.id}`} flexDirection="column" paddingLeft={2}>
              {detail}
            </Box>
          )}
        </Box>
      )
    })

    const sugg = completions(dr, roles)
    const parent = re ? ms.find(m => m.id === re) : undefined

    return (
      <Box flexDirection="column">
        {header}
        {ms.length === 0 ? <Text key="empty" dimColor>No messages yet.</Text> : <Box key="list" flexDirection="column">{rows}</Box>}
        {(note || err) && <Text key="note" color={err ? t.err : t.dim} wrap="truncate-end">{err ?? note}</Text>}
        {md === 'react' && (
          <Box key="react-row" gap={1}>
            {EMOJI.map((em, n) => (
              <Button key={`rx-${n + 1}`} plain hotkey={String(n + 1)} label={em} onPress={async () => { await react($, em) }} />
            ))}
          </Box>
        )}
        {sugg.length > 0 && (
          <Box key="ac-row" gap={1}>
            {sugg.map(r => (
              <Button
                key={`ac-${r}`}
                plain
                label={`@${r}`}
                onPress={async () => {
                  await update($, draft, d => d.replace(/@[\w.-]*$/, `@${r} `))
                  await focusComposer($)
                }}
              />
            ))}
          </Box>
        )}
        {Input ? (
          <Input
            key="composer"
            label={parent ? `↳${short(parent.from, 10)} ` : '› '}
            placeholder={cols < 50 ? '@role message' : '@role message · Enter sends'}
            value={dr}
            submitLabel="send"
            onInput={async (v: string) => { await update($, draft, () => v) }}
            onSubmit={async (v: string) => { await sendDraft($, v) }}
          />
        ) : (
          <Text key="no-input" dimColor>this surface has no text field: compose with /bus send</Text>
        )}
        <Box key="keybar" gap={1}>
          <Button key="k-down" plain hotkey="j" label="↓" onPress={async () => { await act($, 'down') }} />
          <Button key="k-up" plain hotkey="k" label="↑" onPress={async () => { await act($, 'up') }} />
          <Button key="k-open" plain hotkey="o" label="open" onPress={async () => { await act($, 'expand') }} />
          <Button key="k-reply" plain hotkey="r" label="reply" onPress={async () => { await act($, 'reply') }} />
          <Button key="k-react" plain hotkey="e" label="react" onPress={async () => { await act($, 'react') }} />
          {(re || md === 'react') && <Button key="k-cancel" plain hotkey="x" label="cancel" onPress={async () => { await act($, 'cancel') }} />}
          <Button key="k-close" plain hotkey="q" label="close" onPress={async () => { await act($, 'close') }} />
        </Box>
        {Client && (
          <Client
            key="keys"
            module="./keys.tsx"
            props={{
              mode: md === 'react' ? 'react' : re ? 'reply' : 'normal',
              hint: cols < 50 ? 'click: vim keys' : 'click here: j k gg G ^d ^u ↵ r e i q',
              accent: t.accent,
            }}
          />
        )}
      </Box>
    )
  })

  // ----------------------------------------------------------------------------------------- mail for agents

  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    const out = await hook($, python, 'SessionStart')
    known = out
    if (!out.bus) {
      return result
    }
    const lines: string[] = []
    if (!out.role) {
      lines.push(
        `agent-bus: a bus is set up here (${out.dir}) but this session has no role. To join: ` +
          `\`${out.cli} register <role> --kind claude --mode file\` and set AGENT_BUS_ROLE=<role>.`,
      )
    } else {
      lines.push(
        `agent-bus: you are \`${out.role}\`. Bus CLI: \`${out.cli}\` (send <to> "<one line>" [--prio urgent|normal|fyi] ` +
          '[--re <id>], digest, read <id>, react <id> <emoji>). New mail reaches you after tool calls; no acks.',
      )
      if (monitorOn && out.registered && !out.watch) {
        lines.push(armLine(out))
      }
      if (out.text) {
        lines.push(out.text)
      }
    }
    return { ...result, additionalContext: [...(result.additionalContext ?? []), lines.join('\n')] }
  }).catch(($, e, next) => next(e))

  on('classic.PostToolUse', async ($, e, next) => {
    const result = await next(e)
    callsSinceLook += 1
    if (known !== undefined && (!known.bus || !known.role)) {
      // No bus or no role yet: look again now and then (a tab may register mid-session).
      if (callsSinceLook < RECHECK_ROLE_EVERY) {
        return result
      }
    } else if (known?.dir && known.role) {
      // The cheap path: only run Python when the inbox file grew.
      try {
        const st = await $.fs.stat(`${known.dir}/inbox/${known.role}.jsonl`)
        if (st.size === lastSize) {
          return result
        }
        lastSize = st.size
      } catch {
        return result
      }
    }
    callsSinceLook = 0
    const out = await hook($, python, 'PostToolUse')
    known = out
    if (!out.text) {
      return result
    }
    return { ...result, additionalContext: [...(result.additionalContext ?? []), out.text] }
  }).catch(($, e, next) => next(e))

  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    if (e.stop_hook_active || result.block !== undefined) {
      return result // already kept going once: let the agent stop
    }
    const out = await hook($, python, 'Stop')
    known = out
    if (!out.bus || !out.role) {
      return result
    }
    if (out.text) {
      return { ...result, block: out.text }
    }
    const now = Date.now()
    if (monitorOn && out.registered && !out.watch && now - lastReminder > REMIND_EVERY_MS) {
      lastReminder = now
      return { ...result, block: `${armLine(out)} Then you may go idle.` }
    }
    return result
  }).catch(($, e, next) => next(e))
}

function age(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.round(sec / 60)}m`
  return `${Math.round(sec / 3600)}h`
}
