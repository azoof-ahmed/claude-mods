# task-tracker

A shared task tracker for a team of Claude agents (and people) working in one repository. Agents claim tasks
before they edit, declare the files they touch, and get refused when their work overlaps another in-progress task.
You watch it all from a `/tasks` board inside Claude Code.

- **The CLI** (`scripts/task.py`, Python 3.9+, stdlib only): tasks with statuses, priorities, areas, labels, links,
  sub-tasks, blockers, comments and full history in one SQLite file (WAL mode, so many agents read and write at once).
  Claims are atomic. Every read takes `--json`.
- **File conflicts:** each task lists the globs it `touches`. Starting a task whose globs overlap another
  in-progress task is refused with the owner's name; `--force "<reason>"` overrides, logs it and tells them.
- **Roles:** an optional TOML file gives each name a level (admin, lead, member, viewer). No file: everyone may do
  everything.
- **Notifications:** assignments, review requests, blocked and unblocked tasks, @mentions and conflict overrides
  land in each name's inbox (`task.py inbox`), and optionally run a command of yours.
- **In Claude Code:** `/tasks` opens a board pane (board, list, progress, conflicts, task detail), a status line
  shows the counts, a band above the prompt flags conflicts, blocked tasks and reviews waiting on you, and toasts
  announce new blocks, rejected reviews and conflict overrides. At session start Claude is told the exact command
  to use.
- **Terminal app** (optional): `python scripts/board.py`, a live keyboard-driven board built on
  [Textual](https://textual.textualize.io/) (`python -m pip install textual`).

## Install

```
/plugin marketplace add azoof-ahmed/claude-mods
/plugin install task-tracker@claude-mods
```

or in one step:

```
/plugin install task-tracker --marketplace azoof-ahmed/claude-mods
```

Then, in a project: `/tasks init` (or `python <plugin>/scripts/task.py init`) creates `.claude/tasks/tasks.db` and a
roles template beside it.

## Options

Set them in `/config`, or in settings under `pluginConfigs["task-tracker"].options`.

| Option | Default | What it does |
| --- | --- | --- |
| `python` | `python` | Interpreter for the CLI. If it does not start, `python`, `python3` and `py` are tried in turn. |
| `identity` | `user` | The name the `/tasks` pane acts as. Agents use `--as <name>` or `TASKS_AS`. |
| `idPrefix` | `TASK` | Task ids read `<prefix>-<n>`. |
| `dbPath` | `.claude/tasks/tasks.db` | The database, relative to the project root (nearest folder with `.git` or `.claude`). |
| `rolesFile` | `.claude/tasks/roles.toml` | Names and permission levels. Missing: everyone may do everything. |
| `labels` | `bug,feature,chore,docs,test,blocked-external` | The label vocabulary; other labels are refused. Empty: any label. |
| `importers` | (none) | Sources `task.py import` reads; see [Importers](#importers). |
| `notifyCommand` | (none) | Run for each notification, with `{to}` `{sender}` `{key}` `{prio}` `{text}` filled in (no shell). |
| `refreshSeconds` | `3` | How often the status line, band and pane check for changes. `0` turns live updates off. |

The plugin hands these to the CLI as environment variables (`TASKS_PREFIX`, `TASKS_DB`, `TASKS_ROLES`,
`TASKS_LABELS`, `TASKS_IMPORTERS`, `TASKS_NOTIFY_CMD`), and sets `TASKS_CLI` to the CLI's path, so the model's shell
and any worker started from the session use the same tracker. A project can pin its own settings, which beat the
plugin options, in `.claude/claude-mods.json`:

```json
{ "task-tracker": { "idPrefix": "WEB", "labels": "bug,feature,infra" } }
```

Precedence: CLI flags (`--db`, `--roles`, `--prefix`, `--labels`, `--importers`, `--notify-cmd`) > the project file
> environment variables > defaults. `task.py where` shows what was resolved and from where.

## Usage

In Claude Code:

| Command | What it does |
| --- | --- |
| `/tasks` | Open the board pane and summarise the counts. |
| `/tasks TASK-12` (or `/tasks 12`) | Open one task. |
| `/tasks board` `list` `progress` `conflicts` | Switch the pane's view. |
| `/tasks area=api prio=high label=bug`, `/tasks clear` | Filter the board. |
| `/tasks init` | Create the tracker in this project. |

From any shell (`T` = `python "$TASKS_CLI"`, or the path to `scripts/task.py`):

```bash
T new "Rate-limit the login endpoint" --touches "src/auth/**" --label feature --area api
T --as worker-a claim TASK-1          # atomic; refuses on file conflicts
T --as worker-a touch TASK-1 "src/middleware/**"
T --as worker-a move TASK-1 in_review --reviewer reviewer --proof logs/test-run.txt
T --as reviewer review TASK-1 --approve
T mine | list | show TASK-1 | board | conflicts | stats | log | inbox | where
```

The skill bundled with the plugin teaches Claude this loop.

### Roles

`task.py init` writes a template:

```toml
default_level = "member"          # level for a name not listed below

[levels]
admin = ["user", "coordinator"]
lead = ["reviewer"]
member = ["worker-a"]
viewer = []

[identity]
no_assign = []                    # names that are tools, never assigned
strict_names = false              # true: assignees and reviewers must be listed above

[review]
default_reviewer = ""             # reviewer set on every new task
require_proof = false             # true: in_review needs --proof
```

| Level | May |
| --- | --- |
| admin | everything |
| lead | create, assign anyone, review, move any task, import |
| member | create, claim, assign only themselves (anyone else becomes a proposal), update their own tasks, comment |
| viewer | read only |

Names are self-declared (`--as`, `TASKS_AS`), so roles are guard rails for cooperating agents, not security.

### Importers

`task.py import` (lead or admin) seeds or refreshes tasks from your own sources and is safe to run repeatedly. Each
entry in `importers` (comma-separated) is a `.py` file or module name defining `records(ctx)`, or a command whose
stdout is JSON (an array, or one record per line). A record:

```json
{ "ext_key": "issues:42", "title": "Crash on empty input", "status": "todo", "priority": "high",
  "labels": ["bug"], "links": ["url:https://example.com/issues/42"], "touches": ["src/parser/**"],
  "parent": "issues:40", "comments": ["imported from the issue tracker"] }
```

`ext_key` and `title` are required. A re-import updates a field only while the tracker still holds the value the
last import wrote, so changes made in the tracker (a claim, a move) are never overwritten.

### Terminal board

```bash
python -m pip install textual
python scripts/board.py [--area api] [--label bug]
```

Keys: `1`-`4` views, `enter` opens a task, `/` searches, `a` `p` `l` cycle area, priority and label filters, `c`
clears them, `r` refreshes, `q` quits.

## Develop

```
claude plugin validate .
claude plugin test .
python -m unittest discover -s scripts/tests
claude --plugin-dir .
```

## License

MIT
