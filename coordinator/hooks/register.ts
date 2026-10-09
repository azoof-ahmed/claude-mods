import type { EngineInterface, Register } from 'claude-code'

/** What `coord.py context` prints. */
export type CoordContext = {
  configured: boolean
  role: string
  isCoordinator: boolean
  rules: string
  card: string | null
  cardExists: boolean
  live: string[]
  questions: number
  retireTokens: number
  pauseAtPercent: number
  maxLanes: number
}

const USAGE_EVERY_CALLS = 10
const RETIRE_STEP = 0.1 // remind again every further 10% past the threshold

function str(v: unknown, fallback: string): string {
  return typeof v === 'string' && v !== '' ? v : fallback
}

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback
}

/** Runs `coord.py <args>` from this plugin's folder. */
async function runCoord($: EngineInterface, python: string, args: string[], timeoutMs: number) {
  return $.process.run([python, `${$.plugin.root}/scripts/coord.py`, ...args], { timeoutMs })
}

/** Asks coord.py whether this project is coordinated and who this session is. */
async function readContext($: EngineInterface, python: string): Promise<CoordContext | undefined> {
  const ran = await runCoord($, python, ['context'], 15000)
  if (ran.exitCode !== 0) {
    return undefined
  }
  return JSON.parse(ran.stdout) as CoordContext
}

/** The command line a session uses for the coordinator CLI. */
export function cliFor(python: string, root: string): string {
  return `${python} "${root}/scripts/coord.py"`
}

/** The session-start lines for a coordinated project; none for any other project. */
export function contextLines(c: CoordContext, cli: string): string[] {
  if (!c.configured) {
    return []
  }
  if (c.isCoordinator) {
    const q = c.questions > 0 ? ` ${c.questions} question(s) wait for the user: raise them at the next status.` : ''
    return [
      `coordinator: this project runs coordinated worker lanes (${c.live.length} live of max ${c.maxLanes}). ` +
        `If you coordinate here, follow the coordinator skill. CLI: \`${cli}\` (status, lanes, spawn, retire, ` +
        `respawn, close-idle, questions, answer, preflight).${q}`,
    ]
  }
  const card = c.cardExists ? `your lane card ${c.card}` : `your lane card ${c.card} (not written yet: write it first)`
  return [
    `coordinator: you are the worker \`${c.role}\`. Read ${c.rules}, then ${card}. Coordinator CLI: \`${cli}\` ` +
      `(ask "<question>" queues a question for the user; preflight before heavy jobs). Retire at about ` +
      `${Math.round(c.retireTokens / 1000)}k context tokens (you will be reminded); end with SAFE TO CLOSE or KEEP OPEN: <reason>.`,
  ]
}

/** A reminder once context passes the retirement threshold, again at each further step; undefined otherwise. */
export function retireReminder(tokens: number, threshold: number, lastLevel: number): { level: number; text: string } | undefined {
  if (tokens < threshold) {
    return undefined
  }
  const level = Math.floor((tokens - threshold) / (threshold * RETIRE_STEP)) + 1
  if (level <= lastLevel) {
    return undefined
  }
  return {
    level,
    text:
      `coordinator: context is at ${Math.round(tokens / 1000)}k tokens, past the ${Math.round(threshold / 1000)}k ` +
      'retirement point. Finish or park the current step, update your lane card (state, next 3 actions, anchors, ' +
      'open messages), send the coordinator a FINAL, and end with SAFE TO CLOSE so a successor resumes from the card.',
  }
}

