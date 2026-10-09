import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'
import { contextLines, retireReminder, type CoordContext } from '../hooks/register'

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
  pauseAtPercent: 97,
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
