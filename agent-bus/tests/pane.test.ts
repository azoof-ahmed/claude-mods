import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import { completions, keyAction, sendArgs, tickOf } from '../hooks/register'
import type { BusMessage, BusSnapshot } from '../types'

function msg(id: string, from: string, text: string, extra: Partial<BusMessage> = {}): BusMessage {
  return {
    id, from, text, time: `2026-10-10T12:0${id.slice(-1)}:00+00:00`, body: false, prio: 'normal', topic: null,
    chan: null, thread: null, reply_to: null, to: ['user'], cc: [], reactions: {}, pinned: false, edited: false,
    deleted: false, receipts: [], rid: null, unread: false, mention: false, reply_to_me: false, ...extra,
  }
}

const MSGS: BusMessage[] = [
  msg('m1', 'user', 'kick off the build', {
    to: ['worker-a'],
    receipts: [{ to: 'worker-a', r: 'seen', tick: 'vv', why: 'to' }, { to: 'coordinator', r: 'delivered', tick: 'v', why: 'watch' }],
  }),
  msg('m2', 'worker-a', 'build is green', { reactions: { '👍': ['coordinator'] } }),
  msg('m3', 'coordinator', 'hey @user can you look', { unread: true, mention: true, rid: 'm3.1', body: true }),
]

function snapshot(unread: number, mentions: number): BusSnapshot {
  return {
    bus: true, me: 'user', cursor: `c${unread}`,
    health: { deliver: true, deliver_age: 2, watch: false, last_write_age: 5 },
    messages: MSGS, channels: ['all'], unread, mentions,
    roles: [
      { name: 'coordinator', kind: 'claude', mode: 'file', unread: 0, watch: true },
      { name: 'worker-a', kind: 'claude', mode: 'file', unread: 1, watch: false },
      { name: 'worker-b', kind: 'claude', mode: 'file', unread: 0, watch: false },
      { name: 'user', kind: 'shell', mode: 'inbox', unread, watch: false },
    ],
  }
}

type World = { calls: string[][]; closed: string[]; toasts: string[]; snap: BusSnapshot }

