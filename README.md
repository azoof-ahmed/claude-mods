# claude-mods

General-purpose Claude Code mods (plugins). Each one works in any repository and is configured through plugin options, so nothing is tied to one project. Together they run a project as a **coordinator** steering many **worker** agents, each in its own terminal.

| Mod | What it does |
| --- | --- |
| [`coordinator`](coordinator) | Runs many worker agents from one coordinator session. `/coordinator init` scaffolds a rules core, lane cards, briefs, a resume brief, a scheduler-worker brief, a handoff protocol and a question queue. It spawns, retires and respawns workers in Orca, herdr, nicotine, tmux, Windows Terminal or by hand, and produces status reports. Workers are reminded to retire at a token threshold and to pause near usage limits. |
| [`agent-bus`](agent-bus) | A message bus between agent sessions. Mail reaches a busy agent between tool calls and wakes an idle one through a Monitor. Urgent mail can be typed into Orca or herdr terminals. Includes a localhost web chat and a vim-style terminal client (`/bus chat`, `/bus tui`). |
| [`task-tracker`](task-tracker) | A SQLite task tracker with a `task` CLI, role permissions and conflict detection by `touches` globs. In Claude Code it adds a `/tasks` board, a status line and toasts. It also has an optional Textual board. |
| [`code-index`](code-index) | Cheap reading for agents: a compact code index, markdown TOCs with line numbers, and `STATUS(<task>)` comment tags. |
| [`guardrails`](guardrails) | A lint gate on Claude's edits with a baseline ratchet. The project supplies the rules; only new violations block. |
| [`heavy-job-guard`](heavy-job-guard) | Refuses a second heavy job (typecheck, tests, builds) while one is already running on the machine. Works on macOS, Linux and Windows. |

Each mod works alone. Installed together, `coordinator` uses `agent-bus` for messages and `task-tracker` for lane tasks.

## Install

From a Claude Code terminal session:

```
/plugin install coordinator --marketplace azoof-ahmed/claude-mods
/plugin install agent-bus --marketplace azoof-ahmed/claude-mods
/plugin install task-tracker --marketplace azoof-ahmed/claude-mods
/plugin install code-index --marketplace azoof-ahmed/claude-mods
/plugin install guardrails --marketplace azoof-ahmed/claude-mods
/plugin install heavy-job-guard --marketplace azoof-ahmed/claude-mods
```

Answer `y` to add the marketplace, then pick a scope. Or add the marketplace once with `/plugin marketplace add azoof-ahmed/claude-mods` and pick from `/plugin`.

The Python parts need Python 3.9 or newer on PATH. Every mod has a `python` option if yours has another name. The two optional terminal UIs (the bus client and the task board) need `pip install textual`.

## Configuration

Every mod reads its settings in this order, first match wins:

1. flags you type on its CLI;
2. the project's `.claude/claude-mods.json`, one section per mod (for example `"coordinator": {...}`), which `coordinator init` writes;
3. your per-workspace map in `~/.claude/claude-mods.json` (agent-bus, coordinator and task-tracker), for options you keep at user level but that differ per workspace;
4. the plugin options (`/config`, or `pluginConfigs["<mod>"].options` in settings), which reach the CLIs as environment variables;
5. the defaults.

So your plugin options are your defaults everywhere, and a repo can override them for everyone working in it. Keep workspace-specific settings in the workspace's own file, not in the plugin options: those are shared by every workspace.

Paths (bus, cards, tracker and the like) belong to one project. One that comes from an option or the environment and points outside the project is ignored, with a note. It may have been inherited from another project's session, for example through a terminal, multiplexer or daemon started there. The setting then falls back to the project's file or the default. Likewise, an `AGENT_BUS_ROLE` that arrives with a `COORD_LANE` for another project is ignored.

The per-workspace map: each key is an absolute path (or a glob) and applies only inside it. A longer key beats a shorter one.

```json
{
  "workspaces": {
    "/Users/me/projects/clarity": { "agent-bus": { "roles": "coordinator,scheduler" } },
    "/Users/me/projects/*": { "coordinator": { "maxLanes": 2 } }
  }
}
```

## Develop

```
claude plugin validate ./<mod>
claude plugin test ./<mod>
python -m unittest discover -s <mod>/scripts/tests
claude --plugin-dir ./<mod>
```

## License

MIT, © 2026 Azoof Ahmed. `agent-bus/vendor/orca_bus` is orca-bus by polygon-mv, also MIT (its licence is kept beside it).
