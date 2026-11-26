import { expect, test } from 'claude-code/testing'
import { isLight, isShell, startsHeavy } from '../hooks/shell'

const HEAVY =
  /\b(tsc|vitest|jest|playwright|oxlint|eslint|xcodebuild|gradlew)\b|\b(bun|npm|pnpm|yarn)( run)? (typecheck|test|build|lint|check)\b|\bturbo (run )?(typecheck|test|build|lint)\b|\bcargo (build|test|check)\b/

const RUNS = [
  'bun run test',
  'cd app && npm run build 2>&1 | tail -5',
  'FOO=1 npx tsc --noEmit',
  'echo start; ./gradlew assembleDebug',
  'echo "$(npx jest --json)"',
  'out=`yarn lint`',
  'for d in a b; do npm test; done',
  'if true; then cargo test; fi',
  'sudo xcodebuild -list',
  'timeout 60 npx vitest run',
  "bash -c 'cd x && npm run typecheck'",
  'bash <<EOF\nnpm run build\nEOF',
  'find . -name "*.ts" -exec eslint {} +',
  'ls | xargs npx eslint',
  'nicotine agent run -n t -- cargo test',
  'cat <(npx tsc -v)',
  'cat > f <<E\ntext\nE\nnpm run build',
  'npm run \\\n build',
  'npm run build > log 2>&1 &',
  // quoting
  'npm "run" build',
  'n\\pm test',
  "npm run bu''ild",
  'npm run bu\\\nild',
  'npm   run\tbuild',
  'echo \\\\\nnpm test',
  'ls # note \\\nnpm test',
  "echo $'\\'' ; npm test #'",
  `echo "\${x:-"'"}" ; npm test #'`,
  'echo ${x:- #} ; npm test',
  'echo \\>& npm test',
  // substitutions
  '$(echo npm test)',
  'eval "$(echo npm test)"',
  'sh -c "$(echo npm test)"',
  'source <(echo npm test)',
  'x=$(echo ")" ; npm test)',
  'echo `echo \\`npm test\\``',
  'echo $((npm test) )',
  'cat <<E\n$(npm run build)\nE',
  'cat <<E\n`npm run build`\nE',
  // text handed to something that runs it
  'echo "npm test" | sh',
  'printf "npm test" | tee /dev/stderr | bash',
  'echo "npm test" | xargs -I{} sh -c {}',
  'echo "npm test" | while read l; do $l; done',
  'echo "npm test" | { bash; }',
  '{ echo npm test; } | bash',
  '{ echo npm test; } > x.sh; sh x.sh',
  'echo npm test > >(bash)',
  'bash <<< "npm test"',
  'cat <<E | bash\nnpm test\nE',
  'sudo bash <<E\nnpm test\nE',
  'env bash <<E\nnpm test\nE',
  "cat > x.sh <<'E'\nnpm test\nE\nbash x.sh",
  "cat > x.sh <<'E'\nnpm test\nE\nchmod +x x.sh && ./x.sh",
  'exec > x.sh; echo npm test; sh x.sh',
  'python3 - <<E\nimport os\nos.system("npm run build")\nE',
  'python3 -c "print(1)" "npm test"',
  // here-documents that end where they don't seem to
  "cat <<'E'\nfoo \\\nE\nnpm test",
  "cat <<E\nfoo\\\nE\necho '\nE\nnpm test\n#'",
  'cat <<E\"F\nx\nE\nnpm test',
  "git commit -m \"$(cat <<'E'\ndon't (worry)\nE\n)\" && npm test",
  // names for something else
  'X="npm test"; $X',
  'export X="npm test"',
  "alias t='npm test'; t",
  't() { npm test; }; t',
  'function grep { npm test; }; grep',
  'echo() { "$@"; }; echo npm test',
  './echo npm test',
  '/tmp/x/git npm test',
  "trap 'npm test' EXIT",
  'command npm test',
  'builtin command npm test',
  // environment
  "PAGER='npm test' git log",
  "GIT_EDITOR='npm test' git commit",
  'PATH=/tmp/x:$PATH cat npm test',
  'env -i FOO=1 npm test',
  'env -S "npm test"',
  'env -u cat npm test',
  // wrappers whose options are named like a light program
  'sudo -u git npm test',
  'sudo -nu git npm test',
  'sudo --user git npm test',
  'xargs -I cat npm test cat',
  'nice -n 10 npm test',
  'timeout -k 5 60 npm test',
  'time -p npm test',
  'ls | xargs env npm test',
  // find
  'find . -okdir npm test ;',
  'find . -exec echo {} + -exec npm test {} +',
  "find . -exec sh -c 'npm test' \\;",
  // light programs told to run something
  "git -c alias.t='!npm test' t",
  "git config alias.t '!npm test' && git t",
  'git bisect run npm test',
  'git rebase -x "npm test" main',
  "git submodule foreach 'npm test'",
  'git grep -O"npm test" x',
  'echo "npm test" > .git/hooks/pre-commit && git commit -m x',
  "gh alias set --shell t 'npm test'",
  "tmux new-session -d 'npm test'",
  "tmux send-keys -t x 'npm test' Enter",
  "tmux display -p '#(npm test)'",
  `awk 'BEGIN{system("npm test")}'`,
  "sed '1e npm test' f",
  "sed 's/x/npm test/e' f",
  "sed -ne 's/x/npm test/e' f",
  'ssh localhost npm test',
  'ssh -o BatchMode=yes me@127.0.0.1 npm test',
  "ssh -o ProxyCommand='npm test' host",
  'ssh $HOST npm test',
  "rg --pre 'npm test' x",
  "sort --compress-program='npm test' x",
  'codex sandbox macos npm test',
  'nicotine agent new shell -p "npm test"',
  'nicotine agent new claude -- --x "npm test"',
  // zsh
  'for x in a b { npm test }',
  'if [[ a ]] npm test',
  'if { true } npm test',
  'repeat 3 npm test',
  'noglob npm test',
  'for x in "npm test"; do $x; done',
  // what it can't follow
  'echo "npm test',
  'case x in a) npm test;; esac',
]

