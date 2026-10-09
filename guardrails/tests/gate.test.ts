import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

type Run = { argv: string[]; stdin: string; env: Record<string, string> }

const VIOLATION = 'src/app.ts:2: no-console-log: Use the logger.'

/** Stands in for guard.py check-edit: exit 0 = nothing new, 1 = new violations on stdout, 2 = broken. */
function runner(on: On, exitCode: number, stdout = '') {
  const runs: Run[] = []
  on('process.run', ($, e) => {
    runs.push({ argv: [...e.argv], stdin: e.init?.stdin ?? '', env: { ...(e.init?.env ?? {}) } })
    return { value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return runs
}

function editTool(on: On) {
  const state = { ran: false }
  on('tool.call', { tool: 'Edit' }, () => {
    state.ran = true
    return { result: { filePath: '/repo/src/app.ts', oldString: 'a', newString: 'b' } }
  })
  return state
}

const EDIT = {
  tool: 'Edit',
  file_path: '/repo/src/app.ts',
  old_string: 'const a = 1',
  new_string: 'const a = 1\nconsole.log(a)',
} as const

test('denies an edit that adds a violation not in the baseline', async ($, on) => {
  const runs = runner(on, 1, `${VIOLATION}\n`)
  const tool = editTool(on)

  const answer = await $.tool.call(EDIT)

  expect(tool.ran).toBe(false)
  expect(JSON.stringify(answer)).toContain(VIOLATION)
  expect(JSON.stringify(answer)).toContain('Fix the code, not the rules')
  expect(runs[0]?.argv.slice(-1)).toEqual(['check-edit'])
  expect(runs[0]?.argv[1]).toMatch(/scripts\/guard\.py$/)
  const sent = JSON.parse(runs[0]?.stdin ?? '{}')
  expect(sent.tool_name).toBe('Edit')
  expect(sent.tool_input.file_path).toBe('/repo/src/app.ts')
  expect(sent.tool_input.new_string).toContain('console.log')
})

test('allows an edit whose violations are all baselined', async ($, on) => {
  runner(on, 0)
  const tool = editTool(on)

  await $.tool.call(EDIT)

  expect(tool.ran).toBe(true)
})

test('allows an edit the runner finds out of scope, and never runs it for other tools', async ($, on) => {
  const runs = runner(on, 0)
  let read = false
  on('tool.call', { tool: 'Read' }, () => {
    read = true
    return { result: { type: 'text', file: { filePath: '/repo/x', content: '', numLines: 0, startLine: 1, totalLines: 0 } } }
  })
  const tool = editTool(on)

  await $.tool.call({ tool: 'Read', file_path: '/repo/x' })
  expect(read).toBe(true)
  expect(runs.length).toBe(0)

  await $.tool.call({ ...EDIT, file_path: '/repo/docs/notes.md' })
  expect(tool.ran).toBe(true)
  expect(runs.length).toBe(1)
})

test('passes the options to the runner as environment', { options: { paths: 'src/**', python: 'python3' } }, async ($, on) => {
  const runs = runner(on, 0)
  editTool(on)

  await $.tool.call(EDIT)

  expect(runs[0]?.argv[0]).toBe('python3')
  expect(runs[0]?.env.GUARDRAILS_PATHS).toBe('src/**')
  expect(runs[0]?.env.GUARDRAILS_BASELINE).toBe('.claude/guardrails.baseline.json')
  expect(runs[0]?.env.GUARDRAILS_RULES).toBe(undefined)
})

test('warn mode lets a violating edit through', { options: { mode: 'warn' } }, async ($, on) => {
  runner(on, 1, `${VIOLATION}\n`)
  const tool = editTool(on)

  await $.tool.call(EDIT)

  expect(tool.ran).toBe(true)
})

test('fails open when the runner itself is broken', async ($, on) => {
  runner(on, 2)
  const tool = editTool(on)

  await $.tool.call(EDIT)

  expect(tool.ran).toBe(true)
})

test('fails open when the runner cannot start', async ($, on) => {
  on('process.run', () => {
    throw new Error('python: not found')
  })
  const tool = editTool(on)

  await $.tool.call(EDIT)

  expect(tool.ran).toBe(true)
})
