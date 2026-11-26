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

test('a tool server an agent keeps open is not a job', async ($, on) => {
  answerPs(
    on,
    `${PS_IDLE}\n  869 06:00:00 npm exec @playwright/mcp@latest --output-dir x\n  892 06:00:00 node /x/.bin/playwright-mcp --output-dir x`,
  )
  let ran = false
  on('tool.call', { tool: 'Bash' }, () => {
    ran = true
    return { result: { stdout: '', stderr: '', interrupted: false } }
  })

  await $.tool.call({ tool: 'Bash', command: 'bun run test' })

  expect(ran).toBe(true)
})

for (const line of [
  'node /x/node_modules/.bin/vitest run packages/foo-mcp',
  'npx tsc -p packages/mcp',
  'npm test -- my mcp server',
  '/x/foo-mcp/node_modules/.bin/jest',
  'C:\\git tools\\node.exe C:\\x\\jest',
  '/home/a/cat dir/node_modules/.bin/jest',
]) {
  test(`a running job counts: ${line}`, async ($, on) => {
    answerPs(on, `${PS_IDLE}\n  900 01:00 ${line}`)
    let ran = false
    on('tool.call', { tool: 'Bash' }, () => {
      ran = true
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })

    await $.tool.call({ tool: 'Bash', command: 'bun run test' })

    expect(ran).toBe(false)
  })
}

for (const line of [
  'npx -y @playwright/mcp@latest',
  'npx playwright run-mcp-server',
  'grep -rn vitest .',
  `/bin/zsh -c source ~/.snap.sh && eval 'git commit -m "npm test passes"' < /dev/null && pwd -P`,
]) {
  test(`not a running job: ${line}`, async ($, on) => {
    answerPs(on, `${PS_IDLE}\n  900 01:00 ${line}`)
    let ran = false
    on('tool.call', { tool: 'Bash' }, () => {
      ran = true
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })

    await $.tool.call({ tool: 'Bash', command: 'bun run test' })

    expect(ran).toBe(true)
  })
}

const CHILD = '  901 00:59 node /usr/local/bin/npm test'

for (const line of [
  `/bin/zsh -c source ~/.snap.sh && eval 'npm test packages/mcp' < /dev/null && pwd -P`,
  `/bin/zsh -c source ~/.snap.sh && eval 'cat <<E\\012x\\012E\\012npm test' < /dev/null && pwd -P`,
  `/bin/zsh -c source ~/.snap.sh && eval 'echo '"'"'a'"'"'; npm test' < /dev/null && pwd -P`,
  `bash -c npm test; eval 'ls'`,
]) {
  test(`a shell is not a job by itself, only with the program it started: ${line}`, async ($, on) => {
    let ps = `${PS_IDLE}\n  900 01:00 ${line}`
    on('process.run', () => ({
      value: { exitCode: 0, stdout: ps, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
    }))
    let ran = 0
    on('tool.call', { tool: 'Bash' }, () => {
      ran += 1
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })

    await $.tool.call({ tool: 'Bash', command: 'bun run test' })
    expect(ran).toBe(1)

    ps = `${ps}\n${CHILD}`
    const answer = await $.tool.call({ tool: 'Bash', command: 'bun run test' })

    expect(ran).toBe(1)
    expect(JSON.stringify(answer)).toContain('pid 901')
  })
}

for (const line of [
  `/bin/zsh -c source ~/.snap.sh && eval 'while pgrep -f "cargo build" >/dev/null; do sleep 5; done' < /dev/null && pwd -P`,
  `/bin/zsh -c source ~/.snap.sh && eval 'until ! pgrep -f '"'"'cargo (build|test)'"'"'; do sleep 10; done; python3 next.py' < /dev/null && pwd -P`,
  `/bin/zsh -c source ~/.snap.sh && eval 'nicotine resource run heavy -- cargo test' < /dev/null && pwd -P`,
  `/bin/zsh -c source ~/.snap.sh && eval 'piforge -C /w run -- sh -c "cargo test"; python3 x.py' < /dev/null && pwd -P`,
  'bash -c while pgrep -f "cargo build"; do sleep 5; done',
  '/bin/sh -c until ! pgrep -x cargo; do sleep 2; done; echo cargo test done',
  'nicotine resource run heavy -- cargo test',
  'piforge -C /w run -- cargo test',
  'ssh board cargo test',
  'tmux new-session -d cargo build',
  'pgrep -fl cargo build',
]) {
  test(`waiting for a job or handing it elsewhere is not a job: ${line}`, async ($, on) => {
    answerPs(on, `${PS_IDLE}\n  900 01:00 ${line}`)
    let ran = false
    on('tool.call', { tool: 'Bash' }, () => {
      ran = true
      return { result: { stdout: '', stderr: '', interrupted: false } }
    })

    await $.tool.call({ tool: 'Bash', command: 'cargo test' })

    expect(ran).toBe(true)
  })
}

test('generated: shells and waiting loops never count by themselves', async ($, on) => {
  const seed = Date.now() % 100000
  let x = seed || 1
  const next = () => {
    x = (x * 1103515245 + 12345) % 2147483648
    return x / 2147483648
  }
  const pick = <T>(list: readonly T[]): T => list[Math.floor(next() * list.length)] as T
  const shells = ['/bin/sh -c', '/bin/bash -c', '/bin/zsh -c', 'bash -lc', 'dash -c', 'C:\\Windows\\System32\\cmd.exe /c', 'pwsh -Command']
  const words = ['cargo build', 'cargo test', 'npm test', 'tsc', 'jest', 'while', 'pgrep -f', 'sleep 5;', 'done', '"', "'", '*', '日本', '\\012', '$(x)', '|', '&&']
  const lines: string[] = []

  for (let n = 0; n < 200; n += 1) {
    lines.push(`  ${1000 + n} 01:00 ${pick(shells)} ${Array.from({ length: 1 + Math.floor(next() * 8) }, () => pick(words)).join(' ')}`)
  }

  answerPs(on, `${PS_IDLE}\n${lines.join('\n')}`)
  let ran = false
  on('tool.call', { tool: 'Bash' }, () => {
    ran = true
    return { result: { stdout: '', stderr: '', interrupted: false } }
  })

  await $.tool.call({ tool: 'Bash', command: 'cargo test' })

  expect([`seed ${seed}`, ran]).toEqual([`seed ${seed}`, true])
})

test('formats elapsed seconds like ps', async () => {
  expect(etime(5)).toBe('00:05')
  expect(etime(3725)).toBe('01:02:05')
  expect(etime(90061)).toBe('1-01:01:01')
})
