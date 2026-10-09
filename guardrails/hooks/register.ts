import type { EngineInterface, PluginOptions, Register } from 'claude-code'

/** The file-editing tools the gate judges. MultiEdit is kept for hosts that still offer it. */
const EDIT_TOOLS: ReadonlySet<string> = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit'])
const SHOWN_VIOLATIONS = 20

const DEFAULTS: Readonly<Record<string, string>> = {
  baselineFile: '.claude/guardrails.baseline.json',
  paths: '**',
  python: 'python',
}

function option(options: PluginOptions, key: string): string {
  const value = options[key]

  return value === undefined || value === '' ? (DEFAULTS[key] ?? '') : String(value)
}

function cli($: EngineInterface): string {
  return `${$.plugin.root.replaceAll('\\', '/')}/scripts/guard.py`
}

/** The person's options as the runner's environment variables: below a project's own
 * `.claude/claude-mods.json`, above the runner's defaults. An unset rules file stays unset, so the
 * runner looks for .claude/guardrails.toml, then .json. */
export function environment($: EngineInterface, options: PluginOptions): Record<string, string> {
  const rules = option(options, 'rulesFile')

  return {
    ...(rules === '' ? {} : { GUARDRAILS_RULES: rules }),
    GUARDRAILS_BASELINE: option(options, 'baselineFile'),
    GUARDRAILS_PATHS: option(options, 'paths'),
    GUARDRAILS_PYTHON: option(options, 'python'),
    GUARDRAILS_CLI: cli($),
  }
}

export function denial(name: string, stdout: string): string {
  const lines = stdout.split('\n').filter(line => line.trim() !== '' && !line.endsWith('(warn)'))
  const shown = lines.slice(0, SHOWN_VIOLATIONS)
  const more = lines.length > shown.length ? [`  ...and ${lines.length - shown.length} more`] : []

  return [
    `${name}: this edit adds rule violations that are not in the baseline:`,
    ...shown.map(line => `  ${line}`),
    ...more,
    'Fix the code, not the rules: rewrite the change so it no longer violates them. Do not edit the rules ' +
      'file or the baseline to get past this gate; rule changes need the project owner\'s OK.',
  ].join('\n')
}

export const register: Register = (on, options) => {
  const python = option(options, 'python')

  on('session.start', async ($, e, next) => {
    // Bash commands (guard.py check, baseline) inherit these. env.set takes string-literal names.
    const env = environment($, options)
    await $.env.set('GUARDRAILS_RULES', env.GUARDRAILS_RULES)
    await $.env.set('GUARDRAILS_BASELINE', env.GUARDRAILS_BASELINE)
    await $.env.set('GUARDRAILS_PATHS', env.GUARDRAILS_PATHS)
    await $.env.set('GUARDRAILS_PYTHON', env.GUARDRAILS_PYTHON)
    await $.env.set('GUARDRAILS_CLI', env.GUARDRAILS_CLI)

    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    if (!EDIT_TOOLS.has(e.tool)) {
      return next(e)
    }

    // The runner decides scope (paths, the rules' own globs) and judges only what the edit adds.
    const ran = await $.process.run([python, cli($), 'check-edit'], {
      env: environment($, options),
      stdin: JSON.stringify({ tool_name: e.tool, tool_input: e }),
      timeoutMs: 60_000,
    })

    if (ran.exitCode === 0) {
      return next(e)
    }

    if (ran.exitCode !== 1) {
      // Fail open: a broken runner or rules file must not lock every edit. Say so, loudly.
      $.ui.toast(`${$.plugin.name}: the rule runner failed (exit ${ran.exitCode}); edit allowed unchecked`)
      $.ui.log(`${$.plugin.name}: guard.py check-edit exited ${ran.exitCode}: ${(ran.stderr || ran.stdout).trim()}`)

      return next(e)
    }

    if (option(options, 'mode') === 'warn') {
      $.ui.toast(`${$.plugin.name}: this edit adds rule violations (warn mode, edit allowed)`)

      return next(e)
    }

    return { deny: denial($.plugin.name, ran.stdout) }
  }).catch(($, e, next) => {
    if (next.called) {
      return next(e)
    }
    // Fail open when the runner cannot start at all (python missing, timeout).
    $.ui.toast(`${$.plugin.name}: could not run the rule runner; edit allowed unchecked`)

    return next(e)
  })
}