function fakeBus(on: On, world: World) {
  mock.env(on, {})
  mock.store(on)
  on('session.start', async ($$, e) => ({ cwd: e.cwd }))
  on('command.register', async ($$, e) => ({ value: { command: e.name } }))
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('ui.focus', async () => ({}))
  on('env.set', async () => ({ value: undefined }))
  on('ui.close', async ($$, e) => {
    world.closed.push(e.id)
    return { value: undefined }
  })
  // the engine's own band beneath the plugin: an empty row
  on('ui.render', { component: 'AbovePrompt' }, () => ({ type: 'Box', props: { key: 'engine-band' } }) as never)
  on('ui.toast', async ($$, e) => {
    world.toasts.push(e.text)
    return { value: undefined }
  })
  on('process.run', async ($$, e) => {
    const argv = [...e.argv].slice(2)
    world.calls.push(argv)
    let out = 'ok'
    if (argv[0] === 'snapshot') {
      out = argv.includes('--body') ? JSON.stringify({ id: 'm3', body: 'the long body\nsecond line' }) : JSON.stringify(world.snap)
    }
    return { value: { exitCode: 0, stdout: out, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
}

/** The row drawn with the cursor mark. */
async function selected(ui: { findAll: (q: { type: string }) => Promise<readonly { key?: string; text?: string }[]> }) {
  return (await ui.findAll({ type: 'Button' })).find(b => (b.text ?? '').startsWith('›'))?.key
}

const OPEN = { command: 'bus', args: '', origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 120 } }

function paneProps(columns: number) {
  return { title: 'Bus', isFocused: true, bodyColumns: columns, placement: 'dock' as const, scroll: { offset: 0, bodyRows: 30 }, view: {} }
}

const BAND_PROPS = { hasSurvey: false, isWorking: false, maxRows: 3, bodyColumns: 100, scroll: { offset: 0, bodyRows: 3 }, view: {} }

function world(unread = 1, mentions = 1): World {
  return { calls: [], closed: [], toasts: [], snap: snapshot(unread, mentions) }
}

for (const surface of ['terminal', 'desktop'] as const) {
  test(`/bus opens the pane; the list renders and j/k move the selection (${surface})`, async ($, on) => {
    mock.clock(on)
    const w = world()
    fakeBus(on, w)
    const r = await $.command.run(OPEN)
    expect(JSON.stringify(r)).toContain('chat open')
    expect(w.calls.some(c => c[0] === 'snapshot' && c.includes('--as') && c.includes('user'))).toBe(true)
    const ui = await $.ui.mount({ plugin: 'agent-bus', surface, component: 'Pane', requestId: 'bus', props: paneProps(56), viewport: { columns: 58, rows: 32 } })
    expect(await ui.find({ key: 'msg-m1' })).toBeDefined()
    expect(await ui.find({ text: /build is green/ })).toBeDefined()
    expect(await ui.find({ key: 'composer' })).toBeDefined()
    // the newest is selected first; own messages carry their tick
    expect(await selected(ui), 'cur-m3 first').toBe('msg-m3')
    expect(await ui.find({ type: 'Text', text: ' v ' }), 'tick').toBeDefined()
    // raw vim keys through the Client region
    await ui.key({ key: 'k' })
    expect(await selected(ui), 'after k').toBe('msg-m2')
    await ui.key({ key: 'k' })
    await ui.key({ key: 'k' })
    expect(await selected(ui)).toBe('msg-m1')
    await ui.key({ key: 'G' })
    expect(await selected(ui)).toBe('msg-m3')
    await ui.key({ key: 'g' })
    await ui.key({ key: 'g' })
    expect(await selected(ui)).toBe('msg-m1')
    // the hotkey Buttons do the same
    await ui.press({ key: 'k-down' })
    expect(await selected(ui)).toBe('msg-m2')
    await ui.unmount()
  })
}

test('Enter expands one message: full text, body, receipts, and marks it read', async ($, on) => {
  mock.clock(on)
  const w = world()
  fakeBus(on, w)
  await $.command.run(OPEN)
  const ui = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'Pane', requestId: 'bus', props: paneProps(56), viewport: { columns: 58, rows: 32 } })
  await ui.key({ key: 'return' })
  expect(w.calls.some(c => c.join(' ') === 'read m3.1 --as user')).toBe(true)
  expect(w.calls.some(c => c.join(' ') === 'snapshot --body m3')).toBe(true)
  expect(await ui.find({ text: /second line/ })).toBeDefined()
  await ui.key({ key: 'return' })
  expect(await ui.find({ text: /second line/ })).toBeUndefined()
  await ui.key({ key: 'g' })
  await ui.key({ key: 'g' })
  await ui.key({ key: 'return' })
  expect(await ui.find({ text: /worker-a vv seen · coordinator v delivered/ })).toBeDefined()
  await ui.unmount()
})

test('r replies: the composer sends as user with --re to the author', async ($, on) => {
  mock.clock(on)
  const w = world()
  fakeBus(on, w)
  await $.command.run(OPEN)
  const ui = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'Pane', requestId: 'bus', props: paneProps(56), viewport: { columns: 58, rows: 32 } })
  await ui.key({ key: 'r' })
  expect(await ui.find({ key: 'k-cancel' })).toBeDefined()
  await ui.input({ key: 'composer', text: 'on it' })
  const sent = w.calls.find(c => c[0] === 'send')
  expect(sent).toEqual(['send', '--from', 'user', '--re', 'm3', '--', 'coordinator', 'on it'])
  expect(await ui.find({ text: /sent ok/ })).toBeDefined()
  // a fresh line to two roles
  await ui.input({ key: 'composer', text: '@worker-a @worker-b sync up' })
  expect(w.calls.filter(c => c[0] === 'send').at(-1)).toEqual(['send', '--from', 'user', '--', 'worker-a,worker-b', '@worker-a @worker-b sync up'])
  await ui.unmount()
})