/** Local builds: each must still count, however it is spelled. */
const LOCAL_BUILDS = [
  'cargo build',
  'cargo build --release 2>&1 | tail -20',
  'bash -c "cargo test"',
  "sh -c \"bash -c 'cargo test'\"",
  'ls crates | xargs cargo test -p',
  'ls | xargs -n1 cargo check -p',
  'env X=1 cargo build',
  'env -i PATH=/usr/bin cargo build',
  'X=1 cargo test',
  "cat > b.sh <<'E'\ncargo build\nE\nbash b.sh",
  "cat > b.sh <<'E'\ncargo build\nE\nchmod +x b.sh && ./b.sh",
  'nicotine resource run heavy -- cargo test',
  "nicotine resource run board -- sh -c 'cargo test'",
  'nicotine resource run -p high heavy -- env X=1 cargo build',
  'piforge prep -- cargo build',
  'piforge -C . prep -- sh -c "cargo test"',
  'piforge run -- true && cargo build',
  "piforge run -- sh -c 'echo hi'; cargo test",
  // text that reaches something that may run it
  'echo "cargo test" > run.txt; sh run.txt',
  'echo "cargo test" > run.txt; python3 x.py run.txt',
  'echo "cargo test" > notes.md; python3 x.py < notes.md',
  'echo "cargo test" >> notes.md; cp notes.md x.sh; ./x.sh',
  'echo "cargo test" >> notes.md; cat notes.md > x.sh; ./x.sh',
  'echo "cargo test" | tee x.sh >/dev/null; sh x.sh',
  "cat > Makefile <<'E'\nall:\n\tcargo build\nE\nmake",
  "cat > old.py <<'E'\nimport os; os.system('cargo test')\nE\npython3 other.py",
  'echo "cargo test" | python3 x.py',
  'echo "cargo test" | grep cargo | python3 x.py',
  'python3 x.py "$(echo cargo test)"',
  'python3 x.py <<E\n$(echo cargo test)\nE',
  'git commit -m "cargo build" && git log -1 --format=%s | sh',
  'nicotine agent send me "cargo test"; nicotine agent read me | bash',
  'X=$(git log -1 --format=%s); git commit -m "cargo test"; $X',
  'git commit -m "cargo test"; git log -1 --format=%s > Makefile; make',
  "sed -i '' 's/^/cargo test/' x.sh && python3 y.py",
  'awk \'{print "cargo test" > "x"}\' f; python3 y.py',
  'dd if=/dev/zero of=x.sh <<< "cargo test"; python3 y.py',
  // groups, functions and redirections of more than one command
  'for f in a; do echo "cargo test"; done > x.sh; python3 y.py',
  'for f in a; do echo "cargo test"; done | python3 y.py',
  '{ echo "cargo test"; } > x; python3 y.py',
  '( echo "cargo test" ) | python3 y.py',
  'exec > x.sh; echo cargo test; python3 y.py',
  'run() { "$@"; }; run cargo test',
  'echo cargo test | python3 <(echo)',
]

