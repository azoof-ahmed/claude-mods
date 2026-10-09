# coordinator

Run a project from one **coordinator** session that steers many **worker** agents. Each worker runs in its own terminal, with a brief and a lane card.

The plugin brings the playbook (a skill), a CLI, a `/coordinator` command and hooks. The hooks tell each session whether it coordinates or works a lane, and remind workers to retire.

It works on its own. With [agent-bus](../agent-bus) it also messages workers, and with [task-tracker](../task-tracker) it shows each lane's tasks.

## Install

```
/plugin install coordinator --marketplace azoof-ahmed/claude-mods
```

Answer `y` to add the marketplace, then pick a scope. Python 3.9 or newer must be on PATH (or set the `python` option).

To get the whole setup, install the companions too:

```
/plugin install agent-bus --marketplace azoof-ahmed/claude-mods
/plugin install task-tracker --marketplace azoof-ahmed/claude-mods
```

## Quick start

```
/coordinator init                         # scaffold rules, templates, the question queue and config
/coordinator spawn worker-a .claude/coord/briefs/worker-a.md
/coordinator status                       # lanes, card states, questions for you, safe-to-close lanes
```

Or ask Claude: "coordinate this project with three workers". The `coordinator` skill covers the rest.

## What `init` creates

| Path (default) | What it is |
| --- | --- |
| `.claude/coord/RULES_CORE.md` | The short rules every worker reads first. Edit section 1 to describe your project. |
| `.claude/coord/lanes/README.md` | The lane-card template. A card is at most 150 lines and is the worker's handoff. |
| `.claude/coord/lanes/lanes.json` | Which lanes exist, where they run, and their generation. |
| `.claude/coord/briefs/TEMPLATE.md` | The brief template: goal, scope, steps, proof, constraints, report. |
| `.claude/coord/briefs/RESUME.md` | The successor startup: read the rules, the card and the backlog, then work. |
| `.claude/coord/briefs/scheduler.md` | A scheduler worker that grants one heavy job at a time, by RAM, battery and the user's activity. |
| `.claude/coord/briefs/HANDOFF_PROTOCOL.md` | How a worker retires and how a successor resumes. |
| `.claude/coord/reports/questions.jsonl` | The question queue: questions for the user, asked at status time and never blocking. |
| `.claude/claude-mods.json` → `coordinator` | The resolved configuration, shared by every worker. |

With agent-bus installed, `init` also creates the bus and registers the roles (default: `coordinator,scheduler`).

## Commands

`/coordinator <verb>` runs `coord.py <verb>`. Sessions also get the CLI as `$COORD_CLI`.

| Verb | What it does |
| --- | --- |
| `init [--roles a,b] [--force]` | Scaffolds the files above. Existing files are kept unless you pass `--force`. |
| `spawn <role> [brief] [--model M] [--repo P] [--dry-run]` | Starts a worker on a brief (default: the resume brief) through the backend. It refuses past `maxLanes`. |
| `retire <role> [--close]` | Asks the worker, by urgent bus mail, to update its card and stop. `--close` also closes its terminal. |
| `respawn <role>` | Closes the worker and starts a successor on the resume brief. The successor picks up from the card. |
| `status` | A report: live lanes with their card's State lines, open questions, safe-to-close lanes and the coordinator's inbox. |
| `lanes [--json]` | Lanes with backend, generation, card age and size, tracker tasks and the last SAFE TO CLOSE / KEEP OPEN. |
| `close-idle [--dry-run]` | Closes lanes whose last word is SAFE TO CLOSE. |
| `ask "<q>" [--from R] [--options "a\|b"]` | Queues a question for the user. |
| `questions [--all]` | Lists open questions (or all of them). |
| `answer <qid> "<a>"` | Records the answer and sends it to the asker on the bus. |
| `preflight [--min-ram-gb N] [--min-battery P] [--max-user-idle-s S]` | GO (exit 0) or WAIT (exit 1) for a heavy job, with the reasons. Works on Windows, Linux and macOS. |
| `config` | The resolved configuration. |

