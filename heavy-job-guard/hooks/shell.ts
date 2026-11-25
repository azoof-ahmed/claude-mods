/**
 * Whether a shell command would really start a heavy job. Text is not a command: a here-document,
 * a commit message or a search pattern that mentions a test runner starts nothing.
 *
 * The text is excused only when every command in the script is one known to leave it as text.
 * As soon as something else is there (a script, a shell, an interpreter, a variable used as a
 * command, syntax this can't follow), the whole text counts, because that something may run it.
 */

type Word = { text: string; quoted: boolean; inner: Script[] }
type Simple = { words: Word[]; heredoc: string; inner: Script[] }
type Script = { commands: Simple[]; sure: boolean }

/** Commands that look at, print, move or send what they are given, and run none of it. */
const LIGHT = new Set(
  (
    'echo printf cat tee grep egrep fgrep pgrep pkill ps kill ls cd pwd type which head tail cut uniq wc tr ' +
    'diff stat file du df mkdir touch cp mv rm ln chmod jq curl piforge sleep true false : exit return wait ' +
    'break continue date basename dirname realpath readlink seq hostname uname whoami id uptime nl ' +
    'column comm paste shasum pushd popd read unset pbcopy'
  ).split(' '),
)
/** Commands that run none of their text as long as their arguments pass a check of their own. */
const CHECKED: Record<string, (words: Word[]) => boolean> = {
  git: safeGit,
  gh: safeGh,
  tmux: safeTmux,
  ssh: safeSsh,
  scp: args => !args.some(w => /^-[A-Za-z0-9]*[oFSD]/.test(w.text)),
  sed: safeSed,
  awk: args => !args.some(w => /system|getline|\||@|^-\w*[fiel]|^--/.test(w.text)),
  rg: args => !args.some(w => /^--(pre|hostname)/.test(w.text)),
  sort: args => !args.some(w => /^--c/.test(w.text)),
  claude: args => !args.some(w => /^(--(settings|mcp-config|plugin-dir)|mcp$)/.test(w.text)),
  codex: args => !args.some(w => /^(sandbox|debug|mcp|-c|--config)/.test(w.text)),
}
/** Seen in the process list these are never the job: what they start is listed by itself. */
const LIGHT_PROCESS = new Set([...LIGHT, ...Object.keys(CHECKED), 'nicotine', 'less', 'man', 'ag', 'open'])
const SHELLS = new Set('sh bash zsh dash ksh fish eval source .'.split(' '))
/** Programs that run the command after their own options: which options take a value, and how many words come first. */
const WRAPPERS: Record<string, { values: string; first?: number }> = {
  sudo: { values: 'ugphCDRTUrt' },
  time: { values: 'fo' },
  nice: { values: 'n' },
  nohup: { values: '' },
  env: { values: 'uCP' },
  command: { values: '' },
  builtin: { values: '' },
  exec: { values: 'a' },
  timeout: { values: 'ks', first: 1 },
  caffeinate: { values: 'tw' },
  stdbuf: { values: 'ioe' },
  xargs: { values: 'InPLEJRSsda' },
}
const INTERPRETERS = /^(python[\d.]*|node|ruby|perl|php|deno)$/
/** Anything in a program's text that could start a process or load other code. */
const STARTS_PROCESS =
  /system|subprocess|popen|exec|spawn|child_process|shell|\bpty\b|import ?\(|__import__|importlib|runpy|ctypes|multiprocessing|require|eval|fork|%x|\bqx\b|Bun|Deno|asyncio|\bos\b|\bsh\b|bash|`|\$\(/i
/** Variables that decide which program runs, or name one to run. */
const STEERS = /^(PATH|FPATH|HOME|ENV|BASH_ENV|ZDOTDIR|SHELL|XDG_\w+|GIT_\w+|LESS\w*|RIPGREP_\w+|\w*PAGER|\w*EDITOR|\w*BROWSER|IFS)\+?=/
const ASSIGNS = /^[A-Za-z_]\w*(\[[^\]]*\])?\+?=/
const KEYWORD = /^(if|then|elif|else|while|until|do|!|\{|time)$/
const CLOSER = /^(\}|done|fi)$/
const BIN = /^(\/usr)?(\/local)?\/s?bin\/$|^\/opt\/homebrew\/bin\/$/
/** `${…}` with nothing in it that could be read another way. */
const PARAMETER = /^\$\{[^}"'`\\(\s]*\}/
const ARITHMETIC = /^\$\(\([^()`]*\)\)/

/** Reads a script into commands. Anything it can't follow for certain clears `sure`. */
class Parser {
  i = 0
  sure = true
  private pending: { tag: string; tabs: boolean; expands: boolean; into: Simple }[] = []

  constructor(
    private s: string,
    private depth = 0,
  ) {}

  /** Commands up to the end, or up to the `)` that closes a substitution. */
  script(nested = false): Script {
    const commands: Simple[] = []
    let command: Simple = { words: [], heredoc: '', inner: [] }
    let word: Word | undefined
    let parens = 0
    let redirect = false

    const open = () => (word ??= { text: '', quoted: false, inner: [] })
    const endWord = () => {
      if (word !== undefined) {
        command.words.push(word)
        word = undefined
      }
    }
    const endCommand = () => {
      endWord()

      if (command.words.length > 0 || command.inner.length > 0) {
        commands.push(command)
      }

      command = { words: [], heredoc: '', inner: [] }
    }

    if (this.depth > 20) {
      this.sure = false
      this.i = this.s.length
    }

    while (this.i < this.s.length) {
      const c = this.s[this.i] ?? ''
      const wasRedirect = redirect

      redirect = false

      if (c === '\n') {
        endCommand()
        this.i += 1
        this.heredocs()
        continue
      }

      if (c === '\\') {
        if (this.s[this.i + 1] !== '\n') {
          open().text += this.s[this.i + 1] ?? ''
          open().quoted = true
        }

        this.i += 2
        continue
      }

      if (c === "'" || this.s.startsWith("$'", this.i)) {
        const w = open()

        w.quoted = true
        w.text += c === "'" ? this.single() : this.ansi()
        continue
      }

      if (c === '"' || this.s.startsWith('$"', this.i)) {
        const w = open()

        w.quoted = true
        this.i += c === '"' ? 1 : 2
        this.double(w, '"')
        continue
      }

      if (this.expansion(open)) {
        continue
      }

      if (this.s.startsWith('<(', this.i) || this.s.startsWith('>(', this.i) || this.s.startsWith('=(', this.i)) {
        this.i += 2
        open().inner.push(this.nested())
        continue
      }

      if (this.s.startsWith('<<<', this.i)) {
        endWord()
        open().text += '<<<'
        endWord()
        this.i += 3
        continue
      }

      if (this.s.startsWith('<<', this.i)) {
        const m = /^<<(-?)[ \t]*(?:'([^'\n]+)'|"([^"\n$`\\]+)"|([\w.-]+))(?=[\s;|&<>)]|$)/.exec(this.s.slice(this.i, this.i + 300))

        if (m === null) {
          this.sure = false
          this.i += 2
          continue
        }

        endWord()
        this.pending.push({ tag: m[2] ?? m[3] ?? m[4] ?? '', tabs: m[1] === '-', expands: m[4] !== undefined, into: command })
        this.i += m[0].length
        continue
      }

      if (c === '#' && word === undefined) {
        const nl = this.s.indexOf('\n', this.i)

        this.i = nl < 0 ? this.s.length : nl
        continue
      }

      // a redirection's & is not the end of a command
      if (c === '&' && (wasRedirect || this.s[this.i + 1] === '>')) {
        open().text += c
        this.i += 1
        continue
      }

      if (c === ')') {
        if (parens === 0 && nested) {
          endCommand()
          this.i += 1

          return { commands, sure: this.sure }
        }

        this.sure &&= parens > 0
        parens -= 1
      } else if (c === '(') {
        parens += 1
      }

      if (c === ';' || c === '|' || c === '&' || c === '(' || c === ')') {
        endCommand()
        this.i += 1
        continue
      }

      if (c === ' ' || c === '\t' || c === '\r') {
        endWord()
        this.i += 1
        continue
      }

      redirect = c === '>' || c === '<'
      open().text += c
      this.i += 1
    }

    endCommand()

    // something was left open
    if (nested || parens !== 0 || (this.pending.length > 0 && this.depth === 0)) {
      this.sure = false
    }

    return { commands, sure: this.sure }
  }

  private nested(): Script {
    this.depth += 1

    const script = this.script(true)

    this.depth -= 1

    return script
  }

  /** `$(…)`, `$((…))` and backquotes, in or out of double quotes. */
  private expansion(open: () => Word): boolean {
    const sum = ARITHMETIC.exec(this.s.slice(this.i, this.i + 300))

    if (sum !== null) {
      open().text += sum[0]
      this.i += sum[0].length

      return true
    }

    if (this.s.startsWith('${', this.i) || this.s.startsWith('$[', this.i)) {
      const m = PARAMETER.exec(this.s.slice(this.i, this.i + 300))

      this.sure &&= m !== null
      open().text += m?.[0] ?? '${'
      this.i += m?.[0].length ?? 2

      return true
    }

    if (this.s.startsWith('$(', this.i)) {
      this.i += 2
      open().inner.push(this.nested())

      return true
    }

    if (this.s[this.i] !== '`') {
      return false
    }

    const m = /^`((?:\\[^]|[^`\\])*)`/.exec(this.s.slice(this.i))

    if (m === null) {
      this.sure = false
      this.i = this.s.length
    } else {
      open().inner.push(new Parser((m[1] ?? '').replace(/\\([`\\$])/g, '$1'), this.depth + 1).whole())
      this.i += m[0].length
    }

    return true
  }

  whole(): Script {
    const { commands } = this.script()

    return { commands, sure: this.sure && this.pending.length === 0 }
  }

  private single(): string {
    const close = this.s.indexOf("'", this.i + 1)

    this.sure &&= close >= 0

    const end = close < 0 ? this.s.length : close
    const text = this.s.slice(this.i + 1, end)

    this.i = end + 1

    return text
  }

  private ansi(): string {
    const m = /^\$'((?:\\[^]|[^'\\])*)'/.exec(this.s.slice(this.i))

    // an escape can spell any letter
    this.sure &&= m !== null && !/\\[^nrt'"\\]/.test(m[1] ?? '')
    this.i = m === null ? this.s.length : this.i + m[0].length

    return (m?.[1] ?? '').replace(/\\([nrt])/g, ' ').replace(/\\(.)/g, '$1')
  }

  /** Text in which only substitutions are live, up to `close` (or the end, when empty). */
  private double(word: Word, close: string) {
    while (this.i < this.s.length) {
      const c = this.s[this.i] ?? ''

      if (c === close) {
        this.i += 1

        return
      }

      if (c === '\\') {
        if (this.s[this.i + 1] !== '\n') {
          word.text += this.s[this.i + 1] ?? ''
        }

        this.i += 2
      } else if (!this.expansion(() => word)) {
        word.text += c
        this.i += 1
      }
    }

    this.sure &&= close === ''
  }

  private heredocs() {
    const due = this.pending

    this.pending = []

    for (const { tag, tabs, expands, into } of due) {
      let body = ''
      let closed = false

      while (this.i < this.s.length && !closed) {
        const nl = this.s.indexOf('\n', this.i)
        const end = nl < 0 ? this.s.length : nl
        const line = this.s.slice(this.i, end)

        this.i = end + 1
        closed = (tabs ? line.replace(/^\t+/, '') : line) === tag

        if (!closed) {
          body += `${line}\n`
        }
      }

      // a line joined to the next can hide where the text ends
      this.sure &&= closed && !(expands && /\\\n/.test(body))
      into.heredoc += body

      if (expands) {
        const text = new Parser(body, this.depth + 1)
        const word: Word = { text: '', quoted: true, inner: [] }

        text.double(word, '')
        this.sure &&= text.sure && text.pending.length === 0
        into.inner.push(...word.inner)
      }
    }
  }
}

