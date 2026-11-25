/**
 * Which part of a shell command would really start a heavy job. Text is not a command: a
 * here-document, a commit message or a search pattern that mentions a test runner starts
 * nothing. Anything this can't make sense of counts as it did before: by its text.
 */

type Word = { text: string; inner: string[] }

/** Programs that only look at, print or send their arguments, or run them on another machine. */
const LIGHT = new Set(
  (
    'echo printf cat tee grep egrep fgrep rg ag pgrep pkill ps kill ls cd pwd which type man less more head tail ' +
    'sed awk cut sort uniq wc tr diff stat file du df mkdir touch cp mv rm ln chmod git gh jq open ' +
    'claude codex tmux piforge ssh scp'
  ).split(' '),
)
const SHELLS = new Set(['sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'eval'])
/** Programs that run the command given after their own options. */
const WRAPPERS = new Set('sudo time nice nohup env command exec timeout caffeinate stdbuf xargs builtin'.split(' '))

/** Whether a process is one of those programs, by the first word of its command line. */
export function isLight(command: string): boolean {
  return LIGHT.has((command.trim().split(/\s/)[0] ?? '').split(/[/\\]/).pop() ?? '')
}

type Simple = { words: Word[]; raw: string; heredoc: string }

/** Reads `(`…`)` from just after `$(`; returns the text inside and where it ends. */
function substitution(s: string, from: number): [string, number] {
  let depth = 1
  let i = from

  while (i < s.length) {
    const c = s[i]

    if (c === '\\') {
      i += 1
    } else if (c === "'") {
      const end = s.indexOf("'", i + 1)

      i = end < 0 ? s.length : end
    } else if (c === '(') {
      depth += 1
    } else if (c === ')') {
      depth -= 1

      if (depth === 0) {
        return [s.slice(from, i), i]
      }
    }

    i += 1
  }

  return [s.slice(from), s.length]
}

/** The simple commands of a script, with their words unquoted and here-documents set apart. */
export function parse(script: string): Simple[] {
  const out: Simple[] = []
  let words: Word[] = []
  let word: Word | undefined
  let raw = ''
  let current: Simple | undefined
  let pending: { tag: string; tabs: boolean; into: Simple }[] = []
  let i = 0

  const simple = () => (current ??= { words, raw: '', heredoc: '' })
  const add = (text: string) => {
    word ??= { text: '', inner: [] }
    word.text += text
  }
  const endWord = () => {
    if (word !== undefined) {
      words.push(word)
      word = undefined
    }
  }
  const endCommand = () => {
    endWord()

    if (words.length > 0 || current !== undefined) {
      const done = simple()

      done.words = words
      done.raw = raw
      out.push(done)
    }

    words = []
    raw = ''
    current = undefined
  }

  while (i < script.length) {
    const c = script[i] ?? ''

    if (c === '\n' && pending.length > 0) {
      endCommand()
      i += 1

      for (const { tag, tabs, into } of pending) {
        while (i < script.length) {
          const nl = script.indexOf('\n', i)
          const end = nl < 0 ? script.length : nl
          const line = script.slice(i, end)

          i = end + 1

          if ((tabs ? line.replace(/^\t+/, '') : line) === tag) {
            break
          }

          into.heredoc += `${line}\n`
        }
      }

      pending = []
      continue
    }

    if (c === '\\') {
      if (script[i + 1] !== '\n') {
        add(script[i + 1] ?? '')
      }

      raw += script.slice(i, i + 2)
      i += 2
      continue
    }

    if (c === "'") {
      const close = script.indexOf("'", i + 1)
      const end = close < 0 ? script.length : close

      add(script.slice(i + 1, end))
      raw += script.slice(i, end + 1)
      i = end + 1
      continue
    }

    if (c === '"') {
      const start = i

      add('')
      i += 1

      while (i < script.length && script[i] !== '"') {
        if (script[i] === '\\') {
          add(script[i + 1] ?? '')
          i += 2
        } else if (script.startsWith('$(', i)) {
          const [inner, end] = substitution(script, i + 2)

          word?.inner.push(inner)
          i = end + 1
        } else if (script[i] === '`') {
          const close = script.indexOf('`', i + 1)
          const end = close < 0 ? script.length : close

          word?.inner.push(script.slice(i + 1, end))
          i = end + 1
        } else {
          add(script[i] ?? '')
          i += 1
        }
      }

      i += 1
      raw += script.slice(start, i)
      continue
    }

    if (script.startsWith('$(', i) || script.startsWith('<(', i) || script.startsWith('>(', i)) {
      const [inner, end] = substitution(script, i + 2)

      add('')
      word?.inner.push(inner)
      raw += script.slice(i, end + 1)
      i = end + 1
      continue
    }

    if (c === '`') {
      const close = script.indexOf('`', i + 1)
      const end = close < 0 ? script.length : close

      add('')
      word?.inner.push(script.slice(i + 1, end))
      raw += script.slice(i, end + 1)
      i = end + 1
      continue
    }

    const heredoc = script.startsWith('<<<', i) ? null : /^<<(-?)\s*(?:'([^'\n]+)'|"([^"\n]+)"|\\?([\w.-]+))/.exec(script.slice(i, i + 200))

    if (heredoc !== null) {
      endWord()
      pending.push({ tag: heredoc[2] ?? heredoc[3] ?? heredoc[4] ?? '', tabs: heredoc[1] === '-', into: simple() })
      i += heredoc[0].length
      continue
    }

    if (c === '#' && word === undefined) {
      const nl = script.indexOf('\n', i)

      i = nl < 0 ? script.length : nl
      continue
    }

    // a redirection's & is not the end of a command
    if (c === '&' && (script[i - 1] === '>' || script[i - 1] === '<' || script[i + 1] === '>')) {
      raw += c
      i += 1
      continue
    }

    if (c === '\n' || c === ';' || c === '|' || c === '&' || c === '(' || c === ')') {
      endCommand()
      i += 1
      continue
    }

    if (c === ' ' || c === '\t' || c === '\r') {
      endWord()
      raw += ' '
      i += 1
      continue
    }

    add(c)
    raw += c
    i += 1
  }

  endCommand()

  return out
}

const KEYWORD = /^(if|then|elif|else|while|until|do|!|\{|time)$/

/** Whether running `script` would start something `pattern` names. */
export function startsHeavy(text: string, pattern: RegExp, depth = 0): boolean {
  // a command carried over to the next line is one command
  const script = text.replace(/[ \t]*\\\r?\n[ \t]*/g, ' ')

  if (!pattern.test(script)) {
    return false
  }

  if (depth > 6) {
    return true
  }

  const inside = (text: string) => startsHeavy(text, pattern, depth + 1)

  return parse(script).some(({ words, raw, heredoc }) => {
    if (words.some(w => w.inner.some(inside))) {
      return true
    }

    let at = 0

    while (at < words.length && (KEYWORD.test(words[at]?.text ?? '') || /^\w+=/.test(words[at]?.text ?? ''))) {
      at += 1
    }

    const texts = words.slice(at).map(w => w.text)
    const program = (texts[0] ?? '').split('/').pop() ?? ''
    const rest = texts.slice(1)

    // xargs, sudo and the like: what they run is somewhere in their words
    if (WRAPPERS.has(program)) {
      return rest.some((_, k) => {
        const name = (rest[k] ?? '').split('/').pop() ?? ''

        return !LIGHT.has(name) && pattern.test(rest.slice(k).join(' ')) && !rest.slice(0, k).some(t => LIGHT.has(t.split('/').pop() ?? ''))
      })
    }

    if (SHELLS.has(program)) {
      return inside(heredoc) || rest.some(inside) || pattern.test(raw)
    }

    if (program === 'find') {
      const exec = rest.findIndex(t => /^-(exec|execdir|ok)$/.test(t))

      return exec >= 0 && inside(rest.slice(exec + 1).join(' '))
    }

    if (program === 'nicotine') {
      // `nicotine agent run -- <command>` runs here; its other arguments are text
      const dashes = rest.indexOf('--')

      return dashes >= 0 && inside(rest.slice(dashes + 1).join(' '))
    }

    if (LIGHT.has(program)) {
      return false
    }

    return pattern.test(texts.join(' '))
  })
}
