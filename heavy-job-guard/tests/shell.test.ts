import { expect, test } from 'claude-code/testing'
import { isLight, startsHeavy } from '../hooks/shell'

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
