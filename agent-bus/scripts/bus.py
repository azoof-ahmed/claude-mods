"""agent-bus: a message bus between agent sessions (Claude Code, Codex, plain shells), built on orca-bus.

    python bus.py init [--roles coordinator,worker-a]   create the bus dir and register roles (file mode)
    python bus.py send <to> "<text>" [--prio urgent|normal|fyi] [--re ID] [--cc a,b] [--topic T] [--body-file F]
    python bus.py digest --as <role>                     unread mail, one line each
    python bus.py read <id>                              full text + body
    python bus.py watch --as <role>                      for a Monitor: one line per new non-fyi message
    python bus.py watch-live --as <role>                 exit 0 when a live watch (heartbeat) exists for the role
    python bus.py whoami                                 the role this session resolves to
    python bus.py hook --event PostToolUse|Stop          serve new mail as JSON {"text": ...} (used by the mod)
    python bus.py deliver                                the typing daemon (orca backend only)
    python bus.py chat [--detach|--status|--stop]        the localhost web chat (prints its URL)
    python bus.py tui [--open] [--as ROLE] [--theme T]   the terminal client (Textual); --open: in a new terminal
    python bus.py snapshot [--since CURSOR] [--as user]  JSON for the /bus pane (messages, roles, health, cursor)
    every other orca-bus verb passes through: register, who, status, react, thread, sub, pin, edit, delete, ...

Configuration (first match wins): flags > <project>/.claude/claude-mods.json "agent-bus" > environment
(the plugin's options arrive as environment variables) > defaults.
    --dir / AGENT_BUS_DIR            bus directory (default .claude/bus under the project root)
    AGENT_BUS_ROLE                   this session's role (also ORCA_BUS_NAME, or the registry entry for the
                                     terminal handle in ORCA_TERMINAL_HANDLE)
    AGENT_BUS_ORCA_BUS_PATH          a checkout of orca-bus to import from (else the installed package)
    AGENT_BUS_BACKEND                orca | none: who may type urgent mail into a terminal (default none)
    AGENT_BUS_NUDGE_AFTER            seconds of unread mail before the daemon types one nudge (default 300)
    AGENT_BUS_CHAT_PORT / AGENT_BUS_CHAT_BIND   web chat port (8790) and bind address (127.0.0.1)
    AGENT_BUS_TASKS_DB               optional task-tracker db shown in the chat's lane list
    AGENT_BUS_TASK_PREFIX            the tracker's id prefix, linked as task chips in the chat (default TASK)
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WATCH_BEAT_S = 30       # the watch writes its heartbeat this often
WATCH_STALE_S = 90      # a heartbeat older than this is a dead watch
DEFAULTS = {
    'dir': '.claude/bus',
    'orcaBusPath': '',
    'backend': 'none',
    'nudgeAfter': 300,
    'chatPort': 8790,
    'chatBind': '127.0.0.1',
    'tasksDb': '',
    'roles': 'coordinator',
}
ENV = {
    'dir': 'AGENT_BUS_DIR', 'orcaBusPath': 'AGENT_BUS_ORCA_BUS_PATH', 'backend': 'AGENT_BUS_BACKEND',
    'nudgeAfter': 'AGENT_BUS_NUDGE_AFTER', 'chatPort': 'AGENT_BUS_CHAT_PORT', 'chatBind': 'AGENT_BUS_CHAT_BIND',
    'tasksDb': 'AGENT_BUS_TASKS_DB', 'roles': 'AGENT_BUS_ROLES',
}


def project_root(start=None):
    here = Path(start or os.getcwd()).resolve()
    for d in [here, *here.parents]:
        if (d / '.git').exists() or (d / '.claude').is_dir():
            return d
    return here


def load_config(flags=None, root=None):
    root = root or project_root()
    cfg = dict(DEFAULTS)
    for k, var in ENV.items():
        if os.environ.get(var):
            cfg[k] = os.environ[var]
    p = root / '.claude' / 'claude-mods.json'
    if p.exists():  # the project's own file beats the environment (the plugin's options arrive as env)
        try:
            cfg.update(json.loads(p.read_text(encoding='utf-8')).get('agent-bus', {}))
        except (ValueError, OSError) as ex:
            print(f'agent-bus: ignoring {p}: {ex}', file=sys.stderr)
    for k, v in (flags or {}).items():
        if v not in (None, ''):
            cfg[k] = v
    d = Path(cfg['dir'])
    cfg['dir'] = str(d if d.is_absolute() else root / d)
    cfg['nudgeAfter'] = float(cfg['nudgeAfter'])
    cfg['chatPort'] = int(cfg['chatPort'])
    if cfg['backend'] not in ('orca', 'none'):
        raise SystemExit(f"agent-bus: backend must be orca or none, not {cfg['backend']!r}")
    return cfg


VENDOR = HERE.parent / 'vendor'


def import_orca_bus(cfg):
    """orca-bus from orcaBusPath when set, else the copy vendored with this plugin (MIT, see vendor/orca_bus/LICENSE)."""
    sys.path.insert(0, cfg.get('orcaBusPath') or str(VENDOR))
    try:
        import orca_bus  # noqa: F401
        from orca_bus import cli  # noqa: F401
    except ImportError:
        raise SystemExit(f"agent-bus: cannot import orca_bus from {sys.path[0]}; check the orcaBusPath option")
    return orca_bus


def chat_url(cfg):
    host = cfg['chatBind'] if cfg['chatBind'] != '::1' else '[::1]'
    return f"http://{host}:{cfg['chatPort']}/"


def port_open(cfg):
    import socket
    try:
        with socket.create_connection((cfg['chatBind'], cfg['chatPort']), timeout=0.5):
            return True
    except OSError:
        return False


def detach(argv, log_path):
    """Start argv as a background process that outlives this one; returns its pid."""
    import subprocess
    kw = {'stdin': subprocess.DEVNULL, 'stdout': open(log_path, 'a'), 'stderr': subprocess.STDOUT}
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    return subprocess.Popen(argv, **kw).pid


def chat_verb(argv, cfg, root):
    """chat [--port N] [--bind ADDR] [--detach|--stop|--status]: the localhost web chat."""
    port, bind = take(argv, '--port'), take(argv, '--bind')
    if port:
        cfg['chatPort'] = int(port)
    if bind:
        cfg['chatBind'] = bind
    pidf = Path(root) / 'chat.pid'
    url = chat_url(cfg)
    if '--status' in argv:
        print(f'chat: {"up" if port_open(cfg) else "down"} at {url}')
        return 0
    if '--stop' in argv:
        try:
            pid = int(json.loads(pidf.read_text())['pid'])
        except (OSError, ValueError, KeyError):
            print('chat: not started by agent-bus (no chat.pid)')
            return 1
        if os.name == 'nt':
            import subprocess
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
        else:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        pidf.unlink(missing_ok=True)
        print(f'chat: stopped (pid {pid})')
        return 0
    if port_open(cfg):
        print(f'chat: already up at {url}')
        return 0
    if '--detach' in argv:
        pid = detach([sys.executable, str(Path(__file__).resolve()), '--dir', root, 'chat',
                      '--port', str(cfg['chatPort']), '--bind', cfg['chatBind']], Path(root) / 'chat.log')
        pidf.write_text(json.dumps({'pid': pid, 'url': url}))
        for _ in range(20):
            if port_open(cfg):
                break
            time.sleep(0.25)
        print(f'chat: {url} (pid {pid}; stop with `{reply_cmd()} chat --stop`)')
        return 0
    from orca_bus.web import serve as serve_chat
    print(f'chat: {url}', flush=True)
    serve_chat(root, cfg['chatPort'], cfg['tasksDb'] or None, cfg['chatBind'])
    return 0


def tui_verb(argv, cfg, root):
    """tui [--open] [--as ROLE] [--theme T]: the terminal client; --open starts it in a new terminal."""
    tui_dir = HERE.parent / 'tui'
    launcher = tui_dir / 'bus-tui'
    role = take(argv, '--as')
    theme = take(argv, '--theme')
    if '--open' not in argv:
        sys.path.insert(0, str(tui_dir))
        try:
            from bus_tui.app import main as tui_main
        except ImportError as ex:
            print(f'agent-bus tui: {ex} (pip install textual)', file=sys.stderr)
            return 2
        return tui_main(root, role or os.environ.get('AGENT_BUS_ROLE'), theme)
    cmd = [sys.executable, str(launcher), '--dir', root] + (['--as', role] if role else []) + (['--theme', theme] if theme else [])
    env_dir = f'AGENT_BUS_DIR={root}'
    import shutil
    import subprocess
    if cfg['backend'] == 'orca' and shutil.which('orca'):
        r = subprocess.run(['orca', 'terminal', 'create', '--title', 'bus-tui', '--json'], capture_output=True, text=True)
        try:
            handle = json.loads(r.stdout)['result']['handle']
            line = ' '.join(f'"{c}"' for c in cmd)
            subprocess.run(['orca', 'terminal', 'send', '--terminal', handle, '--text', line, '--enter'],
                           capture_output=True)
            print(f'tui: opened in Orca terminal {handle}')
            return 0
        except (ValueError, KeyError, TypeError):
            pass
    if os.environ.get('TMUX') and shutil.which('tmux'):
        subprocess.run(['tmux', 'new-window', '-n', 'bus-tui', '-e', env_dir] + cmd)
        print('tui: opened in a new tmux window')
        return 0
    if os.name == 'nt' and shutil.which('wt'):
        subprocess.Popen(['wt', '-w', '0', 'nt', '--title', 'bus-tui'] + cmd, env={**os.environ, 'AGENT_BUS_DIR': root})
        print('tui: opened in a new Windows Terminal tab')
        return 0
    print('tui: run this in a terminal: ' + ' '.join(f'"{c}"' if ' ' in c else c for c in cmd)
          + f'   (with {env_dir})')
    return 0


def reply_cmd():
    return f'{os.environ.get("AGENT_BUS_PYTHON") or "python"} "{Path(__file__).resolve().as_posix()}"'


def resolve_role(bus, explicit=None):
    for v in (explicit, os.environ.get('AGENT_BUS_ROLE'), os.environ.get('ORCA_BUS_NAME')):
        if v:
            return v
    return bus.name_for_handle(os.environ.get('ORCA_TERMINAL_HANDLE'))


# ---- the watch heartbeat: proves a Monitor is armed for a role

def heartbeat_path(root, role):
    return Path(root) / 'inbox' / f'{role}.watch.json'


def watch_live(root, role, now=None):
    try:
        hb = json.loads(heartbeat_path(root, role).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return (now or time.time()) - float(hb.get('beat', 0)) < WATCH_STALE_S


def start_heartbeat(root, role):
    path = heartbeat_path(root, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'pid': os.getpid(), 'started': started, 'beat': time.time()}), encoding='utf-8')
            os.replace(tmp, path)
            stop.wait(WATCH_BEAT_S)

    beat_once = threading.Thread(target=beat, daemon=True)
    beat_once.start()

    def clear():
        stop.set()
        try:
            hb = json.loads(path.read_text(encoding='utf-8'))
            if hb.get('pid') == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass
    return clear


# ---- the hook: serve new mail between steps

def serve(bus, role, event, line_v2):
    inbox = Path(bus.root) / 'inbox' / f'{role}.jsonl'
    pos_file = Path(bus.root) / 'inbox' / f'{role}.hookpos'
    try:
        size = inbox.stat().st_size
    except OSError:
        return None
    try:
        last = int(pos_file.read_text().strip() or 0)
    except (OSError, ValueError):
        last = -1
    if size == last and event != 'Stop':
        return None
    un = bus.unread(role, ('urgent', 'normal'))
    pos_file.write_text(str(size))
    if not un:
        return None
    cmd = reply_cmd()
    lines = [line_v2(m, cmd) for m in un[:15]]
    more = f'\n(+{len(un) - 15} more: {cmd} digest --as {role})' if len(un) > 15 else ''
    bus.mark_seen(role, [m['id'] for m in un[:15]])
    return (f'Bus mail for {role} ({len(un)} new; reply: {cmd} send <role> --re <id> "..."):\n'
            + '\n'.join(lines) + more)


# ---- snapshot: the in-Claude-Code chat's one read

SNAPSHOT_LIMIT = 150    # newest messages a snapshot carries
DELIVER_STALE_S = 60    # the deliver daemon writes deliver.heartbeat every few seconds


def bus_cursor(root):
    """A cheap fingerprint of every file the index tails: equal cursors mean nothing changed."""
    root = Path(root)
    parts = []
    for p in (root / 'log.jsonl', root / 'chat.jsonl', root / 'registry.json'):
        try:
            st = p.stat()
            parts.append(f'{st.st_size}:{int(st.st_mtime)}')
        except OSError:
            parts.append('-')
    seen = 0
    try:
        with os.scandir(root / 'inbox') as it:
            for e in it:
                if e.name.endswith('.seen'):
                    seen += e.stat().st_size
    except OSError:
        pass
    parts.append(str(seen))
    return '.'.join(parts)


def bus_health(root, me, now=None):
    """The deliver daemon's heartbeat, the user's live watch and the age of the last log write, in seconds."""
    now = now or time.time()
    root = Path(root)

    def age(p):
        try:
            return round(now - p.stat().st_mtime, 1)
        except OSError:
            return None
    d = age(root / 'deliver.heartbeat')
    return {'deliver': d is not None and d < DELIVER_STALE_S, 'deliver_age': d,
            'watch': watch_live(root, me, now), 'last_write_age': age(root / 'log.jsonl')}


def snapshot(bus, me='user', since=None, limit=SNAPSHOT_LIMIT, ensure=True):
    """What the in-Claude-Code chat draws, built from orca-bus's own web index (the web chat's data model)."""
    from orca_bus import chat
    from orca_bus.web import Index
    root = bus.root
    if ensure and me == 'user' and me not in bus.registry():
        # as the web chat's App.ensure_user: the human is a registered inbox role
        bus.register(me, None, 'shell', note='the human, via the in-Claude-Code chat', mode='inbox')
    cur = bus_cursor(root)
    out = {'bus': True, 'me': me, 'cursor': cur, 'health': bus_health(root, me)}
    if since and since == cur:
        out['unchanged'] = True
        return out
    idx = Index(bus)
    views, _ = idx.recent(limit=limit)
    views.sort(key=lambda v: v['time'])
    views = views[-limit:]
    reg = idx.reg
    names = set(reg)
    with idx.lock:
        seen = idx.seen.get(me, set())
        groups = idx.groups
        msgs = []
        for v in views:
            g = groups[v['id']]
            rc = g['rc']
            mine = rc.get(me)
            unread = bool(mine and mine['rid'] not in seen and g['prio'] != 'fyi'
                          and mine['st'] not in ('acked', 'failed'))
            parent = groups.get(v['re']) if v['re'] else None
            msgs.append({
                'id': v['id'], 'from': v['from'], 'time': v['time'], 'text': v['text'], 'body': v['body'],
                'prio': v['prio'], 'topic': v['topic'], 'chan': v['chan'], 'thread': v['thread'],
                'reply_to': v['re'],
                'to': [n for n, r in rc.items() if r['why'] == 'to' and not n.startswith('#')],
                'cc': [n for n, r in rc.items() if r['why'] == 'cc'],
                'reactions': v['rx'], 'pinned': v['pin'], 'edited': v['edited'], 'deleted': v['deleted'],
                'receipts': [{'to': r['to'], 'r': r['r'], 'tick': chat.TICKS.get(r['r'], '?'), 'why': r['why']}
                             for r in v['rc']],
                'rid': mine['rid'] if mine else None,
                'unread': unread,
                'mention': bool(mine and mine['why'] in ('mention', 'all')) or me in chat.mentions(v['text'], names),
                'reply_to_me': bool(parent and parent['from'] == me and v['from'] != me),
            })
        roles = [{'name': n, 'kind': e.get('kind'), 'mode': e.get('mode'), 'unread': idx.unread(n),
                  'watch': watch_live(root, n)} for n, e in sorted(reg.items())]
    chans = sorted({m['chan'] for m in msgs if m['chan']})
    out.update(messages=msgs, roles=roles, channels=['all'] + [c for c in chans if c != 'all'],
               unread=sum(1 for m in msgs if m['unread']),
               mentions=sum(1 for m in msgs if m['unread'] and (m['mention'] or m['reply_to_me'])))
    return out


def snapshot_verb(argv, root):
    """snapshot [--since CURSOR] [--as ROLE] [--limit N] [--body ID]: JSON for the in-Claude-Code chat."""
    from orca_bus.store import Bus
    me = take(argv, '--as') or 'user'
    since = take(argv, '--since')
    limit = int(take(argv, '--limit') or SNAPSHOT_LIMIT)
    body_id = take(argv, '--body')
    if not Path(root).is_dir():
        print(json.dumps({'bus': False, 'dir': str(root)}))
        return 0
    bus = Bus(root)
    if body_id:
        from orca_bus.web import Index
        print(json.dumps({'id': body_id, 'body': Index(bus).body(body_id)}, ensure_ascii=False))
        return 0
    print(json.dumps(snapshot(bus, me, since, limit), ensure_ascii=False))
    return 0


def take(argv, flag):
    """Pop `--flag value` from argv; return the value or None."""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    flags = {'dir': take(argv, '--dir')}
    cfg = load_config(flags)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    verb = argv[0]
    if verb == 'snapshot':
        import_orca_bus(cfg)
        for stream in (sys.stdout,):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except (AttributeError, ValueError):
                pass
        return snapshot_verb(argv[1:], cfg['dir'])
    if verb == 'hook' and not Path(cfg['dir']).is_dir():
        print(json.dumps({'bus': False, 'dir': cfg['dir']}))  # no bus in this project: stay out of the way
        return 0
    import_orca_bus(cfg)
    from orca_bus.store import Bus, BusError
    from orca_bus.cli import line_v2, main as orca_main
    root = cfg['dir']

    if verb == 'init':
        roles = take(argv, '--roles') or cfg['roles']
        bus = Bus(root)
        p = Path(root) / 'config.json'
        conf = bus.config()
        conf.update({'reply_cmd': reply_cmd()})
        conf.setdefault('notify', ['coordinator'])
        p.write_text(json.dumps(conf, indent=1), encoding='utf-8')
        names = [r.strip() for r in str(roles).split(',') if r.strip()]
        for n in names:
            if n not in bus.registry():
                bus.register(n, kind='claude', mode='file')
        print(f'agent-bus: {root} ready; roles: {", ".join(sorted(bus.registry()))}')
        return 0

    bus = Bus(root)
    if verb == 'whoami':
        role = resolve_role(bus, take(argv, '--as'))
        print(role or '')
        return 0 if role else 1

    if verb == 'watch-live':
        role = resolve_role(bus, take(argv, '--as'))
        live = bool(role) and watch_live(root, role)
        print('live' if live else 'none')
        return 0 if live else 1

    if verb == 'hook':
        event = take(argv, '--event') or 'PostToolUse'
        role = resolve_role(bus, take(argv, '--as'))
        out = {'bus': True, 'dir': str(Path(root).resolve().as_posix()), 'role': role, 'text': None,
               'watch': False, 'registered': False, 'cli': reply_cmd()}
        if role:
            entry = bus.registry().get(role) or {}
            out['registered'] = bool(entry)
            out['watch'] = watch_live(root, role)
            if entry.get('mode', 'file') == 'file':
                out['text'] = serve(bus, role, event, line_v2)
        print(json.dumps(out))
        return 0

    if verb == 'watch':
        role = resolve_role(bus, take(argv, '--as'))
        if not role:
            print('agent-bus: who are you? pass --as <role> or set AGENT_BUS_ROLE', file=sys.stderr)
            return 2
        clear = start_heartbeat(root, role)
        try:
            return orca_main(['watch', '--as', role] + argv[1:], root=root, default_reply_cmd=reply_cmd())
        except KeyboardInterrupt:
            return 0
        finally:
            clear()

    if verb == 'deliver':
        if cfg['backend'] != 'orca':
            print('agent-bus: the deliver daemon types into Orca terminals; backend is "none", so mail is served by '
                  'the hook and the watch alone. Set the backend option to orca to run it.', file=sys.stderr)
            return 2
        from orca_bus import deliver
        orig = deliver.Deliverer.nudge_deaf

        def nudge_unless_watched(self, reg=None):
            reg = reg if reg is not None else self.bus.registry()
            orig(self, {n: e for n, e in reg.items() if not watch_live(root, n)})
        deliver.Deliverer.nudge_deaf = nudge_unless_watched
        if '--nudge-after' not in argv:
            argv += ['--nudge-after', str(cfg['nudgeAfter'])]
        return orca_main(argv, root=root, default_reply_cmd=reply_cmd())

    if verb == 'chat':
        return chat_verb(argv[1:], cfg, root)

    if verb == 'tui':
        return tui_verb(argv[1:], cfg, root)

    if verb in ('send', 'digest', 'read', 'ack', 'react', 'sub', 'unsub', 'pin', 'unpin', 'delete', 'edit',
                'inbox') and '--as' not in argv and '--from' not in argv:
        role = resolve_role(bus)
        if role:
            os.environ['ORCA_BUS_NAME'] = os.environ.get('ORCA_BUS_NAME') or role
    try:
        return orca_main(argv, root=root, default_reply_cmd=reply_cmd())
    except BusError as ex:
        print(f'agent-bus: {ex}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
