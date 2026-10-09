import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

type Run = { argv: string[]; env: Record<string, string> }

/** `/index <args>` typed at the prompt, as the engine's call site passes it. */
function run(args: string) {
  return {
    command: 'index',
    args,
    origin: { kind: 'composer' as const },
    presentation: { isFullscreen: false, columns: 120 },
  }
}

function recordRuns(on: On, stdout = '.claude/index/code_index.md: 3 files, 9 entries, 40 lines') {
  const runs: Run[] = []
  on('process.run', ($, e) => {
    runs.push({ argv: [...e.argv], env: { ...(e.init?.env ?? {}) } })
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return runs
}

test('/index rebuilds with the configured python and the options as environment, never as flags', async ($, on) => {
  const runs = recordRuns(on)

  const answer = await $.command.run(run(''))

  expect(runs.length).toBe(1)
  expect(runs[0]?.argv.length).toBe(2)
  expect(runs[0]?.argv[0]).toBe('python')
  expect(runs[0]?.argv[1]).toMatch(/scripts\/build_code_index\.py$/)
  expect(runs[0]?.env.CODE_INDEX_PATH).toBe('.claude/index/code_index.md')
  expect(runs[0]?.env.CODE_INDEX_STATUS_TAG).toBe('STATUS')
  expect(answer.text).toContain('3 files')
})

test(
  '/index carries changed options in the environment, and --full as the only flag',
  { options: { python: 'python3', statusTag: 'TASK', indexPath: 'docs/index.json' } },
  async ($, on) => {
    const runs = recordRuns(on)

    await $.command.run(run('--full'))

    expect(runs[0]?.argv).toEqual(['python3', expect.stringMatching(/build_code_index\.py$/), '--full'])
    expect(runs[0]?.env.CODE_INDEX_PATH).toBe('docs/index.json')
    expect(runs[0]?.env.CODE_INDEX_STATUS_TAG).toBe('TASK')
    expect(runs[0]?.env.CODE_INDEX_PYTHON).toBe('python3')
  },
)

test('/index reports a failed build', async ($, on) => {
  on('process.run', () => ({
    value: { exitCode: 1, stdout: '', stderr: 'boom', isStdoutTruncated: false, isStderrTruncated: false },
  }))

  const answer = await $.command.run(run(''))

  expect(answer.text).toContain('failed (exit 1)')
  expect(answer.text).toContain('boom')
})

test('session start sets the environment and registers /index', async ($, on) => {
  const names: string[] = []
  const env: Record<string, string | undefined> = {}
  on('command.register', ($, e) => {
    names.push(e.name)
    return { value: { command: e.name } }
  })
  on('env.set', ($, e) => {
    env[e.name] = e.value
    return { value: undefined }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })

  expect(names).toEqual(['index'])
  expect(env.CODE_INDEX_PATH).toBe('.claude/index/code_index.md')
  expect(env.CODE_INDEX_CLI).toMatch(/scripts\/build_code_index\.py$/)
  expect(env.CODE_INDEX_TOC).toMatch(/scripts\/md_toc\.py$/)
  expect(env.CODE_INDEX_TAGS).toMatch(/scripts\/status_tags\.py$/)
})

test('the system prompt names the index and the commands', async ($, on) => {
  on('prompt.compose', () => ({ sections: [{ id: 'intro', text: 'hi', scope: 'shared' as const }] }))

  const composed = await $.prompt.compose({
    model: 'm',
    promptModel: 'm',
    surfaces: ['terminal'],
    tools: [],
    outputStyle: null,
    traits: [],
  })
  const added = composed.sections.at(-1)

  expect(composed.sections.length).toBe(2)
  expect(added?.id).toBe('code-index:commands')
  expect(added?.scope).toBe('session')
  expect(added?.text).toContain('.claude/index/code_index.md')
  expect(added?.text).toContain('md_toc.py')
  expect(added?.text).toContain('status_tags.py')
})
