/**
 * Whether a shell command would really start a heavy job. Text is not a command: a here-document,
 * a commit message or a search pattern that mentions a test runner starts nothing.
 *
 * The text is excused only when every command in the script is one known to leave it as text.
 * As soon as something else is there (a script, a shell, an interpreter, a variable used as a
 * command, syntax this can't follow), the whole text counts, because that something may run it.
 */

/** `redirAt`: where in `text` an unquoted `<` or `>` begins a redirection, or -1. */
type Word = { text: string; quoted: boolean; inner: Script[]; redirAt: number }
/** `pipeIn`/`pipeOut`: read from the command before it, or write into the command after it. */
type Simple = { words: Word[]; heredoc: string; inner: Script[]; pipeIn: boolean; pipeOut: boolean }
/** `tangled`: has a subshell, a function or a process substitution, where text can travel unseen. */
type Script = { commands: Simple[]; sure: boolean; tangled: boolean }

/** Commands that look at, print, move or send what they are given, and run none of it. */
const LIGHT = new Set(
  (
    'echo printf cat tee grep egrep fgrep pgrep pkill ps kill ls cd pwd type which head tail cut uniq wc tr ' +
    'diff stat file du df mkdir touch cp mv rm ln chmod jq curl sleep true false : exit return wait ' +
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
const LIGHT_PROCESS = new Set([...LIGHT, ...Object.keys(CHECKED), 'nicotine', 'piforge', 'less', 'man', 'ag', 'open'])
const SHELLS = new Set('sh bash zsh dash ksh fish eval source .'.split(' '))
/** Seen in the process list these only start other processes, which are listed by themselves. */
const SHELL_PROCESS = new Set('sh bash zsh dash ksh fish tcsh csh cmd powershell pwsh'.split(' '))
/** Commands whose arguments name files they write. */
const WRITERS = new Set('tee cp mv ln install rsync scp'.split(' '))
/** Commands that keep what they are given (a commit, a message) where the same program can print it back. */
const STORES = new Set('git gh nicotine tmux claude codex pbcopy pbpaste xclip xsel wl-copy wl-paste clip'.split(' '))
/** Files people read: nothing runs them unless told to by name. */
const DOC = /\.(md|markdown|txt|text|log|rst|adoc|org|csv|tsv)$/i
/** Files with a note's extension that a build tool reads by itself. */
const TOOL_INPUT = /^(CMakeLists\.txt|(requirements|constraints)[\w.-]*\.txt|[\w.-]*\.in\.txt)$/i
const isNote = (f: string) => DOC.test(f) && !TOOL_INPUT.test(base(f))
const NOT_A_FILE = /^\/dev\/(null|stdout|stderr|tty|fd\/\d+)$/
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
/** Anything in a program's text that could start a process, load other code or write a file another command may run. */
const STARTS_PROCESS =
  /system|subprocess|popen|exec|spawn|child_process|shell|passthru|proc_|pcntl|\bpty\b|import ?\(|__import__|importlib|runpy|ctypes|multiprocessing|require|eval|fork|%x|\bqx\b|Bun|Deno|asyncio|\bos\b|\b(z|ba|da|k|fi|tc|c)?sh\b|open|write|file|copy|move|rename|link|`|\$\(/i
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
  tangled = false
  private pending: { tag: string; tabs: boolean; expands: boolean; into: Simple }[] = []

  constructor(
    private s: string,
    private depth = 0,
  ) {}

  /** Commands up to the end, or up to the `)` that closes a substitution. */
  script(nested = false): Script {
    const commands: Simple[] = []
    const fresh = (pipeIn = false): Simple => ({ words: [], heredoc: '', inner: [], pipeIn, pipeOut: false })
    let command = fresh()
    let word: Word | undefined
    let parens = 0
    let redirect = false

    const open = () => (word ??= { text: '', quoted: false, inner: [], redirAt: -1 })
    const endWord = () => {
      if (word !== undefined) {
        command.words.push(word)
        word = undefined
      }
    }
    const endCommand = (piped = false) => {
      endWord()

      if (command.words.length > 0 || command.inner.length > 0) {
        commands.push(command)
      } else if (piped) {
        // what feeds this pipe is a group: `( … ) |`
        this.tangled = true
      }

      command = fresh(piped)
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
        this.tangled = true
        this.i += 2
        open().inner.push(this.nested())
        continue
      }

      if (this.s.startsWith('<<<', this.i)) {
        endWord()
        open().text += '<<<'
        open().redirAt = 0
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

          return { commands, sure: this.sure, tangled: this.tangled }
        }

        this.sure &&= parens > 0
        parens -= 1
      } else if (c === '(') {
        parens += 1
        this.tangled = true
      }

      if (c === '|') {
        const or = this.s[this.i + 1] === '|'

        command.pipeOut ||= !or
        endCommand(!or)
        this.i += or || this.s[this.i + 1] === '&' ? 2 : 1
        continue
      }

      if (c === ';' || c === '&' || c === '(' || c === ')') {
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

      if (redirect && open().redirAt < 0) {
        open().redirAt = open().text.length
      }

      open().text += c
      this.i += 1
    }

    endCommand()

    // something was left open
    if (nested || parens !== 0 || (this.pending.length > 0 && this.depth === 0)) {
      this.sure = false
    }

    return { commands, sure: this.sure, tangled: this.tangled }
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
      const inner = new Parser((m[1] ?? '').replace(/\\([`\\$])/g, '$1'), this.depth + 1)

      open().inner.push(inner.whole())
      this.tangled ||= inner.tangled
      this.i += m[0].length
    }

    return true
  }

  whole(): Script {
    const { commands } = this.script()

    return { commands, sure: this.sure && this.pending.length === 0, tangled: this.tangled }
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
        const word: Word = { text: '', quoted: true, inner: [], redirAt: -1 }

        text.double(word, '')
        this.sure &&= text.sure && text.pending.length === 0
        this.tangled ||= text.tangled
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
  /** Skips options from `at`; -1 when one of them can run a command here. */
  const options = (at: number): number => {
    while (args[at]?.text.startsWith('-')) {
      const t = args[at]?.text ?? ''
      const takesValue = /^-[46AaCfGgKkMNnqsTtVvXxYy]*[BbcDEeIiJLlmOoPpQRSWw]$/.test(t)
      const value = takesValue ? (args[at + 1]?.text ?? '') : t

      if (t.startsWith('--') || /^-\w*F/.test(t) || (/^-\w*o/.test(t) && /command|include|match/i.test(value))) {
        return -1
      }

      at += takesValue ? 2 : 1
    }

    return at
  }

  const at = options(0)
  const host = args[at]
  const name = (host?.text ?? '').replace(/^ssh:\/\//, '').replace(/^.*@/, '')

  // ssh reads options after the host too, up to the remote command
  return (
    at >= 0 &&
    options(at + 1) >= 0 &&
    host !== undefined &&
    literal(host) &&
    /[a-z.]/i.test(name) &&
    !/^(localhost|127\.|0|\[?::|ip6-)/i.test(name)
  )
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

/**
 * The build board's client sends what it runs to the board, so its commands are text here. Only
 * `prep -- <command>` runs a command on this machine, and a stash's refetch command may.
 */
function safePiforge(words: Word[], whole: string): boolean {
  let at = 1

  while (/^-/.test(words[at]?.text ?? '')) {
    at += words[at]?.text === '-C' ? 2 : 1
  }

  const texts = textsOf(words)
  const end = texts.indexOf('--', at)

  if (texts[at] === 'prep') {
    return end < 0 || isText(words.slice(end + 1), '', whole)
  }

  return !(texts[at] === 'stash' && texts.some(t => t.startsWith('--refetch')))
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
function isText(words: Word[], heredoc: string, whole: string, fed = false): boolean {
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

    const flags = args.slice(0, (wrapped(rest, WRAPPERS[name] ?? { values: '' }) ?? 1) - 1)
    const shell = name === 'sudo' && flags.some(w => /^-\w*[si]/.test(w.text))
    const from = (name === 'env' && args.some(w => /^-\w*S/.test(w.text))) || shell ? undefined : wrapped(rest, WRAPPERS[name] ?? { values: '' })
    // what xargs reads becomes the command of a wrapper with none of its own
    const feeds = fed || name === 'xargs'

    // with no command of their own these print, or (exec) redirect
    return from !== undefined && (from >= rest.length ? name !== 'exec' && !fed : isText(rest.slice(from), '', whole, feeds))
  }

  if (name === 'find') {
    const starts = texts.map((t, k) => (/^-(exec|execdir|ok|okdir)$/.test(t) ? k + 1 : -1)).filter(k => k > 0)

    return starts.every(k => isText(rest.slice(k), '', whole))
  }

  if (name === 'piforge') {
    return safePiforge(rest, whole)
  }

  if (name === 'nicotine') {
    const sub = texts.slice(1, 3).join(' ')

    // it holds a resource while the command after `--` runs here
    if (sub === 'resource run') {
      const end = texts.indexOf('--')

      return end > 3 && end < texts.length - 1 && isText(rest.slice(end + 1), '', whole)
    }

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

/** A command, the command whose word or here-document holds its output, and the one piping into it. */
type Node = { c: Simple; outer: number; prev: number }

/** Every command of a script, with those inside its substitutions. */
function flatten(script: Script, outer = -1, nodes: Node[] = []): { nodes: Node[]; sure: boolean; tangled: boolean } {
  let sure = script.sure
  let tangled = script.tangled
  let prev = -1

  for (const command of script.commands) {
    const at = nodes.length

    nodes.push({ c: command, outer, prev: command.pipeIn ? prev : -1 })
    prev = at

    for (const inner of [...command.inner, ...command.words.flatMap(w => w.inner)]) {
      const all = flatten(inner, at, nodes)

      sure &&= all.sure
      tangled ||= all.tangled
    }
  }

  return { nodes, sure, tangled }
}

/** Words whose own text is never excused: assignments, and the words of a test or a loop. */
function ownText(words: Word[]): string {
  const at = words.findIndex(w => !KEYWORD.test(w.text) && !ASSIGNS.test(w.text))
  const all = at < 0 || /^(for|test|\[|\[\[|export|local|env)$/.test(nameOf(words[at]))

  return textsOf(all ? words : words.slice(0, at)).join(' ')
}

/** The words of the program a command runs, past keywords, assignments and wrappers like sudo. */
function program(words: Word[]): Word[] {
  let rest = words.slice(Math.max(0, words.findIndex(w => !KEYWORD.test(w.text) && !ASSIGNS.test(w.text))))

  for (let hops = 0; hops < 10 && Object.hasOwn(WRAPPERS, nameOf(rest[0])); hops += 1) {
    const from = wrapped(rest, WRAPPERS[nameOf(rest[0])] ?? { values: '' })

    if (from === undefined || from >= rest.length) {
      break
    }

    rest = rest.slice(from)
  }

  return rest
}

/** A file a command names: a redirection's target or an argument, as text. */
const fileOf = (w: Word) => (w.inner.length > 0 ? '?' : w.text)
const base = (path: string) => path.replace(/\/+$/, '').replace(/^.*\//, '')

/** Files a command writes: where its output is redirected, and what a writer like tee or cp is given. */
function writes(words: Word[]): string[] {
  const out: string[] = []

  for (let at = 0; at < words.length; at += 1) {
    const w = words[at] as Word

    if (w.redirAt < 0 || w.text[w.redirAt] !== '>') {
      continue
    }

    const op = /^>[>|&]?/.exec(w.text.slice(w.redirAt))?.[0] ?? '>'
    const rest = w.text.slice(w.redirAt + op.length)
    const target = rest === '' ? words[(at += 1)] : { ...w, text: rest }

    // `>&2` copies a descriptor; `>&file` writes a file
    if (target !== undefined && !(op === '>&' && /^(\d+|-)$/.test(target.text))) {
      out.push(fileOf(target))
    }
  }

  const prog = program(words)
  const name = nameOf(prog[0])
  const args = prog.slice(1).filter(w => w.redirAt < 0 && !w.text.startsWith('-'))

  if (WRITERS.has(name) || (name === 'sed' && prog.some(w => /^(-[^-]*i|--in-place)/.test(w.text)))) {
    out.push(...args.map(fileOf))
  } else if (name === 'sort') {
    prog.forEach((w, k) => {
      const m = /^(?:-\w*o|--output=?)(.*)$/.exec(w.text)

      if (m !== null) {
        out.push(m[1] !== '' ? (m[1] ?? '?') : fileOf(prog[k + 1] ?? w))
      }
    })
  } else if (name === 'uniq') {
    const files = prog.slice(1).filter(w => w.redirAt < 0 && (w.text === '-' || !w.text.startsWith('-')))

    out.push(...files.slice(1, 2).map(fileOf))
  } else if (name === 'dd') {
    out.push(...args.filter(w => w.text.startsWith('of=')).map(w => fileOf(w).slice(3)))
  } else if (/^g?awk$/.test(name)) {
    // awk can print into any file its program names
    out.push('?')
  }

  return out.filter(f => !NOT_A_FILE.test(f))
}

/** Every file a command names, by its last part: arguments and redirections alike. */
function names(words: Word[]): string[] {
  return words.map(w => base(w.redirAt < 0 ? w.text : w.text.slice(w.redirAt).replace(/^[<>]+[|&]?/, ''))).filter(n => n !== '' && n !== '-')
}

/** A file name with `*`, `?`, `[…]` or `{a,b}` as a test for the names it matches; plain names give undefined. */
function glob(name: string): RegExp | undefined {
  if (!/[*?[{]/.test(name)) {
    return undefined
  }

  const body = name.replace(/[.+^$()|\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.').replace(/\{([^{}]*)\}/g, (_, alts: string) => `(${alts.split(',').join('|')})`)

  try {
    return new RegExp(`^${body}$`)
  } catch {
    return /^/
  }
}

/**
 * Whether a command the script can't vouch for could be given text that names a heavy job. Text
 * travels by pipe, by being captured into another command's words, through files the script writes
 * (when a command names the file, or the file is one a tool may read by itself) and through what
 * git, nicotine, the clipboard and the like keep and print back. Text that goes none of those ways (a
 * message, a commit, a job handed to the build board, a note) reaches nothing, whatever else the
 * script runs. Each command is visited once.
 */
function reaches(nodes: Node[], unknown: boolean[], mention: boolean[]): boolean {
  const tainted = nodes.map(() => false)
  const stores = nodes.map(n => STORES.has(nameOf(program(n.c.words)[0])))
  const written = nodes.map(n => writes(n.c.words))
  const next: number[][] = nodes.map(() => [])
  const readers = new Map<string, number[]>()
  const globs: { test: RegExp; at: number }[] = []
  const files = new Set<string>()
  const queue: number[] = []
  let kept = false

  nodes.forEach((n, i) => {
    // a pipe carries text to the next command; a substitution's output into its outer command
    next[n.prev]?.push(i)

    if (n.outer >= 0) {
      next[i]?.push(n.outer)
    }

    for (const f of names(n.c.words)) {
      const test = glob(f)

      if (test === undefined) {
        readers.set(f, [...(readers.get(f) ?? []), i])
      } else {
        globs.push({ test, at: i })
      }
    }
  })

  const taint = (i: number) => {
    if (i >= 0 && tainted[i] !== true) {
      tainted[i] = true
      queue.push(i)
    }
  }

  mention.forEach((m, i) => m && taint(i))

  for (let head = 0; head < queue.length; head += 1) {
    const i = queue[head] as number

    next[i]?.forEach(taint)

    if (unknown[i] === true) {
      continue
    }

    for (const f of written[i] ?? []) {
      const name = base(f)

      if (!files.has(name)) {
        files.add(name)
        readers.get(name)?.forEach(taint)
        globs.filter(g => g.test.test(name)).forEach(g => taint(g.at))
      }
    }

    if (stores[i] === true && !kept) {
      kept = true
      stores.forEach((s, k) => s && taint(k))
    }
  }

  return tainted.some((t, i) => t && unknown[i] === true) || tainted.some((t, i) => t && (written[i] ?? []).some(f => !isNote(f)))
}

/** Shapes where text may travel in ways the analysis above does not follow. */
function tangled(nodes: Node[], unknown: boolean[]): boolean {
  return nodes.some((n, i) => {
    const words = n.c.words
    const first = words[0]?.text ?? ''
    const prog = program(words)[0]

    return (
      // a group or a loop whose output goes somewhere
      words.some(w => !w.quoted && /^[{}]$/.test(w.text)) ||
      first === 'function' ||
      (CLOSER.test(first) && (n.c.pipeOut || words.length > 1)) ||
      // exec sends all later output where it says
      nameOf(words.find(w => !KEYWORD.test(w.text) && !ASSIGNS.test(w.text))) === 'exec' ||
      // a command chosen by a variable
      (unknown[i] === true && prog !== undefined && (prog.inner.length > 0 || /\$/.test(prog.text)))
    )
  })
}

/** Whether a command's words or here-document name a heavy job, also where an option is glued to it (`-O"npm test"`). */
function mentioned(c: Simple, pattern: RegExp): boolean {
  const texts = textsOf(c.words)

  return (
    pattern.test(texts.join(' ')) ||
    pattern.test(c.heredoc) ||
    texts.some((t, at) => {
      const tail = texts.slice(at + 1, at + 20).join(' ')
      const cuts = [t.indexOf('=') + 1, ...(t.startsWith('-') ? [1, 2, 3] : [])].filter(k => k > 0)

      return cuts.some(k => pattern.test(`${t.slice(k)} ${tail}`))
    })
  )
}

/** Whether running `text` would start something `pattern` names. */
export function startsHeavy(text: string, pattern: RegExp): boolean {
  const { nodes, sure, tangled: grouped } = flatten(new Parser(text).whole())
  const commands = nodes.map(n => n.c)
  const mentions =
    pattern.test(text) ||
    pattern.test(text.replace(/[ \t]*\\\r?\n[ \t]*/g, ' ')) ||
    commands.some(c => pattern.test(textsOf(c.words).join(' ')))

  if (!mentions) {
    return false
  }

  // a git hook written here runs when git does
  const hooks = /\.git\b|husky|gitconfig|hooksPath|git-(path|dir|common-dir)/i.test(text) && commands.some(c => textsOf(c.words).some(t => /(^|\/)git$/.test(t)))

  if (!sure || hooks || commands.some(c => pattern.test(ownText(c.words)))) {
    return true
  }

  const unknown = commands.map(c => !isText(c.words, c.heredoc, text))

  if (!unknown.includes(true)) {
    return false
  }

  const mention = commands.map(c => mentioned(c, pattern))

  return grouped || tangled(nodes, unknown) || reaches(nodes, unknown, mention)
}

/** A process's program name, when its command line shows it for certain. */
function programName(command: string): string {
  const quoted = /^"([^"]+)"/.exec(command.trim())
  const program = quoted?.[1] ?? command.trim().split(/\s/)[0] ?? ''
  const cut = Math.max(program.lastIndexOf('/'), program.lastIndexOf('\\')) + 1
  const folder = program.slice(0, cut)
  const name = program.slice(cut).toLowerCase()

  // a path cut at a space names a folder, not a program
  if (cut > 0 && !/[/\\]s?bin[/\\]$/.test(folder) && !name.endsWith('.exe')) {
    return ''
  }

  return name.replace(/\.exe$/, '')
}

/** Whether a process is a program that is never the job itself, by how its command line begins. */
export function isLight(command: string): boolean {
  return LIGHT_PROCESS.has(programName(command))
}

/** Whether a process is a shell: it only starts other processes, which are listed by themselves. */
export function isShell(command: string): boolean {
  return SHELL_PROCESS.has(programName(command))
}

/** Programs that start the command after their options; seen in the process list, what they start is the job. */
const PROCESS_WRAPPERS = new Set([...Object.keys(WRAPPERS), 'ionice', 'chrt', 'taskset', 'doas', 'unbuffer'])
/** Compilers a build runs as processes of their own, whatever the pattern says. */
const COMPILERS = /^(rustc|clang(\+\+)?(-\d+)?|cc1(plus|obj)?|swiftc|swift-frontend|javac|kotlinc)$/
/** Build daemons that stay up between builds: a job only while they work. */
const DAEMON = /\b(GradleDaemon|KotlinCompileDaemon|GradleWorkerMain)\b/
const LAUNCHERS = /^(node|bun|bunx|npx|uvx|deno|python[\d.]*|pipx)$/

/** The command line with its first word removed (a quoted first word counts as one). */
function dropFirst(command: string): string {
  const t = command.trim()
  const m = /^"[^"]*"\s*|^\S+\s*/.exec(t)

  return t.slice(m?.[0].length ?? t.length)
}

/** The command line of the program a wrapper like timeout, env or nice starts. */
function unwrapProcess(command: string): string {
  let rest = command.trim()

  for (let hops = 0; hops < 10 && PROCESS_WRAPPERS.has(programName(rest)); hops += 1) {
    const { values = '', first = 0 } = WRAPPERS[programName(rest)] ?? {}

    rest = dropFirst(rest)

    while (/^(-|[A-Za-z_]\w*=)/.test(rest)) {
      const word = /^\S+/.exec(rest)?.[0] ?? ''

      rest = dropFirst(rest)

      if (word === '--') {
        break
      }

      if (/^-[^-]/.test(word) && values.includes(word[word.length - 1] ?? '')) {
        rest = dropFirst(rest)
      }
    }

    for (let k = 0; k < first; k += 1) {
      rest = dropFirst(rest)
    }
  }

  return rest
}

/** Whether a process is a tool server an agent session keeps open, by its program or the package it runs. */
export function isToolServer(command: string): boolean {
  let rest = command.trim()

  while (LAUNCHERS.test(programName(rest)) || /^(npm exec|pnpm dlx|yarn dlx|bun x)\b/.test(rest)) {
    rest = dropFirst(/^(npm|pnpm|yarn|bun) /.test(rest) ? dropFirst(rest) : rest)

    while (/^(-y|--yes|--|--\S+=\S+)(\s|$)/.test(rest)) {
      rest = dropFirst(rest)
    }
  }

  const first = (/^"([^"]*)"/.exec(rest)?.[1] ?? /^\S*/.exec(rest)?.[0] ?? '').replace(/\\/g, '/')
  const name = base(first).replace(/@[\w.^~-]*$/, '').replace(/\.(c|m)?js$|\.exe$|\.cmd$/i, '')

  return (
    /(^|[-_.])mcp([-_.]|$)/i.test(name) ||
    /node_modules\/(@[^/]+\/)?[^/]*mcp[^/]*\//i.test(first) ||
    /^@[^/]+\/[^/@]*mcp/i.test(first)
  )
}

/**
 * What a running process is: a heavy job, a build daemon (a job only while it works), or neither.
 * Only a program counts. A shell or an interpreter running code given inline only starts other
 * processes, which are listed by themselves; a shell's `-c` script counts when it would run a job.
 */
export function judgeProcess(command: string, pattern: RegExp): 'job' | 'daemon' | 'no' {
  const rest = unwrapProcess(command)
  const name = programName(rest)

  if (SHELL_PROCESS.has(name)) {
    const script = /(^|\s)(-\w*c|\/c|-Command|-EncodedCommand)\s+([^]*)$/i.exec(dropFirst(rest))

    return script !== null && startsHeavy(script[3] ?? '', pattern) ? 'job' : 'no'
  }

  if (INTERPRETERS.test(name) && /^(-[A-Za-z]+\s+)*(-\w*[ce]|--eval|-p|--print)(\s|$)/.test(dropFirst(rest))) {
    return 'no'
  }

  if (isToolServer(rest)) {
    return 'no'
  }

  if (COMPILERS.test(name)) {
    return 'job'
  }

  if (DAEMON.test(rest) && /^java(w)?$/.test(name)) {
    return 'daemon'
  }

  return pattern.test(rest) && !isLight(rest) ? 'job' : 'no'
}
