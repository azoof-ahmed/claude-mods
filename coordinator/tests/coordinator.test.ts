import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'
import { contextLines, retireReminder, usageNotes, type CoordContext } from '../hooks/register'

const BASE: CoordContext = {
  configured: true,
  role: '',
  isCoordinator: true,
  rules: '.claude/coord/RULES_CORE.md',
  card: null,
  cardExists: false,
  live: ['worker-a', 'worker-b'],
  questions: 0,
  retireTokens: 250000,
  pauseSessionPercent: 97,
  pauseWeeklyPercent: 80,
  pause: null,
  maxLanes: 6,
}

function answerContext(on: On, c: CoordContext) {
  on('classic.SessionStart', () => ({}))
  on('process.run', () => ({
    value: { exitCode: 0, stdout: JSON.stringify(c), stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
}

test('an uncoordinated project gets no context', async ($, on) => {
  answerContext(on, { ...BASE, configured: false })
  const r = await $.classic.SessionStart({ source: 'startup' } as never)
  expect(r.additionalContext ?? []).toEqual([])
})

test('the coordinator session is told the lanes and the open questions', async ($, on) => {
  answerContext(on, { ...BASE, questions: 2 })
  const r = await $.classic.SessionStart({ source: 'startup' } as never)
  const text = (r.additionalContext ?? []).join('\n')
  expect(text).toContain('2 live of max 6')
  expect(text).toContain('2 question(s)')
  expect(text).toContain('scripts/coord.py')
})

test('a worker is pointed at the rules and its card', async () => {
  const lines = contextLines(
    { ...BASE, role: 'worker-a', isCoordinator: false, card: '.claude/coord/lanes/worker-a.md' },
    'python "/p/scripts/coord.py"',
  )
  expect(lines[0]).toContain('you are the worker `worker-a`')
  expect(lines[0]).toContain('not written yet')
  expect(lines[0]).toContain('250k')
})

test('retirement reminders start at the threshold and repeat every further 10%', async () => {
  expect(retireReminder(200000, 250000, 0)).toBeUndefined()
  const first = retireReminder(250000, 250000, 0)
  expect(first?.level).toBe(1)
  expect(retireReminder(260000, 250000, 1)).toBeUndefined()
  expect(retireReminder(276000, 250000, 1)?.level).toBe(2)
})

test('weekly usage past the threshold tells the coordinator to pause, once per crossing', async () => {
  const week = (p: number) => [{ kind: 'seven_day', percentUsed: p }]
  let r = usageNotes(week(79), BASE, new Set())
  expect(r.notes).toEqual([])
  r = usageNotes(week(81), BASE, r.noted)
  expect(r.notes[0]).toContain('/coordinator pause')
  expect(usageNotes(week(83), BASE, r.noted).notes).toEqual([])
  const reset = usageNotes(week(10), BASE, r.noted)
  expect(usageNotes(week(85), BASE, reset.noted).notes.length).toBe(1)
})

test('workers are not told about the weekly threshold; the coordinator pauses them', async () => {
  const r = usageNotes([{ kind: 'seven_day', percentUsed: 90 }], { ...BASE, isCoordinator: false }, new Set())
  expect(r.notes).toEqual([])
})

test('no weekly note while a pause is already running', async () => {
  const r = usageNotes([{ kind: 'seven_day', percentUsed: 90 }], { ...BASE, pause: 'pausing' }, new Set())
  expect(r.notes).toEqual([])
})

test('everyone checkpoints at the session threshold', async () => {
  const r = usageNotes([{ kind: 'five_hour', percentUsed: 98 }], { ...BASE, isCoordinator: false }, new Set())
  expect(r.notes[0]).toContain('Checkpoint')
})