## Backends

| `backend` | How a worker starts | Retire/close |
| --- | --- | --- |
| `manual` (default) | Prints the command, with `AGENT_BUS_ROLE` set, for you to run in a new terminal. | You close it. |
| `orca` | `orca terminal create --worktree path:<repo> --title <role> --command ...`. The handle is registered on the bus. | `orca terminal close --tab` |
| `tmux` | `tmux new-window -n <role> -c <repo> -e AGENT_BUS_ROLE=<role>`, in the current session or a detached `coord` session. | `tmux kill-window` |
| `wt` | A new Windows Terminal tab (`wt -w 0 nt --title <role> -d <repo>`). | You close it. |

The worker command comes from two templates:

- `workerCommand`, default `claude --model {model} "{prompt}"`;
- `workerPrompt`, default `Read {brief} and execute it. Your role is {role}.`

Add flags such as a permission mode to `workerCommand`.

## Options

Set them in `/config`, or in settings under `pluginConfigs["coordinator"].options`. A project's `.claude/claude-mods.json` (written by `init`) wins over these, so one repo can differ from your defaults.

| Option | Default | What it does |
| --- | --- | --- |
| `repos` | `.` | Comma-separated repo paths workers start in. The first is the default. |
| `backend` | `manual` | `manual`, `orca`, `tmux` or `wt` (see Backends). |
| `shell` | `auto` | How `AGENT_BUS_ROLE` is set before the command: `auto` (PowerShell on Windows, POSIX elsewhere), `posix`, `powershell` or `cmd`. |
| `workerCommand` | `claude --model {model} "{prompt}"` | The worker command template. |
| `workerPrompt` | `Read {brief} and execute it. Your role is {role}.` | The worker's first prompt. |
| `workerModel` | `opus` | `{model}` in the template. `spawn --model` overrides it per lane. |
| `maxLanes` | `6` | The number of concurrent live lanes. |
| `retireTokens` | `250000` | Context tokens at which a worker is told to update its card and retire (again every further 10%). |
| `pauseAtPercent` | `97` | Usage-limit percentage at which sessions are told to checkpoint and pause. |
| `busDir` | `.claude/bus` | The agent-bus directory. |
| `trackerDb` | `.claude/tasks/tasks.db` | The task-tracker db, read-only, for lane tasks. |
| `rulesFile` | `.claude/coord/RULES_CORE.md` | The rules core. |
| `briefsDir` | `.claude/coord/briefs` | Briefs, the resume brief, the scheduler brief and the handoff protocol. |
| `cardsDir` | `.claude/coord/lanes` | Lane cards and `lanes.json`. |
| `reportsDir` | `.claude/coord/reports` | Progress logs, the question queue, and the message fallback without the bus. |
| `minFreeRamGb` | `4` | `preflight` answers WAIT below this. |
| `minBattery` | `40` | `preflight` answers WAIT below this when not charging. |
| `python` | `python` | The interpreter for the CLI. |

## What the hooks do

- **Session start, in a project that ran `init`:**
  - a coordinator session (no role, or the role `coordinator`) is told the live lanes, the open questions and the CLI;
  - a worker (`AGENT_BUS_ROLE` set) is pointed at the rules and its card.

  Other projects get nothing.
- **Every 10 tool calls**, the plugin reads the session's context size and usage windows:
  - past `retireTokens`, it adds a retirement reminder;
  - at `pauseAtPercent` of a usage window, it adds a checkpoint-and-pause notice.
- **The status line** shows `coord: N/M lanes, Q question(s)` in the coordinator session.

## Without the companions
- **No agent-bus:**
  - `retire` and `answer` print what to tell the worker;
  - the rules tell workers to write `<reportsDir>/<role>_progress.md`.
- **No task-tracker:** lanes show no tasks.

## Develop

From the repository root:

```
claude plugin validate ./coordinator
claude plugin test ./coordinator
python -m unittest discover -s coordinator/scripts/tests
claude --plugin-dir ./coordinator
```

## License

MIT
