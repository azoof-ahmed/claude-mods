import type { EngineInterface, PluginOptions, Register } from 'claude-code'

/** The manifest's userConfig defaults, for an option the engine did not fill in. */
const DEFAULTS: Readonly<Record<string, string>> = {
  indexPath: '.claude/index/code_index.md',
  statusTag: 'STATUS',
  python: 'python',
}

function option(options: PluginOptions, key: string): string {
  const value = options[key]

  return value === undefined || value === '' ? (DEFAULTS[key] ?? '') : String(value)
}

function script($: EngineInterface, name: string): string {
  return `${$.plugin.root.replaceAll('\\', '/')}/scripts/${name}`
}

/** The person's options as the scripts' environment variables. They sit below a project's own
 * `.claude/claude-mods.json` and above the scripts' defaults; only a typed flag beats the project file. */
export function environment($: EngineInterface, options: PluginOptions): Record<string, string> {
  return {
    CODE_INDEX_PATH: option(options, 'indexPath'),
    CODE_INDEX_INCLUDE: option(options, 'include'),
    CODE_INDEX_EXCLUDE: option(options, 'exclude'),
    CODE_INDEX_STATUS_TAG: option(options, 'statusTag'),
    CODE_INDEX_PYTHON: option(options, 'python'),
    CODE_INDEX_CLI: script($, 'build_code_index.py'),
    CODE_INDEX_TOC: script($, 'md_toc.py'),
    CODE_INDEX_TAGS: script($, 'status_tags.py'),
  }
}

export const register: Register = (on, options) => {
  const python = option(options, 'python')

  on('session.start', async ($, e, next) => {
    // Bash commands and the scripts they start inherit these. env.set takes string-literal names.
    const env = environment($, options)
    await $.env.set('CODE_INDEX_PATH', env.CODE_INDEX_PATH)
    await $.env.set('CODE_INDEX_INCLUDE', env.CODE_INDEX_INCLUDE)
    await $.env.set('CODE_INDEX_EXCLUDE', env.CODE_INDEX_EXCLUDE)
    await $.env.set('CODE_INDEX_STATUS_TAG', env.CODE_INDEX_STATUS_TAG)
    await $.env.set('CODE_INDEX_PYTHON', env.CODE_INDEX_PYTHON)
    await $.env.set('CODE_INDEX_CLI', env.CODE_INDEX_CLI)
    await $.env.set('CODE_INDEX_TOC', env.CODE_INDEX_TOC)
    await $.env.set('CODE_INDEX_TAGS', env.CODE_INDEX_TAGS)
    await $.command.register({
      name: 'index',
      description: 'Rebuild the code index (symbols and headings with line numbers)',
      argumentHint: '[--full]',
    })

    return next(e)
  })

  on('command.run', { command: 'index' }, async ($, e) => {
    const full = /(^|\s)--full(\s|$)/.test(e.args) ? ['--full'] : []
    const ran = await $.process.run([python, script($, 'build_code_index.py'), ...full], {
      env: environment($, options),
      timeoutMs: 600_000,
    })

    if (ran.exitCode !== 0) {
      return {
        text: `${$.plugin.name}: the index build failed (exit ${ran.exitCode}).\n${(ran.stderr || ran.stdout).trim()}`,
      }
    }

    return {
      text: ran.stdout.trim(),
      context: [`The code index was rebuilt: ${ran.stdout.trim()}`],
    }
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    const text = [
      `Code index (${$.plugin.name}): before reading source, rg the index (${option(options, 'indexPath')} unless ` +
        "the project's .claude/claude-mods.json moves it) for the symbol, then Read only the lines around it; " +
        'whole-file reads only for files under ~300 lines.',
      `Rebuild it with /index or \`${python} "${script($, 'build_code_index.py')}"\`. Markdown sections with line ` +
        `ranges: \`${python} "${script($, 'md_toc.py')}" <file.md>\`. Task tags: ` +
        `\`${python} "${script($, 'status_tags.py')}" list [--task X]\`.`,
    ].join('\n')

    return { sections: [...composed.sections, { id: `${$.plugin.name}:commands`, text, scope: 'session' }] }
  })
}
