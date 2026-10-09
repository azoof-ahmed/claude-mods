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
| `usage [--probe] [--json] [--debug]` | The latest usage reading (session and weekly %). `--probe` reads `/usage` now (orca, tmux). `--debug` saves every raw screen read to `<reportsDir>/usage-debug/`. |
| `usage-watch [--start] [--once] [--probe] [--debug]` | Probes usage on the `probeIntervals` schedule and alerts the coordinator at the thresholds. A reading younger than the interval is reused (and says so); one without numbers never is, and a probe that reads no numbers never replaces a good reading. `--probe` forces a probe now. `--start` runs it in its own terminal. |
| `pause [--tick] [--wait]` | The graceful pause: asks every live lane to checkpoint and stop. `--tick` advances it; `--wait` runs it to the end. The plugin ticks it every minute. |
| `paused` | A worker reports that it has checkpointed (uses `AGENT_BUS_ROLE`, or `--role`). |
| `resume [--dry-run]` | Respawns every paused lane from its card on the resume brief. |
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
| `pauseWeeklyPercent` | `80` | Weekly usage at which the coordinator is alerted to run the graceful pause. |
| `pauseSessionPercent` | `97` | 5-hour session usage at which every session is told to checkpoint and pause itself. |
| `probeIntervals` | `0:60,70:20,78:10` | The watcher's schedule as weekly%:minutes steps: every 60 min, every 20 from 70%, every 10 from 78%. |
| `probeCommand` | `claude --model haiku` | The throwaway session the watcher opens to read `/usage`. |
| `pauseReminderMin` | `10` | Minutes before a silent lane gets one reminder. |
| `pauseGraceMin` | `20` | Minutes before a silent lane is interrupted and its terminal closed. |
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
  - in the coordinator session, at `pauseWeeklyPercent` of the weekly window, it adds a "start the graceful pause"
    note and a toast, once per crossing;
  - in every session, at `pauseSessionPercent` of the 5-hour window, it adds a checkpoint-and-pause notice.
- **The status line** shows `coord: N/M lanes, Q question(s)` in the coordinator session.

## Usage watcher and graceful pause

The goal is to stop at about 80% of the weekly limit without losing work. Going a little over while lanes finish is fine.

**Where the readings come from:**
- **The coordinator session itself.** The plugin reads the usage windows that Claude Code reports with each response (`$.session.usage()`): the 5-hour session window and the weekly window. It writes them to `<reportsDir>/usage.json`, so they count as a probe. No extra session is needed while the coordinator is busy.
- **The watcher**, for when the coordinator is idle: `coord usage-watch --start` runs it in its own terminal.
  - It reuses a fresh reading when there is one.
  - Otherwise it opens a throwaway session (`probeCommand`, a cheap model), types `/usage`, reads the screen and closes it.
  - It probes every 60 minutes, every 20 minutes from 70% and every 10 minutes from 78% (`probeIntervals`).
  - Screen reading needs the `orca` or `tmux` backend. With `wt` or `manual`, only the coordinator session's own readings are available.
- **At the threshold**, the watcher sends the coordinator an urgent bus message, once per crossing. Without agent-bus it appends to `<reportsDir>/alerts.jsonl` and prints. In the coordinator session the plugin adds a note and a toast.

**`/coordinator pause`:**
1. Every live lane gets an urgent PAUSE. Each lane:
   - finishes or parks its atomic step;
   - updates its lane card;
   - releases any scheduler slot;
   - runs `coord paused`;
   - ends with SAFE TO CLOSE.
2. The plugin advances the pause every minute (or run `coord pause --wait`):
   - lanes that checkpointed are closed;
   - silent lanes get one reminder after `pauseReminderMin`;
   - after `pauseGraceMin`, silent lanes get an Escape and are closed. Their card is what they last wrote.
3. When every lane is done, it writes `<reportsDir>/RESUME_LIST.md`.

**`/coordinator resume`** respawns every paused lane on the resume brief. Each successor reads its card and its waiting bus mail, and carries on.

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