export const register: Register = (on, options) => {
  const python = str(options.python, 'python')

  let ctx: CoordContext | undefined
  let calls = 0
  let retireLevel = 0
  let pausedWindows = new Set<string>()

  on('session.start', async ($, e, next) => {
    // Bash children (the model's own coord.py runs, shell workers it starts) inherit these.
    await $.env.set('COORD_CLI', `${$.plugin.root}/scripts/coord.py`)
    await $.env.set('COORD_REPOS', str(options.repos, '.'))
    await $.env.set('COORD_BACKEND', str(options.backend, 'manual'))
    await $.env.set('COORD_SHELL', str(options.shell, 'auto'))
    await $.env.set('COORD_WORKER_COMMAND', str(options.workerCommand, 'claude --model {model} "{prompt}"'))
    await $.env.set('COORD_WORKER_PROMPT', str(options.workerPrompt, 'Read {brief} and execute it. Your role is {role}.'))
    await $.env.set('COORD_WORKER_MODEL', str(options.workerModel, 'opus'))
    await $.env.set('COORD_MAX_LANES', String(num(options.maxLanes, 6)))
    await $.env.set('COORD_RETIRE_TOKENS', String(num(options.retireTokens, 250000)))
    await $.env.set('COORD_PAUSE_AT_PERCENT', String(num(options.pauseAtPercent, 97)))
    await $.env.set('COORD_BUS_DIR', str(options.busDir, '.claude/bus'))
    await $.env.set('COORD_TRACKER_DB', str(options.trackerDb, '.claude/tasks/tasks.db'))
    await $.env.set('COORD_RULES_FILE', str(options.rulesFile, '.claude/coord/RULES_CORE.md'))
    await $.env.set('COORD_BRIEFS_DIR', str(options.briefsDir, '.claude/coord/briefs'))
    await $.env.set('COORD_CARDS_DIR', str(options.cardsDir, '.claude/coord/lanes'))
    await $.env.set('COORD_REPORTS_DIR', str(options.reportsDir, '.claude/coord/reports'))
    await $.env.set('COORD_MIN_FREE_RAM_GB', String(num(options.minFreeRamGb, 4)))
    await $.env.set('COORD_MIN_BATTERY', String(num(options.minBattery, 40)))
    await $.command.register({
      name: 'coordinator',
      description: 'Coordinate worker agents: init, status, lanes, spawn <role> [brief], retire, respawn, close-idle, questions, answer, preflight.',
      argumentHint: '[init|status|lanes|spawn <role> [brief]|retire <role>|respawn <role>|close-idle|questions|answer <id> <text>|preflight]',
    })
    return next(e)
  })

  on('command.run', { command: 'coordinator' }, async ($, e) => {
    const typed = e.args.trim()
    const args = typed === '' ? ['status'] : typed.split(/\s+/)
    const ran = await runCoord($, python, args, 120000)
    const text = [ran.stdout.trim(), ran.stderr.trim()].filter(t => t !== '').join('\n')
    const c = await readContext($, python)
    ctx = c ?? ctx
    if (c?.configured && c.isCoordinator) {
      $.ui.status(`coord: ${c.live.length}/${c.maxLanes} lanes${c.questions > 0 ? `, ${c.questions} question(s)` : ''}`)
    }
    return { text: text === '' ? `coordinator ${args.join(' ')}: done` : text }
  })

  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    const c = await readContext($, python)
    ctx = c ?? ctx
    if (c === undefined) {
      return result
    }
    const lines = contextLines(c, cliFor(python, $.plugin.root))
    if (lines.length === 0) {
      return result
    }
    if (c.isCoordinator) {
      $.ui.status(`coord: ${c.live.length}/${c.maxLanes} lanes${c.questions > 0 ? `, ${c.questions} question(s)` : ''}`)
    }
    return { ...result, additionalContext: [...(result.additionalContext ?? []), ...lines] }
  }).catch(($, e, next) => next(e))

  on('classic.PostToolUse', async ($, e, next) => {
    const result = await next(e)
    calls += 1
    if (ctx === undefined || !ctx.configured || calls % USAGE_EVERY_CALLS !== 0) {
      return result
    }
    const usage = await $.session.usage()
    const notes: string[] = []
    const tokens = usage.context.tokens ?? 0
    const reminder = retireReminder(tokens, ctx.retireTokens, retireLevel)
    if (reminder !== undefined) {
      retireLevel = reminder.level
      notes.push(reminder.text)
    }
    for (const w of usage.rateLimits) {
      if (w.percentUsed >= ctx.pauseAtPercent && !pausedWindows.has(w.kind)) {
        pausedWindows = new Set([...pausedWindows, w.kind])
        notes.push(
          `coordinator: the ${w.kind} usage window is at ${Math.round(w.percentUsed)}%` +
            `${w.resetsAt ? ` (resets ${w.resetsAt})` : ''}. Checkpoint: update your lane card, park the current step, ` +
            'tell the coordinator, and pause until it resets.',
        )
      }
    }
    if (notes.length === 0) {
      return result
    }
    return { ...result, additionalContext: [...(result.additionalContext ?? []), ...notes] }
  }).catch(($, e, next) => next(e))
}