const textsOf = (words: readonly Word[]) => words.map(w => w.text)
/** Text that no expansion can change. */
const literal = (w: Word) => w.inner.length === 0 && !/[$*?[\]{}~]/.test(w.text)

function safeGit(args: Word[]): boolean {
  const texts = textsOf(args)
  let at = 0

  while (texts[at]?.startsWith('-')) {
    if (texts[at] === '-C') {
      at += 1
    } else if (texts[at] !== '--no-pager') {
      return false
    }

    at += 1
  }

  const sub =
    /^(status|log|show|diff|add|commit|push|pull|fetch|branch|checkout|switch|restore|stash|tag|merge|reset|rm|mv|grep|blame|rev-parse|rev-list|worktree|remote|cherry-pick|ls-files|notes|describe|reflog|show-ref|cat-file)$/

  return (
    args[at] !== undefined &&
    literal(args[at] as Word) &&
    sub.test(texts[at] ?? '') &&
    !texts.slice(at + 1).some(t => /^(--(exe|up|rece|open|ext)|-O|ext::)/.test(t))
  )
}

function safeGh(args: Word[]): boolean {
  return /^(pr|issue|api|run|release|search|label|workflow)$/.test(args[0]?.text ?? '')
}

function safeTmux(args: Word[]): boolean {
  return (
    /^(display(-message)?|capture-pane|capturep|ls|list-\w+|has(-session)?|kill-\w+)$/.test(args[0]?.text ?? '') &&
    !args.some(w => w.text.includes('#(') || w.text.endsWith(';') || w.text === '-I')
  )
}

