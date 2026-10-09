import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'
import { etime } from '../hooks/register'

const PS_IDLE = [
  '  101 01:02:03 /usr/sbin/sshd',
  '  202    00:05 node tsserver.js',
  '  303 02:00:00 node_modules/.bin/oxlint --lsp',
  '  404    10:00 bunx vitest --watch',
].join('\n')
const PS_BUSY = [
  PS_IDLE,
  "  4242    03:12 /bin/zsh -c source ~/.snap.sh && eval 'bun run typecheck' < /dev/null && pwd -P",
  '  4250    03:10 node node_modules/.bin/tsc --noEmit -p .',
].join('\n')

function answerPs(on: On, stdout: string) {
  on('process.run', () => ({
    value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
}

test('lets a heavy job run when nothing heavy is running', async ($, on) => {
  answerPs(on, PS_IDLE)
  let ran = false
  on('tool.call', { tool: 'Bash' }, () => {
    ran = true
    return { result: { stdout: 'ok', stderr: '', interrupted: false } }
  })

  await $.tool.call({ tool: 'Bash', command: 'bun run test' })

  expect(ran).toBe(true)
})

test('refuses a heavy job while another is running, naming it', async ($, on) => {
  answerPs(on, PS_BUSY)
  let ran = false
  on('tool.call', { tool: 'Bash' }, () => {
    ran = true
    return { result: { stdout: 'ok', stderr: '', interrupted: false } }
  })

  const answer = await $.tool.call({ tool: 'Bash', command: 'bunx vitest run' })

  expect(ran).toBe(false)
  expect(JSON.stringify(answer)).toContain('pid 4242 (running 03:12): bun run typecheck')
})

test('never checks processes for a light command', async ($, on) => {
  let looked = false
  on('process.run', () => {
    looked = true
    return {
      value: { exitCode: 0, stdout: PS_BUSY, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
    }
  })
  let ran = false
  on('tool.call', { tool: 'Bash' }, () => {
    ran = true
    return { result: { stdout: '', stderr: '', interrupted: false } }
  })

  await $.tool.call({ tool: 'Bash', command: 'git status' })

  expect(looked).toBe(false)
  expect(ran).toBe(true)
})

test('warn mode runs the job anyway', { options: { mode: 'warn' } }, async ($, on) => {
  answerPs(on, PS_BUSY)
  let ran = false
  on('tool.call', { tool: 'Bash' }, () => {
    ran = true
    return { result: { stdout: '', stderr: '', interrupted: false } }
  })

  await $.tool.call({ tool: 'Bash', command: 'bun run typecheck' })

  expect(ran).toBe(true)
})

test('reads the Windows process table when ps is not available', async ($, on) => {
  on('process.run', ($, e) => {
    if (e.argv[0] === 'ps') {
      return { value: { exitCode: 1, stdout: '', stderr: 'ps: unknown option', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    return {
      value: {
        exitCode: 0,
        stdout: '101 3600 C:\\Windows\\explorer.exe\r\n4242 192 node node_modules/.bin/tsc --noEmit -p .\r\n',
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })
  let ran = false
  on('tool.call', { tool: 'Bash' }, () => {
    ran = true
    return { result: { stdout: '', stderr: '', interrupted: false } }
  })

  const answer = await $.tool.call({ tool: 'Bash', command: 'npm run build' })

  expect(ran).toBe(false)
  expect(JSON.stringify(answer)).toContain('pid 4242 (running 03:12)')
})

test('formats elapsed seconds like ps', async () => {
  expect(etime(5)).toBe('00:05')
  expect(etime(3725)).toBe('01:02:05')
  expect(etime(90061)).toBe('1-01:01:01')
})
