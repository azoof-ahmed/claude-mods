# heavy-job-guard

A Claude Code mod that stops Claude from starting a heavy job (typecheck, tests, lint, builds) while
another one is already running on the machine. It helps most on laptops with little memory, where two
`tsc` runs at once can freeze the machine.

Before every Bash command that matches its pattern, it lists the machine's processes. If a heavy job
is already running, it refuses the command and tells Claude what is running and for how long, so
Claude waits instead of piling on. Because it reads the process list rather than its own history, it
also sees jobs started in the background, by subagents, by other Claude sessions and in your own
terminals.

It reads the process list with `ps` on macOS and Linux, and with PowerShell's process table
(`Get-CimInstance Win32_Process`) on Windows. If neither works, it shows a toast once per check and lets
the command run.

## Install

```
/plugin install heavy-job-guard --marketplace azoof-ahmed/claude-mods
```

Answer `y` to add the marketplace, then pick a scope.

## Options

Set them in `/config`, or in settings under `pluginConfigs["heavy-job-guard"].options`.

| Option | Default | What it does |
| --- | --- | --- |
| `pattern` | tsc, vitest, jest, playwright, oxlint, eslint, xcodebuild, gradlew, `bun/npm/pnpm/yarn [run] typecheck/test/build/lint/check`, `turbo run …`, `cargo build/test/check` | Regex for a heavy job. It is matched against both the new command and every running process. |
| `ignore` | `--lsp`, `--watch`, `watch`, language servers, tsserver | Regex for running processes that match `pattern` but never finish on their own. They never block. |
| `mode` | `block` | `block` refuses the command; `warn` runs it and shows a toast. |
| `subagents` | `allow` | `deny` refuses every heavy job a subagent starts, so only the main agent runs them. |

## Develop

From the repository root:

```
claude plugin validate ./heavy-job-guard
claude plugin test ./heavy-job-guard
claude --plugin-dir ./heavy-job-guard
```

## License

MIT