test('e reacts with the emoji row: react <id> <emoji> --as user', async ($, on) => {
  mock.clock(on)
  const w = world()
  fakeBus(on, w)
  await $.command.run(OPEN)
  const ui = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'Pane', requestId: 'bus', props: paneProps(56), viewport: { columns: 58, rows: 32 } })
  await ui.key({ key: 'e' })
  expect(await ui.find({ key: 'rx-1' })).toBeDefined()
  await ui.key({ key: '1' })
  expect(w.calls.find(c => c[0] === 'react')).toEqual(['react', 'm3', '✅', '--as', 'user'])
  expect(await ui.find({ key: 'rx-1' })).toBeUndefined()
  await ui.press({ key: 'k-up' })
  await ui.press({ key: 'k-react' })
  await ui.press({ key: 'rx-6' })
  expect(w.calls.filter(c => c[0] === 'react').at(-1)).toEqual(['react', 'm2', '🎉', '--as', 'user'])
  await ui.unmount()
})

test('@ in the composer suggests roles; picking one completes the draft', async ($, on) => {
  mock.clock(on)
  const w = world()
  fakeBus(on, w)
  await $.command.run(OPEN)
  const ui = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'Pane', requestId: 'bus', props: paneProps(56), viewport: { columns: 58, rows: 32 } })
  await ui.input({ key: 'composer', text: 'ping @wo', kind: 'change' })
  expect(await ui.find({ key: 'ac-worker-a' })).toBeDefined()
  expect(await ui.find({ key: 'ac-worker-b' })).toBeDefined()
  expect(await ui.find({ key: 'ac-coordinator' })).toBeUndefined()
  expect(await ui.find({ key: 'ac-user' })).toBeUndefined()
  await ui.press({ key: 'ac-worker-b' })
  expect((await ui.find({ key: 'composer' }))?.text).toBe('ping @worker-b ')
  expect(await ui.find({ key: 'ac-worker-b' })).toBeUndefined()
  await ui.unmount()
})

test('q closes the pane and stops its polling', async ($, on) => {
  const clock = mock.clock(on)
  const w = world()
  fakeBus(on, w)
  await $.command.run(OPEN)
  const ui = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'Pane', requestId: 'bus', props: paneProps(56), viewport: { columns: 58, rows: 32 } })
  await clock.advance(2100)
  const polls = w.calls.filter(c => c[0] === 'snapshot').length
  expect(w.calls.filter(c => c[0] === 'snapshot' && c.includes('--since')).length).toBeGreaterThan(0)
  await ui.key({ key: 'q' })
  expect(w.closed).toEqual(['bus'])
  await clock.advance(6100)
  expect(w.calls.filter(c => c[0] === 'snapshot').length).toBe(polls)
  await ui.unmount()
})

for (const columns of [40, 60]) {
  test(`the pane fits ${columns} columns`, async ($, on) => {
    mock.clock(on)
    const w = world()
    fakeBus(on, w)
    await $.command.run(OPEN)
    const ui = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'Pane', requestId: 'bus', props: paneProps(columns), viewport: { columns: columns + 2, rows: 32 } })
    for (const id of ['m1', 'm2', 'm3']) expect(await ui.find({ key: `msg-${id}` })).toBeDefined()
    expect(await ui.find({ key: 'composer' })).toBeDefined()
    expect(await ui.find({ key: 'k-close' })).toBeDefined()
    await ui.key({ key: 'j' })
    await ui.unmount()
  })
}