function safeSsh(args: Word[]): boolean {
  let at = 0

  while (args[at]?.text.startsWith('-')) {
    const t = args[at]?.text ?? ''
    const takesValue = /^-[46AaCfGgKkMNnqsTtVvXxYy]*[BbcDEeIiJLlmOoPpQRSWw]$/.test(t)
    const value = takesValue ? (args[at + 1]?.text ?? '') : t

    if (t.startsWith('--') || /^-\w*F/.test(t) || /^-\w*o/.test(t) && /command|include|match/i.test(value)) {
      return false
    }

    at += takesValue ? 2 : 1
  }

  const host = args[at]
  const name = (host?.text ?? '').replace(/^ssh:\/\//, '').replace(/^.*@/, '')

  return host !== undefined && literal(host) && /[a-z.]/i.test(name) && !/^(localhost|127\.|0|\[?::|ip6-)/i.test(name)
}

/** sed can run commands, so only printing, deleting and plain substitutions pass. */
function safeSed(args: Word[]): boolean {
  let scripts = 0

  for (let at = 0; at < args.length; at += 1) {
    const t = args[at]?.text ?? ''
    let script: string | undefined

    if (t === '-e') {
      at += 1
      script = args[at]?.text ?? ''
    } else if (t.startsWith('-')) {
      if (!/^-([nErsuz]+|i\S*)$/.test(t)) {
        return false
      }

      continue
    } else if (t === '' || scripts > 0) {
      continue
    } else {
      script = t
    }

    scripts += 1

    if (!/^[\d,$!]*[pdq=]$|^[\d,$]*s([^\w\s\\])(?:\\[^]|(?!\1)[^\\\n])*\1(?:\\[^]|(?!\1)[^\\\n])*\1[gpiI\d]*$/.test(script)) {
      return false
    }
  }

  return true
}

/** The program's name, when it is one found by name or in a system folder. */
function nameOf(word: Word | undefined): string {
  if (word === undefined || word.inner.length > 0) {
    return ''
  }

  const slash = word.text.lastIndexOf('/') + 1

  return slash === 0 || BIN.test(word.text.slice(0, slash)) ? word.text.slice(slash) : ''
}

/** Where the command a wrapper runs begins; undefined when its options can't be followed. */
function wrapped(words: Word[], { values, first = 0 }: { values: string; first?: number }): number | undefined {
  let at = 1

  while (at < words.length) {
    const t = words[at]?.text ?? ''

    if (t === '--') {
      at += 1
      break
    }

    if (!t.startsWith('-') || t === '-') {
      break
    }

    if (t.startsWith('--') || (words[at]?.inner.length ?? 0) > 0) {
      return undefined
    }

    const flags = t.slice(1)
    const value = [...flags].findIndex(f => values.includes(f))

    // the value is either the rest of this word or the next word
    at += value >= 0 && value === flags.length - 1 ? 2 : 1
  }

  return at + first
}

/** Whether a command is one that runs none of the text in the script. */
function isText(words: Word[], heredoc: string, whole: string): boolean {
  let at = 0

  while (at < words.length) {
    const w = words[at] as Word

    if (STEERS.test(w.text)) {
      return false
    }

    if (!KEYWORD.test(w.text) && !ASSIGNS.test(w.text)) {
      break
    }

    at += 1
  }

  if (at === words.length) {
    return true
  }

  const rest = words.slice(at)
  const texts = textsOf(rest)
  const args = rest.slice(1)
  const name = nameOf(rest[0])
  const last = texts[texts.length - 1]

  // zsh lets a body follow without a separator
  if (args.some(w => !w.quoted && /^[{}]$/.test(w.text))) {
    return false
  }

  if (name === '[' || name === '[[') {
    return last === (name === '[' ? ']' : ']]')
  }

  if (CLOSER.test(name)) {
    return args.length === 0 || /^\d?[<>]/.test(texts[1] ?? '')
  }

  if (name === 'for') {
    return texts[2] === undefined || texts[2] === 'in'
  }

  if (name === 'test') {
    return true
  }

  if (name === 'export' || name === 'local') {
    return !args.some(w => STEERS.test(w.text) || w.text.startsWith('-'))
  }

  if (name === 'set') {
    return args.every(w => /^[-+]\w+$|^pipefail$/.test(w.text))
  }

  if (Object.hasOwn(WRAPPERS, name)) {
    if (name === 'command' && /^-[vV]$/.test(texts[1] ?? '')) {
      return true
    }

    const from = name === 'env' && args.some(w => /^-\w*S/.test(w.text)) ? undefined : wrapped(rest, WRAPPERS[name] ?? { values: '' })

    // with no command of their own these print, or (exec) redirect
    return from !== undefined && (from >= rest.length ? name !== 'exec' : isText(rest.slice(from), '', whole))
  }

  if (name === 'find') {
    const starts = texts.map((t, k) => (/^-(exec|execdir|ok|okdir)$/.test(t) ? k + 1 : -1)).filter(k => k > 0)

    return starts.every(k => isText(rest.slice(k), '', whole))
  }

  if (name === 'nicotine') {
    const sub = texts.slice(1, 3).join(' ')

    // what another agent is asked to do is that agent's to run
    return /^agent (send|report|ls|read|wait|show|set-status)$/.test(sub) || (sub === 'agent new' && /^(claude|codex|gemini|opencode)$/.test(texts[3] ?? '') && !texts.includes('--')) || (texts[1] === 'browser' && texts[2] !== 'eval')
  }

  if (INTERPRETERS.test(name)) {
    // a program given as text that has nothing to start a process with
    return heredoc !== '' && args.every(w => w.text === '-') && !STARTS_PROCESS.test(whole)
  }

  if (SHELLS.has(name)) {
    return false
  }

  return LIGHT.has(name) || (Object.hasOwn(CHECKED, name) && (CHECKED[name]?.(args) ?? false))
}

/** Every command of a script, with those inside its substitutions. */
function flatten(script: Script): { commands: Simple[]; sure: boolean } {
  const commands: Simple[] = []
  let sure = script.sure

  for (const command of script.commands) {
    commands.push(command)

    for (const inner of [...command.inner, ...command.words.flatMap(w => w.inner)]) {
      const all = flatten(inner)

      commands.push(...all.commands)
      sure &&= all.sure
    }
  }

  return { commands, sure }
}

/** Words whose own text is never excused: assignments, and the words of a test or a loop. */
function ownText(words: Word[]): string {
  const at = words.findIndex(w => !KEYWORD.test(w.text) && !ASSIGNS.test(w.text))
  const all = at < 0 || /^(for|test|\[|\[\[|export|local|env)$/.test(nameOf(words[at]))

  return textsOf(all ? words : words.slice(0, at)).join(' ')
}

/** Whether running `text` would start something `pattern` names. */
export function startsHeavy(text: string, pattern: RegExp): boolean {
  const { commands, sure } = flatten(new Parser(text).whole())
  const mentions =
    pattern.test(text) ||
    pattern.test(text.replace(/[ \t]*\\\r?\n[ \t]*/g, ' ')) ||
    commands.some(c => pattern.test(textsOf(c.words).join(' ')))

  if (!mentions) {
    return false
  }

  // a git hook written here runs when git does
  const hooks = /\.git\b|husky|gitconfig|hooksPath|git-(path|dir|common-dir)/i.test(text) && commands.some(c => textsOf(c.words).some(t => /(^|\/)git$/.test(t)))

  return (
    !sure ||
    hooks ||
    commands.some(c => !isText(c.words, c.heredoc, text) || pattern.test(ownText(c.words)))
  )
}

/** Whether a process is a program that is never the job itself, by how its command line begins. */
export function isLight(command: string): boolean {
  const quoted = /^"([^"]+)"/.exec(command.trim())
  const program = quoted?.[1] ?? command.trim().split(/\s/)[0] ?? ''
  const cut = Math.max(program.lastIndexOf('/'), program.lastIndexOf('\\')) + 1
  const folder = program.slice(0, cut)
  const name = program.slice(cut).toLowerCase()

  // a path cut at a space names a folder, not a program
  if (cut > 0 && !/[/\\]s?bin[/\\]$/.test(folder) && !name.endsWith('.exe')) {
    return false
  }

  return LIGHT_PROCESS.has(name.replace(/\.exe$/, ''))
}
