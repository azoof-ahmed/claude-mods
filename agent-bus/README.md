# agent-bus

A message bus between agent sessions: Claude Code tabs, Codex, plain shells. Any number of agents, each with a **role**, send each other mail through a folder of append-only files.

- **A busy agent** gets its new mail after each tool call.
- **An idle agent** is woken by a Monitor.
- **People** follow along in a chat pane inside Claude Code (`/bus`), a localhost web chat, or a full-screen
  vim-style terminal client.

Mail waits for the role, not the session: a successor that takes over a role reads the backlog.

It is built on [orca-bus](https://github.com/polygon-mv/orca-bus) (MIT), vendored in `vendor/orca_bus` with its licence. Orca, the terminal workspace app, is optional: it is only needed to type urgent mail into terminals.

## Install

```
/plugin install agent-bus --marketplace azoof-ahmed/claude-mods
```

Answer `y` to add the marketplace, then pick a scope. Python 3.9 or newer must be on PATH (or set the `python` option). The terminal client also needs `pip install textual`.

## Quick start

```
/bus init                      # create .claude/bus and register the roles in the `roles` option
```

Then start each agent with `AGENT_BUS_ROLE=<role>` set. The [coordinator](../coordinator) plugin does this for you. Inside a session:

```
/bus                           # open the chat pane (as the role `user`)
/bus @worker-a rebase first    # send a message to a role as user
/bus digest                    # your unread mail, one line each
/bus send worker-a "rebase on main before the next slice"
/bus chat                      # start the web chat in the background and print its URL
/bus full                      # open the full-screen terminal client in a new terminal (also `/bus tui`)
```

The agent itself uses the CLI that its session context names: `python "<plugin>/scripts/bus.py" ...`, also available as `$AGENT_BUS_CLI`. The `agent-bus` skill teaches it the verbs.

## How mail reaches an agent: hooks plus a Monitor

| When | What delivers the mail | Cost |
| --- | --- | --- |
| The agent is working | A **PostToolUse hook** checks the role's inbox size after each tool call. Only when the inbox grew does it run the bus and add new urgent and normal mail to the model's context. | No extra turn |
| The agent is about to go idle | A **Stop hook** holds the agent back once if it has unread mail and hands it the mail. Once per 10 minutes, if no watch is live, it asks the agent to arm one before idling. | One continuation |
| The agent is idle | A **Monitor** running `bus watch --as <role> --for 1790` (timeout 1800000 ms) prints one line per new non-fyi message. The line wakes the agent. | A turn per message |
| Session start | A **SessionStart hook** names the role and the CLI, serves the backlog and, unless a watch is already live, asks the agent to arm the Monitor. | None |
| Backstop (orca backend only) | The **deliver daemon** (`bus deliver`) types one nudge into an idle Orca tab after `nudgeAfter` seconds of unread mail. It never nudges a role with a live watch. The hooks start it (one per bus dir) and restart it when its heartbeat goes stale. | One turn |

**How "a watch is live" is known:** `bus watch` writes a heartbeat (`inbox/<role>.watch.json`, refreshed every 30 s, stale after 90 s). The hooks and the daemon read it, so nobody is asked to arm a second Monitor and nobody is nudged while one listens.

`fyi` mail is never pushed. It waits in `bus digest`.

## The CLI

`/bus <verb>` and `bus <verb>` take the same verbs (`/bus` alone opens the chat pane, `/bus @role text` sends to that role, and any other word shows the help; free text never reaches the CLI).

| Verb | What it does |
| --- | --- |
| `init [--roles a,b]` | Creates the bus directory and registers the roles in file mode. |
| `send <to[,to2]> "<text>" [--prio urgent\|normal\|fyi] [--re ID] [--cc a,b] [--topic T] [--body-file F]` | Sends a message. Text over 600 characters, or with several lines, becomes a body file. |
| `digest [--as R] [--mark]` | Lists unread mail, one line each, grouped by priority and topic. |
| `snapshot [--since CURSOR] [--as user] [--body ID]` | JSON for the `/bus` pane: messages with receipts and reactions, roles, channels, bus health and a cursor. |
| `read <id>` | Prints the full text and body, and marks it read. |
| `watch [--as R]` | For a Monitor: prints one line per new non-fyi message and keeps the heartbeat. |
| `react <id> <emoji>` | ✅ 👀 👍 ❌ 🙏 🎉. Reactions never notify anyone. |
| `thread <id>`, `sub <id>`, `unsub <id>` | Threads. |
| `pin`, `unpin`, `edit`, `delete` | Edit and delete work on your own messages, within 5 minutes. |
| `status [id]` | Receipts: `.` queued, `v` delivered, `vv` seen, `!` failed, `x` expired. |
| `who` | Lists the roles. |
| `register <role> --kind claude --mode file [--handle H]` | Joins the bus. |
| `chat [--detach\|--status\|--stop] [--port N] [--bind A]` | The web chat. |
| `tui [--open] [--as R] [--theme T]` | The terminal client. |
| `deliver` | The typing daemon. It needs the orca backend. Every session's hook starts it in the background unless the pid in `deliver.pid` is a running daemon (one per bus dir, under a start lock); one that has not ticked for 10 minutes is stopped and replaced. Output goes to `deliver.out` and `deliver.log` in the bus dir. Create `deliver.STOP` there to stop it and keep it stopped. |
| `whoami`, `watch-live` | Diagnostics. |

**Who is notified:**

- the `to` and `cc` roles;
- every registered `@role` in the text;
- the author of the message you `--re`;
- the thread's subscribers.

A post to a `#channel` notifies nobody unless it mentions someone. `@all` is for admins only (`user`, `coordinator`, `scheduler`).

## Chat inside Claude Code (/bus)

`/bus` with no arguments opens a compact chat pane, about 56 columns wide, where you read and write as the role
`user` (the same identity as the web chat; it is registered on first use). The full experience is the standalone
client: `/bus full` (see [the standalone full-screen client](#the-standalone-full-screen-client-bus-tui)).

**How it opens.** Claude Code has no full-screen overlay for plugins, so the chat is a pane opened with the keyboard
focus and "close on Escape". On the main screen it sits inline above the prompt, as tall as the layout spares; in
the fullscreen layout it docks beside the transcript as a side bar. It stays open until you close it.

**What it shows.**

- A header, for example `bus user ●3 @1 dlv✓ 12s`: unread count, unread @mentions and replies to you, whether the
  deliver daemon's heartbeat is fresh, and the age of the last write to the log.
- The recent messages, oldest at the top. `●` marks unread mail for you, `@` an @mention of you or a reply to
  you, `↳` a reply, `¶` a message with a body. Your own messages show the receipt of their least advanced
  recipient: `.` queued, `v` delivered, `vv` seen, `!` failed, `x` expired, `#` a channel post.
- An expanded message: its full text, recipients and topic, each recipient's receipt, the reactions, and the body.
  Expanding a message addressed to you marks it read.
- A one-line composer. A line that starts with `@role` names goes to those roles; any other line is posted to
  `#all`, and the bus routes it to the @mentioned roles and the coordinator. Typing `@` offers matching role names
  below the field. While replying, the field is labelled with the author and the line goes to them with `--re`.

**Keys.** A plugin pane can take keys in two ways, and the pane uses both:

| Key | Vim region (click it first) | Hotkey (pane focused) | Action |
| --- | --- | --- | --- |
| `j` / `k`, `↓` / `↑` | yes | `j` / `k` | Move the selection |
| `gg` / `G`, `Home` / `End` | yes | | First / last message |
| `Ctrl-d` / `Ctrl-u` | yes | | Down / up eight messages |
| `Enter`, `o`, `l` / `h` | yes | `o` | Expand or collapse the selected message (`h` collapses) |
| `r` | yes | `r` | Reply to the selected message |
| `e`, then `1`-`6` | yes | `e`, then `1`-`6` | React: ✅ 👀 👍 ❌ 🙏 🎉 |
| `i` / `a` | yes | | Focus the composer |
| `x` | yes | `x` | Cancel the reply or the emoji row |
| `q` | yes | `q` | Close the pane |
| `Esc` | | yes | Close the pane (while the pane holds the keyboard) |

The **vim region** is the one-line `NORMAL` bar at the bottom of the pane: a raw-key area that receives every key
once it has the focus (a click on it). The **hotkeys** work whenever the pane holds the keyboard (it opens
focused; a click or `ctrl+x tab` gives it back); they are also drawn as a row of buttons you can click. Both run
the same actions. Rows are buttons too: a click selects and expands one.

**The band.** While the pane is closed and you have unread mail, a one-line band above the prompt reads
`bus: 3 unread · 1 @you [ Open ]`; it disappears when the count is zero. New @mentions of `user` and replies to
`user` also raise a toast once each. Turn the band off with the `band` option.

**Polling.** The pane runs `bus.py snapshot` every 2 seconds while it is open and stops when it closes; the band
looks every 15 seconds. `snapshot --since <cursor>` answers `{"unchanged": true}` cheaply when nothing on the bus
changed. Every write goes through the regular verbs: `send --from user`, `react <id> <emoji> --as user`,
`read <id> --as user`.

**Themes.** The `theme` option: `tokyonight` (blue accent, the default) or `claude` (orange accent, `#D97757`).

**Limitations.**

- No true overlay: a plugin can draw only a pane (inline or docked) and the band above the prompt.
- No global keybinding opens it: type `/bus`, or press `Open` on the band.
- Raw keys (`gg`, `G`, `Ctrl-d`, `Ctrl-u`, `Enter`, arrows, `h`/`l`, `i`/`a`) reach the pane only after a click on the
  vim region, and Escape there returns the focus instead of reaching the pane. Button hotkeys are limited to one
  lowercase letter or digit, so `G`, `gg`, `Ctrl-d`/`Ctrl-u` and `Enter` have no hotkey (Enter does press a
  focused row).
- While the composer has the focus, letters type into it rather than acting as keys.
- The composer is one line: multi-line messages and bodies need `/bus send ... --body-file` or the full client.
- No search, filters, pins, edits, deletes or thread view in the pane: use the full client or the web chat.
- VS Code and mobile have no raw-key region; mobile has no text field, so the pane is read-only there.

## Web chat

`/bus chat` starts the server in the background and prints its URL, by default `http://127.0.0.1:8790/`. The page shows:

- threads, mentions, receipts and reactions;
- pins and edits;
- a per-role lane list, with in-progress tasks when `tasksDb` points at a task-tracker db.

People post as the role `user`. Their posts reach the named roles, and always the coordinator.

The server binds to loopback only and checks the `Host` header. Stop it with `/bus chat --stop`.

## The standalone full-screen client (bus-tui)

The full experience: a Textual client for the bus with vim-style modal keys. It reads the bus incrementally (it polls about once a
second) and writes only through the same store and chat functions the CLI and the web chat use.

### Launch

```bash
/bus full                      # from Claude Code: opens it in a new terminal (Orca, tmux or Windows Terminal;
                               # else prints the command); /bus tui does the same
agent-bus/tui/bus-tui          # directly in a terminal; on Windows: agent-bus\tui\bus-tui.cmd
bus-tui --as coordinator --theme claude --dir path/to/bus
```

- Bus directory: `--dir`, else `$AGENT_BUS_DIR`, else `.claude/bus` under the nearest ancestor that holds `.git`
  or `.claude`.
- Role: `--as`, else `$AGENT_BUS_ROLE`, else `user` (registered on first use, the same way the web chat does).
- Needs Textual (`pip install textual`). Without it the launcher prints that hint and exits with status 2.

### Layout

- **Tabs** (top): `All`, `@me` (to you or from you), one tab per `#channel`, and every thread you open. Unread
  counts appear on each tab.
- **Tree** (left): channels, roles (`●` means the role's watch heartbeat is live; the number is its unread count),
  threads, and pinned messages. It hides below 100 columns. `Space e` toggles it.
- **Messages** (right): threads ordered by last activity, with replies indented under their root. Each row shows
  `●` when unread, the time, the sender, the recipients, `#topic`, the text, `▸body`, `📌`, reactions, and
  receipts on your own messages.
- **Composer** (above the statusline): used in insert mode.
- **Statusline**:
  - left: the mode, your role, the tab, active filters and search;
  - right: your unread count, then bus health: `deliver` (age of the delivery daemon heartbeat, `-` when no
    daemon runs), `watch` (live watch heartbeats, `*` when yours is one of them), `log` (age of the last log
    write), then your position in the list.
- **Toasts**: shown when a new message @mentions your role or replies to you.

### Normal mode

| Key | Action |
| --- | --- |
| `j` / `k`, `↓` / `↑` | next / previous message |
| `gg` / `G` | first / last message |
| `Ctrl-d` / `Ctrl-u` | half page down / up (`Ctrl-f` / `Ctrl-b`: full page) |
| `Enter` / `o` | message detail: full text, body, receipts per recipient, reactions |
| `i` / `a` | insert mode (write in the composer) |
| `c` | compose modal (to, topic, priority, multi-line text) |
| `r` | reply modal for the selected message |
| `e` / `+` | reaction palette: ✅ 👀 👍 ❌ 🙏 🎉 (`1`-`6`; picking one you already added removes it) |
| `p` | pin / unpin |
| `s` | follow / unfollow the thread (its replies then reach your inbox) |
| `t` | open the selected message's thread in a tab |
| `x` | close the thread tab |
| `H` / `L`, `gT` / `gt` | previous / next tab |
| `Tab` | move focus between the tree and the list (in the tree: `j` / `k` move, `Enter` opens) |
| `/` | search: jumps to the first match and highlights every match |
| `n` / `N` | next / previous match |
| `:` | command line |
| `y` | copy the message id |
| `Esc` | clear the search, then the filters |
| `?` | help |
| `Space` | leader (shows the which-key popup) |

Receipts use the bus CLI's symbols: `.` queued, `v` delivered, `vv` seen, `!` failed, `x` expired.

### Insert mode

| Key | Action |
| --- | --- |
| `Enter` | send to the current tab. In a channel tab it posts to that channel, in a thread tab it replies to the selected message, and anywhere else it posts to `#all`. An `@role` in the text notifies that role. |
| `Esc` | back to normal mode |

### Leader (`Space`)

| Key | Action |
| --- | --- |
| `f` | fuzzy picker over everything: channels, roles, threads, messages |
| `r` | roles (picking one filters by it; pick it again to clear) |
| `t` | threads |
| `b` | channels and tabs |
| `p` | pinned messages |
| `e` | toggle the tree |
| `n` | compose |
| `u` | toggle the unread filter |
| `@` | toggle the to-me filter |
| `m` | mark every message in the view read |
| `T` | theme picker |
| `h` | help |
| `q` | quit |

In the picker, typing filters the list (subsequence match; whole words rank first), `↑` / `↓` or `Ctrl-p` /
`Ctrl-n` move, `Enter` picks, and `Esc` closes.

### Command line (`:`)

| Command | Action |
| --- | --- |
| `:reply <text>` | reply to the selected message (without text: opens the reply modal) |
| `:react <emoji> [off]` | toggle a reaction, or remove it with `off` (without arguments: opens the palette) |
| `:send <to[,to2]\|#chan> <text>` | send a message |
| `:thread [id]` | open a thread tab |
| `:pin` / `:unpin` | pin or unpin the selected message |
| `:sub` / `:unsub` | follow or unfollow the selected message's thread |
| `:edit <text>` / `:delete` | edit or delete your own message (within 5 minutes) |
| `:filter [@role] [#chan] [mentions] [unread] [words…]` | narrow the list (`:filter` alone or `:filter clear` resets it) |
| `:theme <name>` | switch the theme |
| `:tree` | toggle the tree |
| `:seen` | mark the view read |
| `:close` | close the thread tab |
| `:compose` | open the compose modal |
| `:help` | show the keymap |
| `:q` | quit |

### Themes

`tokyonight` (default), `catppuccin` (mocha), and `claude` (warm dark with an orange `#D97757` accent). Pick one
with `--theme`, `:theme <name>`, or `Space T`.

## Options

Set them in `/config`, or in settings under `pluginConfigs["agent-bus"].options`. They reach the CLI as environment variables. A project's `.claude/claude-mods.json` section `"agent-bus"` overrides the bus options (`busDir`, `roles`, `orcaBusPath`, `backend`, `nudgeAfter`, `chatPort`, `chatBind`, `tasksDb`; `dir` is still read as an old name for `busDir`); the session options (`role`, `python`, `monitor`, `taskPrefix`, `band`, `theme`) come from `/config` only. Other keys are reported and ignored. Only `init` creates the bus directory.

| Option | Default | What it does |
| --- | --- | --- |
| `busDir` | `.claude/bus` | The bus directory, relative to the project root. |
| `role` | (empty) | The session's role when `AGENT_BUS_ROLE` and `ORCA_BUS_NAME` are unset and the terminal is not registered. |
| `roles` | `coordinator` | The roles `init` registers. |
| `orcaBusPath` | (empty) | A checkout of orca-bus to use instead of the vendored copy. |
| `python` | `python` | The interpreter. |
| `backend` | `none` | `orca` lets the deliver daemon type urgent mail and nudges into Orca tabs. `none` never types. |
| `nudgeAfter` | `300` | Seconds of unread mail before the daemon's one nudge (orca backend). |
| `monitor` | `on` | `off` stops the "arm a Monitor" requests. |
| `chatPort` | `8790` | The web chat port. |
| `chatBind` | `127.0.0.1` | `127.0.0.1`, `localhost` or `::1`. Loopback only. |
| `tasksDb` | (empty) | A task-tracker db for the chat's lane list. |
| `taskPrefix` | `TASK` | Task ids with this prefix (`TASK-12`) become task chips in the chat. |
| `band` | `on` | `off` hides the unread band above the prompt. |
| `theme` | `tokyonight` | The `/bus` pane's colors: `tokyonight` or `claude`. |

The bus's `config.json` holds `reply_cmd`, the command printed in every reply hint, nudge and watch line. `init` writes this CLI's own (the `python` option resolved to a full path, else the interpreter running it, and the versioned script path), and each session's start and stop hooks replace it when its python or script is missing or it names an older plugin version.

**Roles:** a session's role is resolved in this order:

1. `AGENT_BUS_ROLE`;
2. `ORCA_BUS_NAME`;
3. the bus registry entry for `ORCA_TERMINAL_HANDLE`;
4. the `role` option.

## Files

The bus directory (default `.claude/bus`) holds:

| File | What it holds |
| --- | --- |
| `registry.json` | The roles and their modes |
| `log.jsonl` | Every message and delivery state |
| `chat.jsonl` | Reactions, subscriptions, pins and edits |
| `inbox/<role>.jsonl` | The role's mail |
| `inbox/<role>.seen` | What the role has read |
| `inbox/<role>.hookpos` | The hook's cursor |
| `inbox/<role>.watch.json` | The watch heartbeat |
| `bodies/<id>.md` | Long message bodies |

Commit the folder or ignore it, as you prefer. It is plain text.

## Develop

From the repository root:

```
claude plugin validate ./agent-bus
claude plugin test ./agent-bus
python -m unittest discover -s agent-bus/scripts/tests
(cd agent-bus && python -m unittest discover -s tui/tests -t tui)
cd agent-bus/vendor && python -m unittest tests.test_bus tests.test_chat
```

## License

MIT. `vendor/orca_bus` is orca-bus 0.5.0 © 2026 polygon-mv, MIT (see `vendor/orca_bus/LICENSE`). It is changed only to make the chat's task-id prefix configurable.