/** Text and jobs handed to the build board: nothing heavy runs here, whatever else the script does. */
const NOT_LOCAL = [
  `piforge -C /w run -- sh -c '. .toolchain/env; for o in main c75; do echo "== OLD $o"; OLD_RULES=$o cargo test --bin x config:: 2>&1 | grep -v "^warning\\|Compiling"; done; true'`,
  `cat > /s/old.py <<'E'\nprint(1)\nE\npython3 /s/old.py\npiforge -C /w run -- sh -c 'cargo test'\ngit checkout -- src`,
  'piforge -C /w build test',
  'piforge build test && python3 report.py',
  'nicotine resource run board -- piforge -C /w build plugin-test -- ./heavy-job-guard',
  'nicotine resource run board -- piforge -C /w run -- cargo test',
  'nicotine resource run --timeout 60 board -- piforge run -- sh -c "cargo test"',
  'piforge ci && piforge run -- cargo build --release; bun scripts/notify.ts',
  'piforge prep my-task && piforge run -- cargo test',
  'ssh board "cd x && cargo test" && python3 parse.py',
  'nicotine agent send lead "cargo test passes on the board"; python3 x.py',
  'echo "next: cargo test" >> notes.md; python3 x.py',
  "cat >> card.md <<'E'\n- cargo test passes on the board\nE\nbun x.ts",
  'git commit -m "fix: cargo test on board" && bun run deploy',
  "git commit -F - <<'E'\ncargo test passes\nE\npython3 x.py",
  'git commit -m "$(cat <<\'E\'\ncargo test passes\nE\n)" && python3 x.py',
  'nicotine agent send lead "$(cat <<\'E\'\nran cargo test\nE\n)"; bun x.ts',
  'echo "cargo test" | grep -c cargo; python3 x.py',
  'echo "cargo test" > /dev/null; python3 x.py',
  "S=/x; mkdir -p $S; cat > $S/probe.ts <<'E'\nconsole.log(1)\nE\ncat > $S/notes.md <<'E'\ncargo test\nE\nbun $S/probe.ts",
  'while pgrep -f "cargo build" >/dev/null; do sleep 5; done; python3 next.py',
  'until ! pgrep -fl "cargo (build|test)"; do sleep 10; done',
  'piforge run -- cargo test 2>&1 | tail -5 > out.log; python3 parse.py',
]

