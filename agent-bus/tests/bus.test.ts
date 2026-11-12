import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'
import { BUS_HELP, busArgs, noBusText } from '../hooks/register'

const CLI = 'python "/plugins/agent-bus/scripts/bus.py"'

type Out = Record<string, unknown>

/** Answers every `bus.py hook` run with `out`, and counts the runs (and their working directories). */
function answerHook(on: On, out: Out, runs: string[][] = [], root = '/repo', cwds: (string | undefined)[] = []) {
  // No settings hooks beneath the plugin: each classic event answers nothing of its own.
  on('classic.SessionStart', () => ({}))
  on('classic.PostToolUse', () => ({}))
  on('classic.Stop', () => ({}))
  on('session.root', () => ({ value: root }))
  on('process.run', ($, e) => {
    runs.push([...e.argv])
    cwds.push(e.init?.cwd)
    return {
      value: {
        exitCode: 0,
        stdout: JSON.stringify(out),
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  return runs
}

const BASE = { bus: true, dir: '/repo/.claude/bus', cli: CLI, role: 'worker-a', registered: true }

const POST_TOOL = {
  tool_name: 'Bash',
  tool_input: { command: 'ls' },
  tool_response: { stdout: '', stderr: '' },
  tool_use_id: 'toolu_1',
} as never

test('session start tells a registered role its CLI and asks for a Monitor when no watch is live', async ($, on) => {
  answerHook(on, { ...BASE, watch: false, text: null })
  const r = await $.classic.SessionStart({ source: 'startup' } as never)
  const text = (r.additionalContext ?? []).join('\n')
  expect(text).toContain('you are `worker-a`')
  expect(text).toContain(`${CLI} watch --as worker-a --for 1790`)
  expect(text).toContain('1800000')
})

test('session start does not ask for a Monitor while a watch is live', async ($, on) => {
  answerHook(on, { ...BASE, watch: true, text: null })
  const r = await $.classic.SessionStart({ source: 'startup' } as never)
  expect((r.additionalContext ?? []).join('\n')).not.toContain('Monitor')
})

test('session start adds nothing in a project without a bus', async ($, on) => {
  answerHook(on, { bus: false })
  const r = await $.classic.SessionStart({ source: 'startup' } as never)
  expect(r.additionalContext ?? []).toEqual([])
})

test('session start in a project without a bus still shows a global pause in force', async ($, on) => {
  const runs = answerHook(on, { bus: false, text: '[GLOBAL PAUSE in force since 2026-11-09 10:00, by a/coordinator] hot' })
  const r = await $.classic.SessionStart({ source: 'startup', session_id: 'sess-1' } as never)
  expect((r.additionalContext ?? []).join('\n')).toContain('GLOBAL PAUSE in force')
  expect(runs[0]).toContain('--session')
  expect(runs[0]).toContain('sess-1')
})

test('an agent without a bus about to idle is kept going once with a global message', async ($, on) => {
  answerHook(on, { bus: false, text: '[GLOBAL from a/coordinator g1] stop' })
  const r = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(r.block).toContain('[GLOBAL from a/coordinator g1] stop')
})

test('/bus broadcast and /bus global reach the CLI', async () => {
  expect(busArgs('broadcast --pause hot board')).toEqual(['broadcast', '--pause', 'hot', 'board'])
  expect(busArgs('global status')).toEqual(['global', 'status'])
})

test('new mail is handed to the model after a tool call', async ($, on) => {
  answerHook(on, { ...BASE, watch: true, text: 'Bus mail for worker-a (1 new): [bus m1 from coordinator] go' })
  const r = await $.classic.PostToolUse(POST_TOOL)
  expect((r.additionalContext ?? []).join('\n')).toContain('[bus m1 from coordinator] go')
})

test('an agent about to idle with unread mail is kept going once with the mail', async ($, on) => {
  answerHook(on, { ...BASE, watch: true, text: 'Bus mail for worker-a: [bus m2 from coordinator] next' })
  const r = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(r.block).toContain('m2')
  const again = await $.classic.Stop({ stop_hook_active: true } as never)
  expect(again.block).toBeUndefined()
})

test('an agent about to idle without a live watch is asked to arm one, once', async ($, on) => {
  answerHook(on, { ...BASE, watch: false, text: null })
  const first = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(first.block).toContain('watch --as worker-a')
  const second = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(second.block).toBeUndefined()
})

test('a live watch lets the agent idle', async ($, on) => {
  answerHook(on, { ...BASE, watch: true, text: null })
  const r = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(r.block).toBeUndefined()
})

test('the monitor option off never asks for a Monitor', { options: { monitor: 'off' } }, async ($, on) => {
  answerHook(on, { ...BASE, watch: false, text: null })
  const r = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(r.block).toBeUndefined()
})

test('/bus arguments map to bus.py verbs; none opens the pane', async () => {
  expect(busArgs('')).toBeNull()
  expect(busArgs('  ')).toBeNull()
  expect(busArgs('digest')).toEqual(['digest'])
  expect(busArgs('full')).toEqual(['tui', '--open'])
  expect(busArgs('full --theme claude')).toEqual(['tui', '--open', '--theme', 'claude'])
  expect(busArgs('chat')).toEqual(['chat', '--detach'])
  expect(busArgs('chat --stop')).toEqual(['chat', '--stop'])
  expect(busArgs('tui')).toEqual(['tui', '--open'])
  expect(busArgs('status m1')).toEqual(['status', 'm1'])
})

test('/bus free text never reaches the CLI: @role sends as user, anything else shows help', async () => {
  expect(busArgs('pane')).toBeNull()
  expect(busArgs('@coordinator rebase before the next slice')).toEqual(
    ['send', '--from', 'user', '--', 'coordinator', 'rebase before the next slice'])
  expect(busArgs('@w1, @w2 stop now')).toEqual(['send', '--from', 'user', '--', 'w1,w2', 'stop now'])
  const says = busArgs("says there's no bus in this project. what the heck?")
  expect(typeof says).toBe('string')
  expect(says as string).toContain('"says" is not a subcommand')
  expect(says as string).toContain(BUS_HELP)
  expect(busArgs('help')).toBe(BUS_HELP)
  expect(typeof busArgs('@coordinator')).toBe('string')
  expect(busArgs('send w1 hi')).toEqual(['send', 'w1', 'hi'])
  expect(busArgs('init --roles a,b')).toEqual(['init', '--roles', 'a,b'])
})

test('the no-bus message points at busDir in .claude/claude-mods.json', async () => {
  const t = noBusText('/p/.claude/bus')
  expect(t).toContain('/bus init')
  expect(t).toContain('"busDir"')
  expect(t).toContain('.claude/claude-mods.json')
})

test('bus.py runs in the session project root, not wherever the agent cd-ed to', async ($, on) => {
  const cwds: (string | undefined)[] = []
  answerHook(on, { ...BASE, watch: true, text: null }, [], '/work/proj', cwds)
  on('session.cwd', async () => ({ value: '/work/other-repo' }))
  await $.classic.Stop({ stop_hook_active: false } as never)
  expect(cwds.length).toBeGreaterThan(0)
  expect(cwds.every(c => c === '/work/proj')).toBe(true)
})

test('the watch it asks for is pinned to the bus the hook found', async ($, on) => {
  answerHook(on, { ...BASE, watch: false, text: null })
  const r = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(r.block).toContain('watch --as worker-a --for 1790 --dir "/repo/.claude/bus"')
})

test('an idle role with nothing live to send it mail is not asked to arm a watch', async ($, on) => {
  answerHook(on, { ...BASE, role: 'coordinator', watch: false, text: null, needWatch: false })
  const start = await $.classic.SessionStart({ source: 'startup' } as never)
  expect((start.additionalContext ?? []).join('\n')).not.toContain('Monitor')
  const stop = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(stop.block).toBeUndefined()
})

test('mail still wakes an idle role that needs no watch', async ($, on) => {
  answerHook(on, { ...BASE, watch: false, needWatch: true, text: 'Bus mail for worker-a: [bus m3 from user] hi' })
  const r = await $.classic.Stop({ stop_hook_active: false } as never)
  expect(r.block).toContain('[bus m3 from user] hi')
})
