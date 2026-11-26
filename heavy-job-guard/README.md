# heavy-job-guard

A Claude Code mod that stops Claude from starting a heavy job (typecheck, tests, lint, builds) while
another one is already running on the machine. It helps most on laptops with little memory, where two
`tsc` runs at once can freeze the machine.

Before every Bash command that matches its pattern, it lists the machine's processes. If a heavy job
is already running, it refuses the command and tells Claude what is running and for how long, so
Claude waits instead of piling on. Because it reads the process list rather than its own history, it
also sees jobs started in the background, by subagents, by other Claude sessions and in your own
terminals.

It judges only what would run on this machine:

- **Text is not a command.** A commit message, a search, a message to another agent, an `echo`, a note
  appended with `>>` or a here-document written to a file that mentions a test runner is let through.
- **Remote jobs are not local.** A command handed to the build board (`piforge run/build/ci/ssh`, also
  through `nicotine resource run <name> -- piforge …`) or to `ssh host …` runs elsewhere. `piforge prep
  -- <command>` and `nicotine resource run <name> -- <command>` run the command here, so it is judged.
- **Other commands don't make text count.** When the script also runs something it can't vouch for (a
  script, an interpreter, a tool it doesn't know), a mention counts only if its text can reach that
  command: through a pipe, by being captured into its words, through a file the script writes and
  the command names (also by a glob such as `*.txt`), a file a tool may read on its own (a `Makefile`,
  `CMakeLists.txt`; plain notes like `.md`, `.txt` and `.log` are exempt), or what git, nicotine or the
  clipboard keep and print back.
- **When unsure, it refuses.** Shells, variables used as commands, functions, groups and loops whose
  output goes somewhere, `exec` redirections, an interpreter whose program could write files or start
  processes, and syntax it can't follow make any mention count. A false refusal costs a wait; a false
  pass costs the machine's memory.

Some text is still refused: a variable holding the text that is later used as an argument, and a
non-note file written with the text beside a command it can't vouch for, even when nothing runs it.

Out of scope: a job that hides behind a script's own name is seen only once it is running, and text
typed into another agent's shell (a prompt or keystrokes sent to a local terminal) is that agent's to run.

Only real programs count as a running job: a compiler (`rustc`, `clang`, `swiftc`…), a test runner, gradle
and the like, also under a wrapper such as `timeout`, `env` or `nice`. A build daemon (Gradle, Kotlin)
counts while it uses CPU. A shell or an interpreter running inline code only starts other processes, so
a waiting loop (`while pgrep -f "cargo build"; do sleep 5; done`) is not a job; a shell's script counts
when it would start one. Tool servers an agent keeps open (MCP servers) are never jobs.

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
| `pattern` | tsc, vitest, jest, playwright, oxlint, eslint, xcodebuild, gradlew, `bun/npm/pnpm/yarn [run/workspace x/--filter x] typecheck/test/t/build/lint/check`, `turbo run …`, `cargo [+toolchain] [-flags] build/test/check/clippy/run/bench/doc/nextest` (and `b`, `t`, `c`, `r`) | Regex for a heavy job. It is matched against both the new command and every running process. |
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