const ONLY_MENTIONS = [
  "pgrep -fl 'Simulator|xcodebuild'",
  "cat > notes.md <<'E'\nrun cargo test -- --nocapture\nE\nls",
  'python3 - <<E\nprint("npm run build")\nE',
  'git commit -m "fix: jest config; npm run test passes"',
  'gh pr create --body "checked with cargo test (board)"',
  'grep -rn "bun run test" . | head',
  'ps aux | grep -E "vitest|jest"',
  'echo "then: npm run build"',
  'ls # npm run build',
  'nicotine agent send a "please run cargo test"',
  'piforge run -- cargo test',
  'ls | xargs grep vitest',
  'ls | xargs -n 1 grep vitest',
  'sudo -n grep vitest /etc/hosts',
  "git commit -m \"$(cat <<'E'\ndon't (run) npm test\nE\n)\" && git push",
  'cd app && git -C x log --grep "npm test" | head -5',
  "sed -n '1,20p' vitest.config.ts",
  "sed -i '' 's/jest/vitest/g' a.ts",
  'N=$(pgrep -f vitest | wc -l); echo $((N + 1))',
  'until grep -q "npm test" log; do sleep 5; done',
  'find . -name "jest.config.*" -exec grep -l vitest {} +',
  'ssh -o BatchMode=yes board.example "cargo test"',
  'tmux display-message "npm test done"',
  'command -v jest',
  'nicotine agent new claude -p "then cargo test on the board"',
  'if [ -f a ]; then cat jest.config.js; fi',
  'echo "npm test" > notes.txt',
  '/usr/bin/grep vitest a',
  'echo ${x:-1} $((2 * 3)) vitest',
]

test('finds the command that would run', async () => {
  for (const c of RUNS) {
    expect([c, startsHeavy(c, HEAVY)]).toEqual([c, true])
  }
})

test('text that only mentions one starts nothing', async () => {
  for (const c of ONLY_MENTIONS) {
    expect([c, startsHeavy(c, HEAVY)]).toEqual([c, false])
  }
})

test('a local build counts, however it is spelled', async () => {
  for (const c of LOCAL_BUILDS) {
    expect([c, startsHeavy(c, HEAVY)]).toEqual([c, true])
  }
})

test('jobs for the build board and text start nothing here, beside other commands', async () => {
  for (const c of NOT_LOCAL) {
    expect([c, startsHeavy(c, HEAVY)]).toEqual([c, false])
  }
})

/** A small seeded generator, so a failure can be replayed from the seed it prints. */
function random(seed: number) {
  let x = seed >>> 0 || 1

  const next = () => {
    x ^= x << 13
    x ^= x >>> 17
    x ^= x << 5

    return (x >>> 0) / 4294967296
  }

  return { next, pick: <T>(list: readonly T[]): T => list[Math.floor(next() * list.length)] as T }
}

const NASTY = [
  '', ' ', 'cargo test', 'CARGO TEST', 'Cargo Build', '1234567890', '*', '?', '[a-z]*', '{a,b}', '~', '$HOME', '$(x)', '`x`',
  '"', "'", '\\', '\n', '\r\n', '\t', '\u0000', '\u0007', '\u001b[31m', 'é', '日本語', '🚀', 'a'.repeat(5000), ';', '|', '&',
  '>', '<<E', '#', '!', '-', '--', '\u202e', 'cargo\ntest', 'npm run build',
]
const LIGHT_PIECES = ['ls', 'git status', 'pwd', 'echo ok', 'sleep 1', 'git -C x log -1', 'grep -c x f']
const UNKNOWN_PIECES = ['python3 x.py', 'bun x.ts', './tool', 'node a.js', 'make lint-docs']
const BUILDS = ['cargo build', 'cargo test', 'npm run build', 'npx tsc --noEmit', './gradlew assemble']
const single = (s: string) => `'${s.replaceAll("'", `'\\''`)}'`
const double = (s: string) => '"' + s.replace(/[\\"$`]/g, m => '\\' + m) + '"'

function text(r: ReturnType<typeof random>): string {
  const parts = Array.from({ length: 1 + Math.floor(r.next() * 4) }, () => r.pick(NASTY))

  return parts.join(r.pick([' ', '', '\n', 'cargo test']))
}

