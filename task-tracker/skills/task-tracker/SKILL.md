---
name: task-tracker
description: Use the project's shared task tracker (task.py) to find, claim, update and finish tasks, declare the files you edit, and avoid colliding with other agents. Use when the project has a task tracker, when told to pick up or report on a task id like TASK-12, or when several agents work in the same repository.
---

# Using the task tracker

The CLI ships with this plugin. `$TASKS_CLI` holds its path (the plugin sets it at session start), so every command is:

```bash
python "$TASKS_CLI" <command>      # PowerShell: python $env:TASKS_CLI <command>
```

Below, `T` stands for that prefix. Every read takes `--json`. `T --help` lists everything.

## Who you are

`--as <name>` (before the command), else `$TASKS_AS`, else `user`. If you were given a role (`worker-a`, `reviewer`, ...),
use it on every call. `T whoami` shows your name and permission level.

## The loop

1. **Before editing, claim.** `T mine` shows your tasks; `T list --assignee none` shows open ones. Then `T claim TASK-12`
   (atomic: one owner, moves it to in_progress). No task for your work? `T new "title" --touches "<glob>"`.
   A near-duplicate title is refused: comment on the existing task instead.
2. **Declare the files you edit:** `T touch TASK-12 "src/api/**"`. Starting work whose globs overlap another in-progress
   task is refused, naming the owner. Talk to them (`T comment TASK-9 "@worker-b ..."`), or `--force "<reason>"`
   (logged, and they are told). `T conflicts` lists every overlap.
3. **Between steps:** `T mine` (one line per task, reviews waiting on you, unread notes). `T inbox` reads the notes.
4. **Stuck:** `T move TASK-12 blocked -m "why"`, or `T block TASK-12 TASK-9` (you are told when TASK-9 closes).
5. **Finished:** `T move TASK-12 done`, or with a reviewer `T move TASK-12 in_review --reviewer reviewer
   [--proof <log or url>]`. A task with a reviewer reaches done only via `T review TASK-12 --approve` (the reviewer,
   a lead or an admin); `--reject -m "why"` sends it back.
6. **Giving up a task:** `T release TASK-12 -m "why"`.
7. Never edit files of a task you do not own without commenting to its owner first.

## Reading

`T show TASK-12` (body, links, touches, sub-tasks, comments, history), `T list --status in_progress,blocked --area api`,
`T board`, `T stats`, `T log [TASK-12]`, `T labels`, `T where` (database, roles file, prefix).

## Permissions

The roles file (`T where` shows its path) gives each name a level: admin (everything), lead (create, assign anyone,
review, move any task, import), member (create, claim, assign only themselves, update their own tasks, comment),
viewer (read only). A refusal prints the rule that refused. A member asking for someone else uses
`T assign TASK-12 worker-b --propose`. No roles file: everyone may do everything.

## No tracker yet

`T init` (or `/tasks init`) creates the database and a roles template.