test('the band shows unread and @you counts, opens the pane, and hides at zero', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(3, 1)
  fakeBus(on, w)
  await $.session.start({ cwd: '/work/proj', surface: 'terminal', isInteractive: true })
  await clock.advance(15100)
  const band = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'AbovePrompt', props: BAND_PROPS, viewport: { columns: 100, rows: 3 } })
  expect(await band.find({ text: /3 unread/ })).toBeDefined()
  expect(await band.find({ text: /1 @you/ })).toBeDefined()
  expect(await band.find({ key: 'bus-open' })).toBeDefined()
  await band.unmount()
  const desk = await $.ui.mount({ plugin: 'agent-bus', surface: 'desktop', component: 'AbovePrompt', props: BAND_PROPS, viewport: { columns: 100, rows: 3 } })
  expect(await desk.find({ text: /3 unread/ })).toBeDefined()
  await desk.unmount()

  w.snap = snapshot(0, 0)
  await clock.advance(15100)
  const quiet = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'AbovePrompt', props: BAND_PROPS, viewport: { columns: 100, rows: 3 } })
  expect(await quiet.find({ key: 'bus-open' })).toBeUndefined()
  await quiet.unmount()

  w.snap = snapshot(2, 0)
  await clock.advance(15100)
  const again = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'AbovePrompt', props: BAND_PROPS, viewport: { columns: 100, rows: 3 } })
  expect(await again.find({ text: /2 unread/ })).toBeDefined()
  await again.press({ key: 'bus-open' })
  expect(await again.find({ key: 'bus-open' })).toBeUndefined() // the pane is open: the band steps aside
  await again.unmount()
})

test('the band option off never draws the band', { options: { band: 'off' } }, async ($, on) => {
  mock.clock(on)
  const w = world(3, 1)
  fakeBus(on, w)
  await $.session.start({ cwd: '/work/proj', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ plugin: 'agent-bus', surface: 'terminal', component: 'AbovePrompt', props: BAND_PROPS, viewport: { columns: 100, rows: 3 } })
  expect(await band.find({ key: 'bus-open' })).toBeUndefined()
  await band.unmount()
})

test('new @mentions and replies to the user are toasted once', async ($, on) => {
  mock.clock(on)
  const w = world()
  fakeBus(on, w)
  await $.command.run(OPEN) // the first look primes: backlog is not toasted
  expect(w.toasts).toEqual([])
  w.snap = { ...snapshot(2, 2), cursor: 'c-next', messages: [...MSGS, msg('m4', 'worker-b', 'done, see diff', { unread: true, reply_to_me: true, reply_to: 'm1' })] }
  await $.command.run(OPEN)
  expect(w.toasts).toEqual(['↳ worker-b: done, see diff'])
  await $.command.run(OPEN)
  expect(w.toasts.length).toBe(1)
})

test('helpers: ticks, routing, completion and the vim key map', async () => {
  expect(tickOf(MSGS[0] as BusMessage)).toBe('v')
  expect(tickOf(msg('x', 'user', 't', { receipts: [{ to: 'a', r: 'failed', tick: '!', why: 'to' }, { to: 'b', r: 'seen', tick: 'vv', why: 'to' }] }))).toBe('!')
  expect(sendArgs('hello', undefined, ['worker-a'])).toEqual(['send', '--from', 'user', '--', '#all', 'hello'])
  expect(sendArgs('@nobody hi', undefined, ['worker-a'])).toEqual(['send', '--from', 'user', '--', '#all', '@nobody hi'])
  expect(completions('x @', ['user', 'a', 'b'])).toEqual(['a', 'b'])
  expect(completions('mail@host', ['host'])).toEqual([])
  expect(keyAction('d', true, false, false, false).action).toBe('pagedown')
  expect(keyAction('g', false, false, false, false)).toEqual({ g: true })
  expect(keyAction('g', false, false, false, true).action).toBe('top')
  expect(keyAction('3', false, false, true, false).action).toBe('emoji:3')
  expect(keyAction('3', false, false, false, false).action).toBeUndefined()
})
