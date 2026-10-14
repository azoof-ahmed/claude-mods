---
name: agent-bus
description: Talk to other agent sessions over the project's message bus (agent-bus). Use when you have a bus role, are told to message a role (coordinator, a worker, the user), receive "Bus mail for ..." context, or need to arm or re-arm the bus watch Monitor.
---

# agent-bus

The bus is a folder of append-only files (default `.claude/bus`). Mail waits for a ROLE, not a session: a successor that takes the same role reads the backlog.

The session-start context names your role and the exact CLI (`<python> "<plugin>/scripts/bus.py"`). It is written as `bus` below.

## Reading
- **While you work:** new urgent and normal mail is added after your tool calls as "Bus mail for <role> ...". You do not poll.
- **When you go idle:** a Monitor wakes you. Arm it once with the Monitor tool when the context asks:
  - command: `bus watch --as <role> --for 1790`;
  - timeout: 1800000 ms.

  It prints one line per new non-fyi message. When it ends, arm it again. Never run two of them.
- **Before you stop:** if mail is still unread when you end a turn, the Stop hook keeps you going once with it.
- **Backlog and fyi:**
  - `bus digest --as <role>` lists unread mail, one line each; `--mark` marks it all read.
  - `bus read <id>` prints the full text and body.

## Sending
`bus send <to[,to2]> "<one line>" [--prio urgent|normal|fyi] [--re <id>] [--cc a,b] [--topic T] [--body-file F]`

- **Priorities:**
  - `normal` (the default) is read at the recipient's next step;
  - `fyi` is never pushed and waits for the digest (FYIs, "done" notes nobody acts on);
  - `urgent` is only for interruptions (a stop order, a broken build). With the orca backend it may be typed into an idle terminal.
- **No acks.** Do not send "got it". Use `bus react <id> ✅` (or 👀 for "looking") instead: reactions notify nobody.
- **Long content:** more than 600 characters, or several lines, goes to a body file automatically. You can also pass `--body-file`.
- **Who is notified:**
  - `to`, `cc`, every registered `@role` in the text, the author of the message you `--re`, and the thread's subscribers.
  - `#channel` posts notify nobody unless they mention someone.
- **Threads:** `bus thread <id>`; `bus sub <id>` / `bus unsub <id>`.

## Other verbs
- `who`: roles and their modes.
- `status <id>`: receipts. `.` queued, `v` delivered, `vv` seen, `!` failed, `x` expired.
- `pin <id>`, `edit <id> "..."`, `delete <id>`: the last two only for your own messages, within 5 minutes.

## Joining the bus
1. If the project has no bus yet, create it: `bus init --roles coordinator,<others>`.
2. Join it: `bus register <role> --kind claude --mode file`.
3. Set `AGENT_BUS_ROLE=<role>` for the session. The coordinator does this when it starts a worker.

## Human views
- `/bus chat` starts the localhost web chat and prints its URL. People post there as the role `user`.
- `/bus` (no arguments) opens a compact chat pane for the person, who posts as `user`; `/bus full` (or `/bus tui`) opens the full-screen terminal client.
