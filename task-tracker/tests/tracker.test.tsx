import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const SNAP = {
  me: { name: 'user', level: 'admin', via: 'default' },
  prefix: 'TASK',
  stats: {
    by_status: { backlog: 2, todo: 1, in_progress: 2, in_review: 1, blocked: 1, done: 3, cancelled: 0 },
    done_today: 1, conflicts: 1, proposed: 0, last_event: 41,
    areas: { api: { backlog: 0, todo: 1, in_progress: 2, in_review: 0, blocked: 1, done: 1, cancelled: 0 } },
    labels: { bug: { backlog: 0, todo: 1, in_progress: 2, in_review: 1, blocked: 0, done: 3, cancelled: 0 } },
  },
  counts: { todo: 1, in_progress: 2, in_review: 1, blocked: 1, done: 1 },
  board: {
    todo: [{ key: 'TASK-1', title: 'Write the parser', priority: 'high', assignee: null, area: 'api', proposed: null }],
    in_progress: [
      { key: 'TASK-2', title: 'Refactor the API layer', priority: 'urgent', assignee: 'worker-a', area: 'api', proposed: null },
      { key: 'TASK-3', title: 'Fix the handler', priority: 'med', assignee: 'worker-b', area: 'api', proposed: null },
    ],
    in_review: [{ key: 'TASK-4', title: 'Docs pass', priority: 'med', assignee: 'worker-a', area: 'docs', proposed: null }],
    blocked: [{ key: 'TASK-5', title: 'Waits on the schema', priority: 'low', assignee: null, area: null, proposed: null }],
    done: [{ key: 'TASK-6', title: 'Done thing', priority: 'med', assignee: 'worker-b', area: 'api', proposed: null }],
  },
  events: [],
  conflicts: [{ a: 'TASK-2', a_title: 'Refactor the API layer', a_assignee: 'worker-a', a_glob: 'src/api/**', b: 'TASK-3', b_title: 'Fix the handler', b_assignee: 'worker-b', b_glob: 'src/api/handler.py' }],
  areas: ['api', 'docs'],
  labels: ['bug', 'feature'],
  review_for_me: [],
  unread_notes: 0,
}

const DETAIL = {
  key: 'TASK-2', title: 'Refactor the API layer', body: '**why:** the handlers repeat themselves', status: 'in_progress',
  priority: 'urgent', assignee: 'worker-a', proposed: null, reviewer: 'reviewer', creator: 'coordinator', area: 'api',
  parent: null, created: '2026-10-10T00:40:00+00:00', updated: '2026-10-10T00:41:00+00:00',
  labels: ['feature'], links: [{ kind: 'ref', value: 'issue-42' }], touches: ['src/api/**'], blocked_by: [],
  blocks: [], children: [], comments: [{ author: 'reviewer', ts: '2026-10-10T00:42:00+00:00', body: 'OK to start' }],
  history: [{ ts: '2026-10-10T00:41:00+00:00', actor: 'worker-a', action: 'claim', detail: {} }],
}

function where(dbExists: boolean) {
  return { root: '/work/proj', db: '/work/proj/.claude/tasks/tasks.db', db_exists: dbExists, roles: '/work/proj/.claude/tasks/roles.toml', roles_exists: false, prefix: 'TASK', labels: ['bug', 'feature'] }
}

type Call = { argv: string[]; env: Record<string, string> }

