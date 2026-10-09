import type { EngineInterface, Register } from 'claude-code'

type Job = { pid: number; elapsed: string; command: string }

const SHOWN_JOBS = 3
const SHOWN_CHARS = 120

/** Claude's Bash tool wraps each command as `zsh -c source … && eval '<command>' …`. */
function unwrap(command: string): string {
  const evaled = /\beval '((?:[^']|'\\'')*)'/.exec(command)

  return (evaled?.[1] ?? command).replaceAll(`'\\''`, `'`)
}

function clip(text: string): string {
  return text.length > SHOWN_CHARS ? `${text.slice(0, SHOWN_CHARS - 1)}…` : text
}

/** Seconds as `ps` prints an elapsed time: `mm:ss`, `hh:mm:ss` or `d-hh:mm:ss`. */
export function etime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const two = (n: number) => String(n).padStart(2, '0')
  const days = Math.floor(s / 86400)
  const hours = Math.floor((s % 86400) / 3600)
  const clock = `${two(Math.floor((s % 3600) / 60))}:${two(s % 60)}`

  if (days > 0) {
    return `${days}-${two(hours)}:${clock}`
  }

  return hours > 0 ? `${two(hours)}:${clock}` : clock
}

/**
 * The machine's processes as `pid etime command` lines: `ps` on macOS and Linux, and
 * Windows' process table through PowerShell where `ps` is missing or is not the BSD/procps one.
 */
async function processList($: EngineInterface): Promise<string | undefined> {
  const ps = await $.process.run(['ps', '-axo', 'pid=,etime=,command=']).catch(() => undefined)

  if (ps !== undefined && ps.exitCode === 0) {
    return ps.stdout
  }

  const script =
    'Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | ForEach-Object { ' +
    "'{0} {1} {2}' -f $_.ProcessId, [int]((Get-Date) - $_.CreationDate).TotalSeconds, $_.CommandLine }"
  const win = await $.process
    .run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], { timeoutMs: 20000 })
    .catch(() => undefined)

  if (win === undefined || win.exitCode !== 0) {
    $.ui.toast(`${$.plugin.name}: cannot list processes on this machine, so heavy jobs are not checked`)

    return undefined
  }

  return win.stdout
    .split(/\r?\n/)
    .map(line => line.replace(/^(\d+) (\d+) /, (_, pid: string, secs: string) => `${pid} ${etime(Number(secs))} `))
    .join('\n')
}

/** Every process on the machine whose command line matches `pattern` and not `ignore`, parents first. */
export async function runningJobs(
  $: EngineInterface,
  pattern: RegExp,
  ignore: RegExp,
): Promise<Job[]> {
  const listing = await processList($)

  if (listing === undefined) {
    return []
  }

  const jobs: Job[] = []

  for (const line of listing.split('\n')) {
    const match = /^\s*(\d+)\s+(\S+)\s+(.*)$/.exec(line)
    const command = match?.[3] ?? ''

    if (match === null || !pattern.test(command) || ignore.test(command)) {
      continue
    }

    jobs.push({ pid: Number(match[1]), elapsed: match[2] ?? '', command: unwrap(command) })
  }

  return jobs.sort((a, b) => a.pid - b.pid)
}

export function describeJobs(jobs: readonly Job[]): string {
  const shown = jobs
    .slice(0, SHOWN_JOBS)
    .map(job => `  pid ${job.pid} (running ${job.elapsed}): ${clip(job.command)}`)
  const more = jobs.length > SHOWN_JOBS ? [`  …and ${jobs.length - SHOWN_JOBS} more`] : []

  return [...shown, ...more].join('\n')
}

export const register: Register = (on, options) => {
  const pattern = new RegExp(String(options.pattern))
  const ignore = new RegExp(String(options.ignore))

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (!pattern.test(e.command)) {
      return next(e)
    }

    if (options.subagents === 'deny' && e.agentId !== undefined) {
      return {
        deny:
          `${$.plugin.name}: subagents don't run heavy jobs here (typecheck, tests, builds). ` +
          'Finish your edits and report back; the main agent runs this one.',
      }
    }

    const jobs = await runningJobs($, pattern, ignore)

    if (jobs.length === 0) {
      return next(e)
    }

    if (options.mode === 'warn') {
      $.ui.toast(`${$.plugin.name}: starting a heavy job while ${jobs.length} already run`)

      return next(e)
    }

    return {
      deny: [
        `${$.plugin.name}: a heavy job is already running on this machine, and two at once ` +
          'compete for its memory and CPU:',
        describeJobs(jobs),
        'Wait for it to finish (watch it with Monitor, or come back to this later), then run ' +
          'the command again. Do not reword the command to get past this check.',
      ].join('\n'),
    }
  }).catch(($, e, next) => next(e))
}