test('generated: quoted text is text, beside commands it cannot vouch for', async () => {
  const seed = Date.now() % 100000
  const r = random(seed)
  const started = Date.now()

  for (let n = 0; n < 400; n += 1) {
    const message = `${text(r)} cargo test`
    const said = r.pick([
      `nicotine agent send lead ${double(message)}`,
      `echo ${single(message)}`,
      `git commit -m ${double(message)}`,
      `echo ${double(message)} >> notes.md`,
      `piforge run -- sh -c ${single(message)}`,
      `ssh board ${single(message)}`,
    ])
    const script = [said, r.pick(UNKNOWN_PIECES), r.pick(LIGHT_PIECES)].sort(() => r.next() - 0.5).join(r.pick(['; ', ' && ', '\n']))

    expect([`seed ${seed}`, script, startsHeavy(script, HEAVY)]).toEqual([`seed ${seed}`, script, false])
  }

  expect(Date.now() - started < 10000).toBe(true)
})

test('generated: a build on a line of its own counts, whatever comes before it', async () => {
  const seed = Date.now() % 100000
  const r = random(seed)
  const started = Date.now()

  for (let n = 0; n < 400; n += 1) {
    const before = [text(r), r.pick([...LIGHT_PIECES, ...UNKNOWN_PIECES])].join(r.pick(['; ', '\n', ' ']))
    // a line ending in a backslash joins the build to it
    const script = `${before.replace(/\\+$/, '')}\n${r.pick(BUILDS)}`

    if (/(^|\n)[^\n'"]*#/.test(before)) {
      continue
    }

    expect([`seed ${seed}`, script, startsHeavy(script, HEAVY)]).toEqual([`seed ${seed}`, script, true])
  }

  expect(Date.now() - started < 10000).toBe(true)
})

test('generated: any input gives an answer quickly', async () => {
  const seed = Date.now() % 100000
  const r = random(seed)
  const started = Date.now()

  for (let n = 0; n < 300; n += 1) {
    const script = Array.from({ length: 1 + Math.floor(r.next() * 12) }, () => r.pick([...NASTY, ...LIGHT_PIECES, ...UNKNOWN_PIECES, ...BUILDS])).join(r.pick(['', ' ', '\n', '|', ';', '(', ')', '{', '}']))

    expect([`seed ${seed}`, typeof startsHeavy(script, HEAVY)]).toEqual([`seed ${seed}`, 'boolean'])
  }

  // ten thousand commands in one script
  const long = Array.from({ length: 10000 }, (_, k) => (k % 2 === 0 ? 'echo "cargo test" | grep c' : 'python3 x.py')).join('\n')

  expect(startsHeavy(long, HEAVY)).toBe(false)
  expect(startsHeavy(`${long}\ncargo test`, HEAVY)).toBe(true)
  expect([`seed ${seed}`, Date.now() - started < 10000]).toEqual([`seed ${seed}`, true])
})

test('a process is light by its program, not by a word in its path', async () => {
  for (const c of [
    'grep -rn npm test',
    '/opt/homebrew/bin/gh pr create --body "npm test"',
    '/home/a/.local/bin/claude -p "npm test"',
    'C:\\tools\\git.exe commit -m "npm test"',
    '"C:\\Program Files\\Git\\cmd\\git.exe" commit -m "npm test"',
  ]) {
    expect([c, isLight(c)]).toEqual([c, true])
  }

  for (const c of [
    '/home/a/cat dir/node_modules/.bin/jest',
    'C:\\ps test\\node_modules\\.bin\\jest',
    'C:\\Program Files\\nodejs\\node.exe jest',
    '"C:\\Program Files\\nodejs\\node.exe" jest',
    'node /x/.bin/vitest',
  ]) {
    expect([c, isLight(c)]).toEqual([c, false])
  }
})

test('a shell is told apart from a job by its program', async () => {
  for (const c of ['/bin/zsh -c while pgrep -f "cargo build"; do sleep 5; done', 'bash -c cargo test', 'C:\\Windows\\System32\\cmd.exe /c npm test', 'pwsh -Command npm test']) {
    expect([c, isShell(c)]).toEqual([c, true])
  }

  for (const c of ['cargo test', '/home/a/sh dir/node_modules/.bin/jest', 'node /x/.bin/vitest', 'python3 -c "cargo test"']) {
    expect([c, isShell(c)]).toEqual([c, false])
  }
})