function fakeCli(on: On, dbExists: boolean, calls: Call[]) {
  on('session.start', async ($$, e) => ({ cwd: e.cwd }))
  on('command.register', async ($$, e) => ({ value: { command: e.name } }))
  on('classic.SessionStart', async () => ({}))
  on('session.root', async () => ({ value: '/work/proj' }))
  on('process.run', async ($$, e) => {
    const argv = [...e.argv]
    calls.push({ argv, env: { ...(e.init?.env ?? {}) } })
    let out = JSON.stringify(SNAP)
    if (argv.includes('where')) out = JSON.stringify(where(dbExists))
    else if (argv.includes('show')) out = argv.includes('--json') ? JSON.stringify(DETAIL) : 'TASK-2  Refactor the API layer'
    else if (argv.includes('init')) out = 'created /work/proj/.claude/tasks/tasks.db'
    return { value: { exitCode: 0, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
}

const PANE_PROPS = { title: 'Tasks', isFocused: true, bodyColumns: 140, placement: 'dock' as const, scroll: { offset: 0, bodyRows: 38 }, view: {} }

function run(args: string) {
  return { command: 'tasks', args, origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 150 } }
}

test('runs the bundled CLI with the options as environment, and tells the model the command', { options: { idPrefix: 'WEB', python: 'python3' } }, async ($, on) => {
  mock.env(on, {})
  mock.store(on)
  mock.clock(on)
  const calls: Call[] = []
  fakeCli(on, true, calls)
  on('ui.status', async () => ({ value: undefined }))
  const envSet: Record<string, string | undefined> = {}
  on('env.set', async ($$, e) => {
    envSet[e.name] = e.value
    return { value: undefined }
  })

  await $.session.start({ cwd: '/work/proj', surface: 'terminal', isInteractive: true })
  const first = calls[0]
  expect(first?.argv[0]).toBe('python3')
  expect(first?.argv[1]?.endsWith('/scripts/task.py')).toBe(true)
  expect(first?.env.TASKS_PREFIX).toBe('WEB')
  expect(first?.argv.includes('--prefix')).toBe(false)
  expect(envSet.TASKS_PREFIX).toBe('WEB')
  expect(String(envSet.TASKS_CLI).endsWith('/scripts/task.py')).toBe(true)

  const r = await $.classic.SessionStart({ source: 'startup' })
  const ctx = (r.additionalContext ?? []).join('\n')
  expect(ctx).toContain('python3 ')
  expect(ctx).toContain('scripts/task.py')
  expect(ctx).toContain('claim TASK-<n>')
})

test('without a tracker, the context is one line pointing at /tasks init', async ($, on) => {
  mock.env(on, {})
  mock.store(on)
  mock.clock(on)
  const calls: Call[] = []
  fakeCli(on, false, calls)
  const r = await $.classic.SessionStart({ source: 'startup' })
  const ctx = (r.additionalContext ?? []).join('\n')
  expect(ctx).toContain('/tasks init')
  expect(ctx.split('\n').length).toBe(1)
  expect(calls.some(c => c.argv.includes('snapshot'))).toBe(false)
})

test('the board pane draws columns, opens a task, and lists conflicts', async ($, on) => {
  mock.env(on, {})
  mock.store(on)
  mock.clock(on)
  const calls: Call[] = []
  fakeCli(on, true, calls)
  const statuses: (string | undefined)[] = []
  on('ui.status', async ($$, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.open', async () => ({ value: { isPlaced: true } }))

  const ran = await $.command.run(run('board'))
  expect(JSON.stringify(ran)).toContain('2 in progress')
  expect(calls.some(c => c.argv.includes('snapshot'))).toBe(true)
  expect(statuses.at(-1)).toContain('⚠ 1 conflict')

  const ui = await $.ui.mount({ plugin: 'task-tracker', surface: 'terminal', component: 'Pane', requestId: 'task-tracker', props: PANE_PROPS, viewport: { columns: 150, rows: 40 } })
  expect(await ui.find({ text: /In progress/ })).toBeDefined()
  expect(await ui.find({ key: 'card-TASK-2' })).toBeDefined()
  expect(await ui.find({ text: /1 conflicts/ })).toBeDefined()

  await ui.press({ key: 'card-TASK-2' })
  expect(await ui.find({ text: /OK to start/ })).toBeDefined()
  expect(await ui.find({ key: 'back' })).toBeDefined()

  await ui.press({ key: 'back' })
  await ui.press({ key: 'v-conflicts' })
  expect(await ui.find({ text: /overlaps/ })).toBeDefined()

  await ui.press({ key: 'v-progress' })
  expect(await ui.find({ text: /Areas/ })).toBeDefined()

  await ui.press({ key: 'v-list' })
  expect(await ui.find({ key: 'row-TASK-5' })).toBeDefined()
  await ui.unmount()

  const narrow = await $.ui.mount({ plugin: 'task-tracker', surface: 'terminal', component: 'Pane', requestId: 'task-tracker', props: { ...PANE_PROPS, bodyColumns: 70 }, viewport: { columns: 72, rows: 30 } })
  expect(await narrow.find({ key: 'row-TASK-1' })).toBeDefined()
  await narrow.unmount()
})

test('/tasks init creates the tracker when there is none', async ($, on) => {
  mock.env(on, {})
  mock.store(on)
  mock.clock(on)
  const calls: Call[] = []
  fakeCli(on, false, calls)
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  const ran = await $.command.run(run('init'))
  expect(calls.some(c => c.argv.includes('init'))).toBe(true)
  expect(JSON.stringify(ran)).toContain('created')
})

for (const surface of ['terminal', 'desktop'] as const) {
  test(`the pane draws and opens a task on ${surface}`, async ($, on) => {
    mock.env(on, {})
    mock.store(on)
    mock.clock(on)
    fakeCli(on, true, [])
    on('ui.status', async () => ({ value: undefined }))
    on('ui.open', async () => ({ value: { isPlaced: true } }))
    await $.command.run(run('board'))
    const ui = await $.ui.mount({ plugin: 'task-tracker', surface, component: 'Pane', requestId: 'task-tracker', props: PANE_PROPS, viewport: { columns: 150, rows: 40 } })
    expect(await ui.find({ key: 'card-TASK-2' })).toBeDefined()
    await ui.press({ key: 'card-TASK-2' })
    expect(await ui.find({ text: /OK to start/ })).toBeDefined()
    await ui.unmount()
  })
}
