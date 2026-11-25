import { expect, test } from 'claude-code/testing'
import { startsHeavy } from '../hooks/shell'

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
