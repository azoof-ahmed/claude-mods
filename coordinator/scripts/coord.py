"""coordinator: run many worker agents, each in its own terminal with a brief, from one coordinator session.

    python coord.py init [--roles a,b] [--force]       scaffold rules, templates, the question queue and config
    python coord.py spawn <role> [<brief>] [--repo P] [--model M] [--coordinator] [--dry-run]
                                                        start a worker on a brief (default: the resume brief);
                                                        --coordinator (or role `coordinator`) skips maxLanes
    (any verb) --herdr-session NAME                      pin herdr calls to this session (default: the one this shell runs in)
    python coord.py succeed [--model M] [--dry-run]     (the coordinator) start the successor coordinator on its card
    python coord.py handed-over                         (the old coordinator) say SAFE TO CLOSE after `succeed`
    python coord.py close-predecessor [--wait S]        (the successor) close the old coordinator once it said SAFE TO CLOSE
    python coord.py retire <role> [--close]             ask a worker to update its card and stop; --close its terminal
    python coord.py respawn <role> [--dry-run]          close the worker and start a successor on its lane card
    python coord.py lanes [--json]                      the lanes, their terminals, cards and tasks
    python coord.py status                              a status report: lanes, open questions, safe-to-close
    python coord.py close-idle [--dry-run]              close the terminals of lanes whose last word is SAFE TO CLOSE
    python coord.py reap [<role>...] [--dry-run]        kill tmux sessions still running retired lanes (or these lanes)
    python coord.py ask "<question>" [--from ROLE] [--options "a|b"]   queue a question for the user
    python coord.py questions [--all]                   the open (or all) questions
    python coord.py answer <qid> "<answer>"             answer a question; the asker gets it on the bus
    python coord.py preflight [--min-ram-gb N] [--min-battery P] [--max-user-idle-s S]
                                                        GO (exit 0) or WAIT (exit 1) for a heavy job, with reasons
    python coord.py usage [--probe] [--json] [--debug]   the latest usage reading (session and weekly %)
    python coord.py usage-watch [--start] [--once] [--probe] [--debug]  probe usage on a schedule; alert the coordinator at the thresholds
    python coord.py pause [--tick] [--wait] [--now]     graceful pause: every lane checkpoints its card and stops
    python coord.py paused [--role R]                   (a worker) report that this lane has checkpointed and stops
    python coord.py resume [--dry-run]                  respawn the paused lanes from their cards
    python coord.py config                              the resolved configuration

Configuration (first match wins): flags > <project>/.claude/claude-mods.json "coordinator" > environment
(COORD_<KEY>; the plugin's options arrive this way) > defaults. Keys: see DEFAULTS. `init` writes the resolved
values into the project file, so every worker, shell or not, shares them.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / 'templates'

DEFAULTS = {
    'repos': '.',
    'backend': 'manual',              # auto | orca | herdr | tmux | wt | manual
    'shell': 'auto',                  # auto | posix | powershell | cmd: how the env prefix is written
    'workerCommand': 'claude --model {model} "{prompt}"',
    'workerPrompt': 'Read {brief} and execute it. Your role is {role}.',
    'workerModel': 'opus',
    'maxLanes': 6,
    'retireTokens': 250000,
    'pauseSessionPercent': 97,        # 5-hour window: every session checkpoints and pauses itself
    'pauseWeeklyPercent': 80,         # weekly window: the coordinator runs the graceful pause
    'probeIntervals': '0:60,70:20,78:10',  # weekly %:minutes between usage probes
    'probeCommand': 'claude --model haiku',
    'pauseReminderMin': 10,           # minutes before a silent lane gets one reminder
    'pauseGraceMin': 20,              # minutes before a silent lane is interrupted and closed
    'busDir': '.claude/bus',
    'trackerDb': '.claude/tasks/tasks.db',
    'rulesFile': '.claude/coord/RULES_CORE.md',
    'briefsDir': '.claude/coord/briefs',
    'cardsDir': '.claude/coord/lanes',
    'reportsDir': '.claude/coord/reports',
    'minFreeRamGb': 4,
    'minBattery': 40,
}
INTS = ('maxLanes', 'retireTokens', 'pauseSessionPercent', 'pauseWeeklyPercent', 'minBattery')
FLOATS_EXTRA = ('pauseReminderMin', 'pauseGraceMin')
FLOATS = ('minFreeRamGb',)
PATHS = ('busDir', 'trackerDb', 'rulesFile', 'briefsDir', 'cardsDir', 'reportsDir')
BACKENDS = ('auto', 'orca', 'herdr', 'tmux', 'wt', 'manual')


def detect_backend(env=None):
    """`auto`: herdr inside a herdr pane (HERDR_ENV=1), orca inside an Orca terminal, tmux inside tmux, else manual.
    herdr is checked first: a herdr pane that runs the agent inside tmux (claude-auto-retry) still has HERDR_ENV."""
    env = os.environ if env is None else env
    if env.get('HERDR_ENV') == '1' and env.get('HERDR_PANE_ID'):
        return 'herdr'
    if env.get('ORCA_TERMINAL_HANDLE'):
        return 'orca'
    if env.get('TMUX'):
        return 'tmux'
    return 'manual'


# Child output is UTF-8 (orca, claude, the bus); the Windows locale codepage (cp1252) cannot decode it.
TEXT = {'text': True, 'encoding': 'utf-8', 'errors': 'replace'}


def env_key(k):
    return 'COORD_' + re.sub(r'(?<!^)(?=[A-Z])', '_', k).upper()


def project_root(start=None):
    here = Path(start or os.getcwd()).resolve()
    for d in [here, *here.parents]:
        if (d / '.git').exists() or (d / '.claude').is_dir():
            return d
    return here


class Config(dict):
    def __init__(self, root, flags=None):
        super().__init__(DEFAULTS)
        self.root = root
        self.file = root / '.claude' / 'claude-mods.json'
        for k in DEFAULTS:
            v = os.environ.get(env_key(k))
            if v:
                self[k] = v
        if self.file.exists():  # the project's own file beats the environment (the plugin's options arrive as env)
            try:
                section = json.loads(self.file.read_text(encoding='utf-8')).get('coordinator') or {}
                for k in section:
                    if k == 'python':
                        print(f'coordinator: {self.file}: "python" is a plugin option (set it in /config); '
                              f'the project file does not set it', file=sys.stderr)
                    elif k not in DEFAULTS:
                        print(f'coordinator: {self.file}: unknown key "{k}" ignored; known keys: '
                              f'{", ".join(DEFAULTS)}', file=sys.stderr)
                self.update({k: v for k, v in section.items() if k in DEFAULTS})
            except (ValueError, OSError, AttributeError) as ex:
                print(f'coordinator: ignoring {self.file}: {ex}', file=sys.stderr)
        for k, v in (flags or {}).items():
            if v not in (None, ''):
                self[k] = v
        for k in INTS:
            self[k] = int(self[k])
        for k in FLOATS + FLOATS_EXTRA:
            self[k] = float(self[k])
        if self['backend'] not in BACKENDS:
            raise SystemExit(f"coordinator: backend must be {', '.join(BACKENDS[:-1])} or manual, "
                             f"not {self['backend']!r}")

    @property
    def backend(self):
        """The backend in effect: `auto` resolves to the terminal this session runs in (see detect_backend)."""
        return detect_backend() if self['backend'] == 'auto' else self['backend']

    def path(self, key):
        p = Path(self[key])
        return p if p.is_absolute() else self.root / p

    def repos(self):
        return [str((self.root / r).resolve()) if not Path(r).is_absolute() else r
                for r in str(self['repos']).split(',') if r.strip()]

    def save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding='utf-8'))
            except ValueError:
                data = {}
        data['coordinator'] = {k: self[k] for k in DEFAULTS}
        self.file.write_text(json.dumps(data, indent=1), encoding='utf-8')


# ---- state: lanes.json and questions.jsonl live beside the cards

def lanes_file(cfg):
    return cfg.path('cardsDir') / 'lanes.json'


def load_lanes(cfg):
    try:
        return json.loads(lanes_file(cfg).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_lanes(cfg, lanes):
    p = lanes_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(lanes, indent=1, sort_keys=True), encoding='utf-8')
    os.replace(tmp, p)


def questions_file(cfg):
    return cfg.path('reportsDir') / 'questions.jsonl'


def load_questions(cfg):
    out = {}
    try:
        for line in questions_file(cfg).read_text(encoding='utf-8').splitlines():
            if line.strip():
                q = json.loads(line)
                out.setdefault(q['id'], {}).update(q)
    except OSError:
        pass
    return out


def append_question(cfg, rec):
    p = questions_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


# ---- the bus and the tracker, when their plugins are installed

def _version_key(path):
    """Sort key for a cached plugin's version folder: numeric parts compare as numbers."""
    return [int(p) if p.isdigit() else -1 for p in re.split(r'[.\-+]', path.parent.parent.name)]


def bus_cli(here=None):
    """The agent-bus CLI as an argv prefix, or None.

    Found via AGENT_BUS_CLI (set by the agent-bus hook inside a session), else a sibling plugin folder.
    Siblings sit side by side in a checkout (<repo>/agent-bus) but in versioned folders in Claude Code's
    plugin cache (<cache>/<marketplace>/agent-bus/<version>), so a plain shell needs both layouts.
    """
    plugin = (here or HERE).parent  # .../coordinator or .../coordinator/<version>
    cands = [os.environ.get('AGENT_BUS_CLI', ''), str(plugin.parent / 'agent-bus' / 'scripts' / 'bus.py')]
    cached = sorted(plugin.parent.parent.glob('agent-bus/*/scripts/bus.py'), key=_version_key, reverse=True)
    cands += [str(c) for c in cached]
    for c in cands:
        if c and Path(c).is_file():
            return [sys.executable, c]
    return None


def bus(cfg, *args, check=False):
    cli = bus_cli()
    if not cli or not cfg.path('busDir').is_dir():
        return None
    r = subprocess.run(cli + ['--dir', str(cfg.path('busDir'))] + list(args), capture_output=True, **TEXT,
                       cwd=cfg.root)
    if check and r.returncode != 0:
        print(f'coordinator: bus {" ".join(args)}: {r.stderr.strip()}', file=sys.stderr)
    return r


def tracker_tasks(cfg, role):
    """In-progress tasks for a role from a task-tracker db, read-only; [] when there is none."""
    db = cfg.path('trackerDb')
    if not db.exists():
        return []
    import sqlite3
    try:
        con = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True, timeout=2)
        table = next((t for t in ('issues', 'tasks')
                      if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()), None)
        if table is None:
            return []
        cols = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
        who = 'assignee' if 'assignee' in cols else ('owner' if 'owner' in cols else None)
        if not who:
            return []
        rows = con.execute(f"SELECT id, title, status FROM {table} WHERE {who} = ? AND status IN "
                           "('in_progress', 'in_review', 'blocked') ORDER BY id", (role,)).fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        return []


# ---- terminals

def shell_kind(cfg):
    if cfg['shell'] != 'auto':
        return cfg['shell']
    return 'powershell' if os.name == 'nt' else 'posix'


def lane_key(cfg, role):
    """COORD_LANE, which marks a worker's environment as this project's lane `role` (reaping matches on it)."""
    return f'{cfg.root.resolve().as_posix()}#{role}'


def with_env(cfg, role, cmd):
    kind = shell_kind(cfg)
    key = lane_key(cfg, role)
    if kind == 'powershell':
        return f"$env:COORD_LANE='{key}'; $env:AGENT_BUS_ROLE='{role}'; {cmd}"
    if kind == 'cmd':
        return f'set COORD_LANE={key}&& set AGENT_BUS_ROLE={role}&& {cmd}'
    return f'COORD_LANE={shlex.quote(key)} AGENT_BUS_ROLE={shlex.quote(role)} {cmd}'


def worker_command(cfg, role, brief, model=None):
    vals = {'role': role, 'brief': brief, 'model': model or cfg['workerModel'], 'repo': cfg.repos()[0]}
    vals['prompt'] = cfg['workerPrompt'].format(**vals)
    return cfg['workerCommand'].format(**vals)


# ---- herdr sessions. `herdr <cmd>` talks to the session named by HERDR_SOCKET_PATH / HERDR_SESSION, and to the
# `default` session when neither is set (a shell without them, a daemon, an agent run through a wrapper). Pane ids
# (w1:p3) repeat across sessions, so a lane belongs to the session it was opened in, and every herdr call is pinned.

HERDR_PIN = {'session': None}


def herdr_env_session(env=None):
    """The herdr session this process runs in, from its environment; None when it cannot tell."""
    env = os.environ if env is None else env
    if env.get('HERDR_SESSION'):
        return env['HERDR_SESSION']
    sock = env.get('HERDR_SOCKET_PATH')
    if sock:
        d = Path(sock).parent
        return d.name if d.parent.name == 'sessions' else 'default'
    return None


def herdr_session():
    """The session herdr calls are pinned to: an explicit pin (a lane's own session), else this process's."""
    return HERDR_PIN['session'] or herdr_env_session()


def with_herdr_session(argv):
    """`herdr --session NAME <cmd>` when the session is known (calls that name one already are left alone)."""
    if argv and argv[0] == 'herdr' and '--session' not in argv:
        name = herdr_session()
        if name:
            return [argv[0], '--session', name, *argv[1:]]
    return argv


def run(argv, dry=False, **kw):
    argv = with_herdr_session(argv)
    if dry:
        print('would run: ' + ' '.join(shlex.quote(a) for a in argv))
        return subprocess.CompletedProcess(argv, 0, '{}', '')
    return subprocess.run(argv, capture_output=True, **TEXT, **kw)


def orca_handle(stdout):
    """The terminal handle in `orca terminal create --json` output, or None.

    orca prints {"ok":true,"result":{"terminal":{"handle":"term_..."}}}; result.handle and handle are accepted too.
    """
    try:
        res = json.loads(stdout or '{}')
    except ValueError:
        return None
    if not isinstance(res, dict):
        return None
    result = res.get('result') if isinstance(res.get('result'), dict) else {}
    term = result.get('terminal') if isinstance(result.get('terminal'), dict) else {}
    return term.get('handle') or result.get('handle') or res.get('handle') or None


def orca_titled(repo, title, dry=False):
    """Handles of the orca terminals titled `title` in `repo`."""
    r = run(['orca', 'terminal', 'list', '--worktree', f'path:{repo}', '--json'], dry)
    try:
        res = json.loads(r.stdout or '{}')
        terms = (res.get('result') or {}).get('terminals') or res.get('terminals') or []
    except (ValueError, AttributeError):
        terms = []
    return {t['handle'] for t in terms if isinstance(t, dict) and t.get('title') == title and t.get('handle')}


def orca_close_by_title(repo, title, before=frozenset(), dry=False):
    """Close the orca terminals titled `title` in `repo` that were not in `before`: the tab of a create whose
    handle could not be read. Tabs that already had the title are left alone."""
    closed = 0
    for h in sorted(orca_titled(repo, title, dry) - set(before)):
        if run(['orca', 'terminal', 'close', '--terminal', h, '--tab'], dry).returncode == 0:
            closed += 1
    return closed


# ---- herdr: workspaces hold tabs, a tab holds panes. A lane is one tab in the workspace whose cwd is its repo; its
# handle is the tab's root pane id (w1:p3). Pane ids are stable while the herdr server runs but may be renumbered when
# it restarts, so the lane also keeps the pane's terminal_id, and every lookup tries that first.

HERDR_OPENED = {}  # pane id -> {'tab', 'terminal'} for the panes open_terminal created in this process


def herdr_json(argv, dry=False):
    """(CompletedProcess, result dict) for `herdr <argv>`; the result is {} when the output is not herdr's JSON."""
    r = run(['herdr', *argv], dry)
    try:
        res = json.loads(r.stdout or '{}')
    except ValueError:
        return r, {}
    return r, (res.get('result') if isinstance(res, dict) and isinstance(res.get('result'), dict) else {})


def herdr_panes(dry=False):
    """Every pane of the running herdr server as dicts, or None when herdr can't be asked."""
    if dry or not shutil.which('herdr'):
        return None
    r, res = herdr_json(['pane', 'list'])
    if r.returncode != 0 or not isinstance(res.get('panes'), list):
        return None
    return [p for p in res['panes'] if isinstance(p, dict)]


def same_path(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (TypeError, ValueError, OSError):
        return a == b


def herdr_session_names():
    """Names of the running herdr sessions (`herdr session list`), or None when they cannot be listed."""
    if not shutil.which('herdr'):
        return None
    try:
        r = subprocess.run(['herdr', 'session', 'list'], capture_output=True, **TEXT)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return [ln.split()[0] for ln in r.stdout.splitlines()[1:] if len(ln.split()) > 1 and ln.split()[1] == 'running']


def require_herdr_session(dry=False):
    """Never open a tab in a guessed session: with no session known and several running, `herdr` would use `default`."""
    if dry or herdr_session():
        return
    names = herdr_session_names() or []
    if len(names) > 1:
        raise SystemExit('coordinator: cannot tell which herdr session this is (no HERDR_SESSION or HERDR_SOCKET_PATH) and '
                         f'{len(names)} run ({", ".join(names)}); pass --herdr-session NAME or set HERDR_SESSION')


def herdr_workspace(repo, dry=False):
    """The herdr workspace whose pane runs in `repo`, else a new one labelled after the repo (not focused)."""
    for key in ('cwd', 'foreground_cwd'):
        for p in herdr_panes(dry) or []:
            if p.get(key) and p.get('workspace_id') and same_path(p[key], repo):
                return p['workspace_id']
    r, res = herdr_json(['workspace', 'create', '--cwd', repo, '--label', Path(repo).name or repo, '--no-focus'], dry)
    ws = (res.get('workspace') or {}).get('workspace_id')
    if not ws and not dry:
        raise SystemExit(f'coordinator: herdr workspace create failed: {r.stderr.strip() or r.stdout.strip()}')
    return ws or '<workspace>'


def herdr_tabs_labelled(ws, label):
    """Tab ids labelled `label` in workspace `ws`."""
    r, res = herdr_json(['tab', 'list', '--workspace', ws])
    return {t['tab_id'] for t in res.get('tabs') or [] if isinstance(t, dict) and t.get('label') == label
            and t.get('tab_id')}


def herdr_close_by_label(ws, label, before=frozenset()):
    """Close the tabs labelled `label` in `ws` that were not in `before`: a create whose output could not be read."""
    closed = 0
    for t in sorted(herdr_tabs_labelled(ws, label) - set(before)):
        if run(['herdr', 'tab', 'close', t]).returncode == 0:
            closed += 1
    return closed


def herdr_open_tab(ws, repo, label, env=(), dry=False):
    """A new tab in `ws`: returns its root pane dict (pane_id, tab_id, terminal_id), or raises SystemExit.
    A tab whose create output could not be read is found by its label and closed, so none is left behind."""
    before = set() if dry else herdr_tabs_labelled(ws, label)
    args = ['tab', 'create', '--workspace', ws, '--cwd', repo, '--label', label]
    for e in env:
        args += ['--env', e]
    r, res = herdr_json(args + ['--no-focus'], dry)
    pane = dict(res.get('root_pane') or {})
    pane.setdefault('tab_id', (res.get('tab') or {}).get('tab_id'))
    if not pane.get('pane_id') and not dry:
        if r.returncode == 0:
            herdr_close_by_label(ws, label, before)
        raise SystemExit(f'coordinator: herdr tab create failed: {r.stderr.strip() or r.stdout.strip()}')
    return pane


def herdr_live_handles(dry=False):
    """Pane ids and terminal ids of every live herdr pane, or None when herdr can't be asked."""
    panes = herdr_panes(dry)
    if panes is None:
        return None
    return {p[k] for p in panes for k in ('pane_id', 'terminal_id') if p.get(k)}


def herdr_locate(lane, dry=False):
    """The live pane of a herdr lane, looked up in the lane's own session."""
    old = HERDR_PIN['session']
    HERDR_PIN['session'] = lane.get('session') or old
    try:
        return herdr_locate_pinned(lane, dry)
    finally:
        HERDR_PIN['session'] = old


def herdr_locate_pinned(lane, dry=False):
    """The live pane of a herdr lane: by its terminal id first, then its pane id. {} when it is gone, None when
    herdr can't be asked."""
    panes = herdr_panes(dry)
    if panes is None:
        return None
    term, h = lane.get('terminal'), lane.get('handle')
    for p in panes:
        if term and p.get('terminal_id') == term:
            return p
    p = next((p for p in panes if h and p.get('pane_id') == h), None)
    if not p:
        return {}
    if lane.get('role'):
        # the pane id may be another pane's after a restart: take it only when its tab still carries the lane's name
        _, res = herdr_json(['tab', 'get', p.get('tab_id') or ''])
        if (res.get('tab') or {}).get('label') != lane.get('role'):
            return {}
    return p


def herdr_screen(h, dry=False):
    """The visible screen of herdr pane `h` as text."""
    out = run(['herdr', 'pane', 'read', h, '--source', 'visible'], dry).stdout or ''
    save_debug(out, out)
    return out


def open_terminal(cfg, role, repo, cmd, dry=False):
    """Start `cmd` for `role` in a new terminal; returns (handle, how). The worker's env carries AGENT_BUS_ROLE."""
    b = cfg.backend
    if b == 'herdr':
        # a tab in the repo's workspace; its env carries the lane marker, so no prefix is typed before the command
        require_herdr_session(dry)
        ws = herdr_workspace(repo, dry)
        pane = herdr_open_tab(ws, repo, role, [f'AGENT_BUS_ROLE={role}', f'COORD_LANE={lane_key(cfg, role)}'], dry)
        handle = pane.get('pane_id')
        s = run(['herdr', 'pane', 'run', handle or '<pane>', cmd], dry)
        if s.returncode != 0 and not dry:
            run(['herdr', 'tab', 'close', pane.get('tab_id') or handle])
            raise SystemExit(f'coordinator: herdr pane run failed: {s.stderr.strip() or s.stdout.strip()}')
        if handle:
            HERDR_OPENED[handle] = {'tab': pane.get('tab_id'), 'terminal': pane.get('terminal_id')}
        return handle, 'herdr'
    if b == 'orca':
        # Open a plain shell, then type the worker command into it. `create --command <interactive TUI>` makes
        # orca time out waiting for the handle ("Timed out waiting for terminal handle after creation") once
        # the TUI owns the terminal, and no tab is left to adopt; the usage probe already works this way.
        before = set() if dry else orca_titled(repo, role)
        r = run(['orca', 'terminal', 'create', '--worktree', f'path:{repo}', '--title', role, '--json'], dry)
        handle = orca_handle(r.stdout)
        if not handle and not dry:
            if r.returncode == 0:
                orca_close_by_title(repo, role, before)  # the tab may exist: never leave an orphan
            raise SystemExit(f'coordinator: orca terminal create failed: {r.stderr.strip() or r.stdout.strip()}')
        s = run(['orca', 'terminal', 'send', '--terminal', handle or '<handle>', '--text', with_env(cfg, role, cmd),
                 '--enter', '--json'], dry)
        if s.returncode != 0 and not dry:
            run(['orca', 'terminal', 'close', '--terminal', handle, '--tab'])
            raise SystemExit(f'coordinator: orca terminal send failed: {s.stderr.strip() or s.stdout.strip()}')
        return handle, 'orca'
    if b == 'tmux':
        target = []
        if not os.environ.get('TMUX'):
            has = run(['tmux', 'has-session', '-t', 'coord'], dry)
            if has.returncode != 0:
                run(['tmux', 'new-session', '-d', '-s', 'coord', '-n', 'coord'], dry)
            target = ['-t', 'coord']
        r = run(['tmux', 'new-window', *target, '-P', '-F', '#{window_id}', '-n', role, '-c', repo,
                 '-e', f'AGENT_BUS_ROLE={role}', '-e', f'COORD_LANE={lane_key(cfg, role)}', cmd], dry)
        return (r.stdout or '').strip() or None, 'tmux'
    if b == 'wt':
        if os.name == 'nt':
            shell = ['powershell', '-NoExit', '-Command', with_env(cfg, role, cmd)]
        else:
            shell = ['sh', '-c', with_env(cfg, role, cmd)]
        run(['wt', '-w', '0', 'nt', '--title', role, '-d', repo, *shell], dry)
        return None, 'wt'
    print(f'Start {role} in a new terminal (cwd {repo}):\n  {with_env(cfg, role, cmd)}')
    return None, 'manual'


def close_terminal(cfg, lane, dry=False):
    """Close the lane's terminal (in the herdr session it was opened in), then reap any tmux session still running it."""
    old = HERDR_PIN['session']
    HERDR_PIN['session'] = lane.get('session') or old
    try:
        return close_terminal_pinned(cfg, lane, dry)
    finally:
        HERDR_PIN['session'] = old


def close_terminal_pinned(cfg, lane, dry=False):
    h, how = lane.get('handle'), lane.get('backend')
    ok = False
    if how == 'orca' and h:
        ok = run(['orca', 'terminal', 'close', '--terminal', h, '--tab'], dry).returncode == 0
    elif how == 'herdr' and h:
        if dry:
            ok = run(['herdr', 'tab', 'close', lane.get('tab') or h], dry).returncode == 0
        else:
            pane = herdr_locate(lane)
            if pane is None:
                print(f'{lane.get("role")}: herdr is not reachable; close its tab yourself.')
            elif not pane:
                ok = True  # the tab is already gone
            else:
                ok = run(['herdr', 'tab', 'close', pane.get('tab_id') or pane['pane_id']]).returncode == 0
    elif how == 'tmux' and h:
        ok = run(['tmux', 'kill-window', '-t', h], dry).returncode == 0
    else:
        print(f'Close the terminal of {lane.get("role")} yourself ({how}: no handle to close).')
    reaped, alive = reap_lane(cfg, lane, dry)
    if reaped:
        print(f'{lane.get("role")}: killed tmux session(s) {", ".join(reaped)}')
    if alive:
        print(f'{lane.get("role")}: WARNING pid(s) {", ".join(map(str, alive))} still running after the close')
    return ok and not alive


# ---- tmux sessions that outlive their terminal
#
# A `claude` wrapper such as claude-auto-retry runs the agent inside its own tmux session and only attaches the
# terminal to it. Closing the terminal detaches the session; the agent keeps working beside its successor. The
# session's environment still names the lane: COORD_LANE (with_env or the herdr tab env sets it; older workers carry
# only AGENT_BUS_ROLE) and the terminal's handle: ORCA_TERMINAL_HANDLE, or HERDR_PANE_ID inside herdr.

def proc_table():
    """{pid: (ppid, start time)} for every process; the start time tells a pid from a later reuse of it."""
    out = {}
    if os.name == 'nt':
        return out
    r = run(['ps', '-axo', 'pid=,ppid=,lstart='])
    for ln in (r.stdout or '').splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), parts[2].strip())
    return out


def tmux_sessions(dry=False):
    """[{id, name, attached, role, lane, handle, pids, starts}] for every tmux session; pids are its pane processes
    and their descendants, starts their start times. Empty without tmux."""
    if dry or os.name == 'nt' or not shutil.which('tmux'):
        return []
    r = run(['tmux', 'list-sessions', '-F', '#{session_id}\t#{session_name}\t#{session_attached}'])
    if r.returncode != 0:
        return []
    procs = proc_table()
    children = {}
    for pid, (ppid, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    out = []
    for ln in (r.stdout or '').splitlines():
        sid, _, rest = ln.partition('\t')
        name, _, attached = rest.partition('\t')
        if not sid.startswith('$'):
            continue
        env = {}
        # targets are session ids ($N): a name would fall back to a prefix or pattern match once the session is gone
        for e in (run(['tmux', 'show-environment', '-t', sid]).stdout or '').splitlines():
            k, eq, v = e.partition('=')
            if eq:
                env[k] = v
        panes = run(['tmux', 'list-panes', '-s', '-t', sid, '-F', '#{pane_pid}']).stdout or ''
        pids, todo = [], [int(p) for p in panes.split() if p.isdigit()]
        while todo:
            p = todo.pop()
            pids.append(p)
            todo.extend(children.get(p, []))
        herdr = env.get('HERDR_ENV') == '1' and bool(env.get('HERDR_PANE_ID'))
        out.append({'id': sid, 'name': name, 'attached': attached.strip() not in ('', '0'),
                    'role': env.get('AGENT_BUS_ROLE'), 'lane': env.get('COORD_LANE'),
                    'handle': env.get('HERDR_PANE_ID') if herdr else env.get('ORCA_TERMINAL_HANDLE'),
                    'herdr': herdr, 'pids': pids,
                    'starts': {p: procs[p][1] for p in pids if p in procs}})
    return out


def own_pids():
    """This process and its ancestors: a session holding one of them is the caller's own and is never reaped."""
    procs = proc_table()
    pids, p = set(), os.getpid()
    while p and p not in pids:
        pids.add(p)
        p = procs.get(p, (None,))[0]
    return pids


def kill_sessions(sessions, dry=False, wait_s=5.0):
    """Kill these tmux sessions and make sure their processes exit: tmux hangs them up; any process still alive
    after `wait_s` gets SIGTERM, then SIGKILL. Only the processes listed for these sessions are signalled, and only
    while their start time is unchanged (never a reused pid). Returns (names killed, pids still alive)."""
    import signal
    names, starts = [], {}
    for s in sessions:
        if run(['tmux', 'kill-session', '-t', s.get('id') or s['name']], dry).returncode == 0:
            names.append(s['name'])
        starts.update(s.get('starts') or {})
    if dry:
        return names, []

    def alive():
        procs = proc_table()
        return [p for p, st in starts.items() if p in procs and procs[p][1] == st]
    left = alive()
    for sig in (None, signal.SIGTERM, signal.SIGKILL):
        if sig is not None:
            for p in left:
                try:
                    os.kill(p, sig)
                except OSError:
                    pass
        deadline = time.monotonic() + wait_s
        while left and time.monotonic() < deadline:
            time.sleep(0.25)
            left = alive()
        if not left:
            break
    return names, left


def lane_sessions(cfg, lane, sessions):
    """The sessions running this lane: its terminal handle, or its COORD_LANE (a session that has none, from an
    older worker, by its role). Any other match is an older generation's orphan. The caller's own session is
    excluded."""
    if not sessions:
        return []
    mine = own_pids()
    h, role = lane.get('handle'), lane.get('role')
    key = lane_key(cfg, role) if role else None

    def ours(s):
        # a herdr pane id can name another pane after a herdr restart, so it never matches alone: herdr lanes
        # always carry COORD_LANE, which the next line checks
        if h and s['handle'] == h and not s.get('herdr'):
            return True
        return bool(role) and (s['lane'] == key if s.get('lane') else s['role'] == role)
    return [s for s in sessions if not mine & set(s['pids']) and ours(s)]


def reap_lane(cfg, lane, dry=False):
    found = lane_sessions(cfg, lane, tmux_sessions(dry))
    return kill_sessions(found, dry) if found else ([], [])


def orca_live_handles(dry=False):
    """Handles of every live orca terminal, or None when orca can't be asked."""
    if dry or not shutil.which('orca'):
        return None
    r = run(['orca', 'terminal', 'list', '--limit', '1000', '--json'])
    try:
        res = json.loads(r.stdout or '{}')
        terms = (res.get('result') or {}).get('terminals') or res.get('terminals') or []
    except (ValueError, AttributeError):
        return None
    if r.returncode != 0:
        return None
    return {t['handle'] for t in terms if isinstance(t, dict) and t.get('handle')}


def live_handles(lanes, dry=False):
    """Handles of every live terminal of the backends these lanes use (orca handles; herdr pane and terminal ids),
    or None when no lane has one or a backend can't be asked: liveness is then unknown, so nothing is reconciled."""
    used = {l.get('backend') for l in lanes.values() if l.get('handle')} & {'orca', 'herdr'}
    if not used:
        return None
    out = set()
    for b, ask in (('orca', orca_live_handles), ('herdr', herdr_live_handles)):
        if b in used:
            got = ask(dry)
            if got is None:
                return None
            out |= got
    return out


def lane_alive(lane, handles):
    """Is the lane's terminal among the live handles (a herdr lane: its terminal id or its pane id)?"""
    return bool(handles) and (lane.get('handle') in handles or (bool(lane.get('terminal'))
                                                              and lane['terminal'] in handles))


def session_lane(cfg, s, lanes):
    """The role of the lane in this project that session `s` runs, or None."""
    if s.get('lane'):
        root, _, role = s['lane'].rpartition('#')
        return role if root == lane_key(cfg, '').rstrip('#') and role in lanes else None
    return s['role'] if s['role'] in lanes else None


def orphans(cfg, lanes, sessions=None, handles=None):
    """(orphans, detached). An orphan provably runs a lane that should not be running: the lane is closed or paused,
    or a successor's terminal is live under another handle. `reap` kills orphans. A live lane whose terminal is
    gone is only detached: its agent may still be working, so it is listed for a person to reattach or stop
    (`reap <role>`), never reaped by a bare `reap`. Coordinator lanes are neither."""
    sessions = tmux_sessions() if sessions is None else sessions
    out, detached = [], []
    for s in sessions:
        role = session_lane(cfg, s, lanes)
        lane = lanes.get(role) if role else None
        if lane is None or is_coordinator(role, lane):
            continue
        if lane.get('state') not in ('live', 'retiring'):
            out.append(s)
        elif s['handle'] and s['handle'] != lane.get('handle') and handles is not None \
                and lane_alive(lane, handles):
            out.append(s)  # superseded: the live lane's own terminal is up
        elif not s['attached'] and handles is not None and s['handle'] and s['handle'] not in handles:
            detached.append(s)
    return out, detached


# ---- verbs

def render(text, cfg, role='<role>'):
    vals = {
        'role': role, 'rules': rel(cfg, cfg.path('rulesFile')), 'cards': rel(cfg, cfg.path('cardsDir')),
        'briefs': rel(cfg, cfg.path('briefsDir')), 'reports': rel(cfg, cfg.path('reportsDir')),
        'retire_tokens': f"{cfg['retireTokens'] // 1000}k", 'pause_percent': cfg['pauseSessionPercent'],
        'pause_weekly': cfg['pauseWeeklyPercent'],
        'coord': 'coord',  # the session context names the real command; plugin paths change between versions
    }
    for k, v in vals.items():
        text = text.replace('{{' + k + '}}', str(v))
    return text


def rel(cfg, p):
    try:
        return Path(p).resolve().relative_to(cfg.root.resolve()).as_posix()
    except ValueError:
        return Path(p).as_posix()


def cmd_init(cfg, args):
    force = '--force' in args
    roles = take(args, '--roles') or 'coordinator,scheduler'
    made = []
    targets = {
        TEMPLATES / 'RULES_CORE.md': cfg.path('rulesFile'),
        TEMPLATES / 'lanes' / 'README.md': cfg.path('cardsDir') / 'README.md',
        TEMPLATES / 'briefs' / 'TEMPLATE.md': cfg.path('briefsDir') / 'TEMPLATE.md',
        TEMPLATES / 'briefs' / 'RESUME.md': cfg.path('briefsDir') / 'RESUME.md',
        TEMPLATES / 'briefs' / 'scheduler.md': cfg.path('briefsDir') / 'scheduler.md',
        TEMPLATES / 'briefs' / 'COORDINATOR_RESUME.md': cfg.path('briefsDir') / 'COORDINATOR_RESUME.md',
        TEMPLATES / 'HANDOFF_PROTOCOL.md': cfg.path('briefsDir') / 'HANDOFF_PROTOCOL.md',
    }
    for src, dst in targets.items():
        if dst.exists() and not force:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(render(src.read_text(encoding='utf-8'), cfg), encoding='utf-8')
        made.append(rel(cfg, dst))
    cfg.path('reportsDir').mkdir(parents=True, exist_ok=True)
    questions_file(cfg).touch()
    if not lanes_file(cfg).exists():
        save_lanes(cfg, {})
    cfg.save()
    made.append(rel(cfg, cfg.file) + ' (config)')
    cli = bus_cli()
    if cli:
        r = subprocess.run(cli + ['--dir', str(cfg.path('busDir')), 'init', '--roles', roles], capture_output=True,
                           **TEXT, cwd=cfg.root)
        made.append('bus: ' + (r.stdout.strip() or r.stderr.strip()))
    else:
        made.append('bus: agent-bus plugin not found; messages fall back to files in ' + rel(cfg, cfg.path('reportsDir')))
    print('coordinator init:\n  ' + '\n  '.join(made))
    print('Next: edit section 1 of the rules file (the project), then `spawn <role> <brief>`.')
    return 0


def is_coordinator(role, lane):
    """The coordinator's own lane (role `coordinator` or kind `coordinator`) never counts toward maxLanes, so a
    coordinator can always start its successor."""
    return role == 'coordinator' or lane.get('kind') == 'coordinator'


def cmd_spawn(cfg, args):
    dry = '--dry-run' in args
    repo = take(args, '--repo') or cfg.repos()[0]
    model = take(args, '--model')
    pos = [a for a in args if not a.startswith('--')]
    if not pos:
        raise SystemExit('usage: spawn <role> [<brief>] [--repo P] [--model M]')
    role = pos[0]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,40}', role):
        raise SystemExit(f'coordinator: bad role name {role!r} (letters, digits, - _ .)')
    brief = pos[1] if len(pos) > 1 else rel(cfg, cfg.path('briefsDir') / 'RESUME.md')
    if not (cfg.root / brief).exists() and not Path(brief).exists():
        raise SystemExit(f'coordinator: no brief at {brief}')
    lanes = load_lanes(cfg)
    coordinator = is_coordinator(role, lanes.get(role, {})) or '--coordinator' in args
    live = [r for r, l in lanes.items() if l.get('state') == 'live' and r != role and not is_coordinator(r, l)]
    if not coordinator and len(live) >= cfg['maxLanes']:
        raise SystemExit(f"coordinator: {len(live)} lanes live (max {cfg['maxLanes']}): retire one first "
                         f"({', '.join(sorted(live))})")
    if lanes.get(role, {}).get('state') == 'live':
        raise SystemExit(f'coordinator: {role} is already live; use respawn')
    if not dry:
        # a session left over from an earlier generation of this role would work beside the new one
        reaped, alive = reap_lane(cfg, {'role': role})
        if reaped:
            print(f'{role}: killed leftover tmux session(s) {", ".join(reaped)}')
        if alive:
            raise SystemExit(f'coordinator: an earlier {role} is still running (pids {alive}); stop it first')
    cmd = worker_command(cfg, role, brief, model)
    handle, how = open_terminal(cfg, role, repo, cmd, dry)
    if dry:
        return 0
    if handle and how in ('orca', 'herdr'):
        bus(cfg, 'register', role, '--handle', handle, '--kind', 'claude', '--mode', 'file')
    elif bus_cli() and cfg.path('busDir').is_dir():
        bus(cfg, 'register', role, '--kind', 'claude', '--mode', 'file')
    gen = lanes.get(role, {}).get('generation', 0) + 1
    lanes[role] = {'role': role, 'brief': brief, 'repo': repo, 'backend': how, 'handle': handle, 'state': 'live',
                   'started': now(), 'generation': gen, 'model': model or cfg['workerModel']}
    if how == 'herdr' and herdr_session():
        lanes[role]['session'] = herdr_session()
    if how == 'herdr' and handle in HERDR_OPENED:
        lanes[role].update({k: v for k, v in HERDR_OPENED[handle].items() if v})
    if coordinator:
        lanes[role]['kind'] = 'coordinator'
    save_lanes(cfg, lanes)
    print(f'{role}: started (generation {gen}, {how}{" " + handle if handle else ""}) on {brief}')
    return 0


def cmd_retire(cfg, args):
    pos = [a for a in args if not a.startswith('--')]
    if not pos:
        raise SystemExit('usage: retire <role> [--close]')
    role = pos[0]
    lanes = load_lanes(cfg)
    lane = lanes.get(role)
    if not lane:
        raise SystemExit(f'coordinator: no lane {role}')
    card = rel(cfg, cfg.path('cardsDir') / f'{role}.md')
    text = (f'Retire now: update your lane card {card} (state, next 3 actions, anchors, open messages) so a successor '
            'can resume from it alone, finish or park the current step, then end with SAFE TO CLOSE.')
    r = bus(cfg, 'send', role, text, '--prio', 'urgent', '--from', 'coordinator', '--topic', 'retire')
    if r is None:
        print(f'(no bus) tell {role}: {text}')
    if '--close' in args:
        close_terminal(cfg, lane, '--dry-run' in args)
        lane['state'] = 'closed'
    else:
        lane['state'] = 'retiring'
    lane['retired'] = now()
    save_lanes(cfg, lanes)
    print(f'{role}: {lane["state"]}')
    return 0


PREDECESSOR = 'coordinator-prev'


def own_terminal():
    """(backend, handle) of the terminal this session runs in, from its environment."""
    how = detect_backend()
    handle = {'herdr': os.environ.get('HERDR_PANE_ID'), 'orca': os.environ.get('ORCA_TERMINAL_HANDLE')}.get(how)
    return how, handle


def cmd_succeed(cfg, args):
    """The coordinator hands over: its own lane becomes `coordinator-prev` (retiring), and a fresh `coordinator`
    session starts on COORDINATOR_RESUME.md, which reads the coordinator card and closes the old terminal once the old
    one has said SAFE TO CLOSE (`handed-over`)."""
    dry = '--dry-run' in args
    model = take(args, '--model')
    card = cfg.path('cardsDir') / 'coordinator.md'
    if not card.exists() and not dry:
        raise SystemExit(f'coordinator: write your card {rel(cfg, card)} first (lanes, open questions, decisions '
                         'pending, next 3 actions); the successor starts from it')
    brief = cfg.path('briefsDir') / 'COORDINATOR_RESUME.md'
    if not brief.exists() and not dry:
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text(render((TEMPLATES / 'briefs' / 'COORDINATOR_RESUME.md').read_text(encoding='utf-8'), cfg),
                         encoding='utf-8')
    lanes = load_lanes(cfg)
    if lanes.get(PREDECESSOR, {}).get('state') in ('live', 'retiring'):
        raise SystemExit(f'coordinator: an earlier handover is unfinished ({PREDECESSOR} is still {lanes[PREDECESSOR]["state"]}); '
                         'run close-predecessor from the current coordinator, or retire that lane first')
    old = dict(lanes.get('coordinator') or {})
    how, handle = own_terminal()
    if handle:  # what this session knows about its own terminal beats a stale record
        old.update({'backend': how, 'handle': handle})
    if how == 'herdr':
        # the successor opens beside the old coordinator: in the session this one runs in, never a guessed one
        mine = herdr_env_session() or old.get('session')
        if mine:
            old['session'] = mine
            HERDR_PIN['session'] = HERDR_PIN['session'] or mine
    old.update({'role': PREDECESSOR, 'kind': 'coordinator', 'state': 'retiring', 'retired': now()})
    lanes.pop('coordinator', None)
    lanes[PREDECESSOR] = old
    if not dry:
        save_lanes(cfg, lanes)
    code = cmd_spawn(cfg, ['coordinator', rel(cfg, brief), '--coordinator'] + (['--model', model] if model else [])
                     + (['--dry-run'] if dry else []))
    if code == 0 and not dry:
        print('Handed over. Now: finish your turn by running `handed-over` and then ending with SAFE TO CLOSE. '
              'The successor closes this terminal when it sees that.')
    return code


def cmd_handed_over(cfg, args):
    """(the old coordinator) Record the last word SAFE TO CLOSE for the predecessor lane."""
    lanes = load_lanes(cfg)
    if PREDECESSOR not in lanes:
        raise SystemExit('coordinator: no handover is running; run `succeed` first')
    p = cfg.path('reportsDir') / f'{PREDECESSOR}_progress.md'
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as f:
        f.write(f'\n[{now()}] handover done, card current.\nSAFE TO CLOSE\n')
    print('recorded. End your turn with the words SAFE TO CLOSE.')
    return 0


def cmd_close_predecessor(cfg, args):
    """(the successor) Close the old coordinator's terminal, but only once its last word is SAFE TO CLOSE."""
    wait = int(take(args, '--wait') or 0)
    lanes = load_lanes(cfg)
    lane = lanes.get(PREDECESSOR)
    if not lane or lane.get('state') == 'closed':
        print('no predecessor to close')
        return 0
    if lane.get('handle') and lane.get('handle') == own_terminal()[1]:
        raise SystemExit('coordinator: that terminal is this session; the predecessor is closed from the successor')
    deadline = time.time() + wait
    while True:
        word = last_word(cfg, PREDECESSOR)
        if word == 'safe' or time.time() >= deadline:
            break
        time.sleep(min(5, max(0.1, deadline - time.time())))
    if word != 'safe':
        print(f'predecessor not closed: its last word is {"KEEP OPEN" if word == "keep" else "missing"}, not SAFE TO CLOSE. '
              'Message it on the bus (role coordinator-prev) or ask the user; run this again afterwards.')
        return 1
    if not close_terminal(cfg, lane):
        print(f'{PREDECESSOR}: close attempted but it may still be running; close its terminal yourself')
    lane['state'] = 'closed'
    save_lanes(cfg, lanes)
    print(f'{PREDECESSOR}: closed')
    return 0


def cmd_respawn(cfg, args):
    dry = '--dry-run' in args
    pos = [a for a in args if not a.startswith('--')]
    if not pos:
        raise SystemExit('usage: respawn <role>')
    role = pos[0]
    lanes = load_lanes(cfg)
    lane = lanes.get(role)
    if lane and lane.get('state') not in ('closed', 'paused'):
        if not close_terminal(cfg, lane, dry) and not dry and reap_lane(cfg, lane)[1]:
            raise SystemExit(f'coordinator: {role} is still running after the close; not starting a successor')
        if not dry:
            lane['state'] = 'closed'
            save_lanes(cfg, lanes)
    resume = rel(cfg, cfg.path('briefsDir') / 'RESUME.md')
    return cmd_spawn(cfg, [role, resume] + (['--repo', lane['repo']] if lane and lane.get('repo') else [])
                     + (['--dry-run'] if dry else []))


def card_info(cfg, role):
    p = cfg.path('cardsDir') / f'{role}.md'
    if not p.exists():
        return {'card': None}
    text = p.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()
    state = []
    grab = False
    for ln in lines:
        if ln.startswith('## '):
            grab = ln.lower().startswith('## state')
            continue
        if grab and ln.strip():
            state.append(ln.strip())
    tail = '\n'.join(lines[-15:])
    safe = re.search(r'SAFE TO CLOSE', tail) is not None and re.search(r'KEEP OPEN', tail) is None
    age = int((time.time() - p.stat().st_mtime) / 60)
    return {'card': rel(cfg, p), 'lines': len(lines), 'age_min': age, 'cardState': state[:5], 'safe': safe}


def last_word(cfg, role):
    """SAFE TO CLOSE / KEEP OPEN from the role's progress log or card, whichever says it last."""
    for p in (cfg.path('reportsDir') / f'{role}_progress.md', cfg.path('cardsDir') / f'{role}.md'):
        try:
            tail = p.read_text(encoding='utf-8', errors='replace')[-3000:]
        except OSError:
            continue
        i, j = tail.rfind('SAFE TO CLOSE'), tail.rfind('KEEP OPEN')
        if i > j:
            return 'safe'
        if j > i:
            return 'keep'
    return None


def cmd_lanes(cfg, args):
    lanes = load_lanes(cfg)
    rows = []
    for role, lane in sorted(lanes.items()):
        info = card_info(cfg, role)
        rows.append({**lane, **info, 'tasks': tracker_tasks(cfg, role), 'last_word': last_word(cfg, role)})
    if '--json' in args:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print('no lanes yet: `spawn <role> <brief>`')
        return 0
    for r in rows:
        card = f"card {r['lines']}l {r['age_min']}m ago" if r.get('card') else 'NO CARD'
        tasks = ', '.join(f'#{t[0]} {t[2]}' for t in r['tasks']) or '-'
        word = {'safe': ' SAFE TO CLOSE', 'keep': ' keep open'}.get(r['last_word'], '')
        print(f"{r['role']:<16} {r['state']:<8} {r['backend']:<6} gen {r.get('generation', 1)}  {card}  tasks: {tasks}{word}")
    return 0


def reconcile(cfg, lanes, sessions, handles):
    """Mark closed every live orca or herdr lane whose terminal is gone and that no tmux session still runs; returns
    them."""
    if handles is None:
        return []
    gone = []
    for role, lane in lanes.items():
        if (lane.get('state') in ('live', 'retiring') and lane.get('backend') in ('orca', 'herdr')
                and lane.get('handle') and not lane_alive(lane, handles) and not lane_sessions(cfg, lane, sessions)):
            lane['state'] = 'closed'
            lane['retired'] = now()
            gone.append(role)
    if gone:
        save_lanes(cfg, lanes)
    return gone


def cmd_status(cfg, args):
    lanes = load_lanes(cfg)
    sessions = tmux_sessions()
    handles = live_handles(lanes)
    gone = reconcile(cfg, lanes, sessions, handles)
    live = [r for r, l in lanes.items() if l.get('state') in ('live', 'retiring')]
    workers = [r for r in live if not is_coordinator(r, lanes[r])]
    print(f'# Status {now()}\n')
    print(f"## Lanes ({len(workers)} live of max {cfg['maxLanes']}"
          + (f", plus coordinator {', '.join(sorted(set(live) - set(workers)))}" if len(workers) < len(live) else '')
          + ')')
    if gone:
        print(f"- terminal gone, marked closed: {', '.join(sorted(gone))}")
    for role in sorted(live):
        info = card_info(cfg, role)
        print(f'- **{role}** ({lanes[role]["state"]})' + ('' if info.get('card') else ' -- no lane card yet'))
        for s in info.get('cardState', []):
            print(f'  {s}')
    qs = [q for q in load_questions(cfg).values() if not q.get('answer')]
    print(f'\n## Questions for you ({len(qs)})')
    for q in qs:
        opts = f" [{q['options']}]" if q.get('options') else ''
        print(f"- {q['id']} from {q.get('from', '?')}: {q['text']}{opts}")
    safe = [r for r in live if last_word(cfg, r) == 'safe']
    if safe:
        print(f"\n## Safe to close\n- {', '.join(sorted(safe))} (`close-idle`)")
    orph, detached = orphans(cfg, lanes, sessions, handles)
    if orph:
        print('\n## Orphan sessions (still running a retired or replaced lane; `reap` kills them)')
        for o in orph:
            print(f"- {o['name']} ({o['role']}, {'attached' if o['attached'] else 'detached'}, pids {o['pids'][:4]})")
    if detached:
        print('\n## Detached lanes (terminal gone, agent may still be working; never reaped by a bare `reap`)')
        for o in detached:
            print(f"- {o['role']}: reattach with `tmux attach -t '={o['name']}'`, or stop it with `reap {o['role']}`")
    r = bus(cfg, 'digest', '--as', 'coordinator')
    if r is not None and r.stdout.strip():
        print('\n## Coordinator inbox\n' + r.stdout.strip())
    return 0


def cmd_close_idle(cfg, args):
    dry = '--dry-run' in args
    lanes = load_lanes(cfg)
    closed = []
    for role, lane in sorted(lanes.items()):
        if (lane.get('state') in ('live', 'retiring') and not is_coordinator(role, lane)
                and last_word(cfg, role) == 'safe'):
            close_terminal(cfg, lane, dry)
            if not dry:
                lane['state'] = 'closed'
                lane['retired'] = now()
            closed.append(role)
    if not dry:
        save_lanes(cfg, lanes)
    print(('would close: ' if dry else 'closed: ') + (', '.join(closed) or 'none'))
    return 0


def cmd_reap(cfg, args):
    """Kill the orphan sessions `status` lists, or every session of the named lanes."""
    dry = '--dry-run' in args
    pos = [a for a in args if not a.startswith('--')]
    lanes = load_lanes(cfg)
    sessions = tmux_sessions()
    if pos:
        found = []
        for role in pos:
            found += lane_sessions(cfg, lanes.get(role) or {'role': role}, sessions)
    else:
        handles = live_handles(lanes)
        mine = own_pids()
        found = [o for o in orphans(cfg, lanes, sessions, handles)[0] if not mine & set(o['pids'])]
    if not found:
        print('no orphan sessions')
        return 0
    if dry:
        print('would kill: ' + ', '.join(f"{s['name']} ({s['role']})" for s in found))
        return 0
    names, alive = kill_sessions(found)
    print('killed: ' + (', '.join(names) or 'none'))
    if alive:
        print(f"WARNING still running: pids {', '.join(map(str, alive))}")
        return 1
    return 0


def cmd_ask(cfg, args):
    frm = take(args, '--from') or os.environ.get('AGENT_BUS_ROLE') or 'coordinator'
    opts = take(args, '--options')
    pos = [a for a in args if not a.startswith('--')]
    if not pos:
        raise SystemExit('usage: ask "<question>" [--from ROLE] [--options "a|b"]')
    n = len(load_questions(cfg)) + 1
    q = {'id': f'q{n}', 'from': frm, 'text': ' '.join(pos), 'options': opts, 'asked': now()}
    append_question(cfg, q)
    print(f"{q['id']} queued: asked at the next status, never blocking. Keep working meanwhile.")
    return 0


def cmd_questions(cfg, args):
    qs = list(load_questions(cfg).values())
    if '--all' not in args:
        qs = [q for q in qs if not q.get('answer')]
    if not qs:
        print('no open questions')
    for q in qs:
        ans = f" => {q['answer']}" if q.get('answer') else ''
        opts = f" [{q['options']}]" if q.get('options') else ''
        print(f"{q['id']} ({q.get('from', '?')}, {q.get('asked', '')}): {q['text']}{opts}{ans}")
    return 0


def cmd_answer(cfg, args):
    pos = [a for a in args if not a.startswith('--')]
    if len(pos) < 2:
        raise SystemExit('usage: answer <qid> "<answer>"')
    qid, ans = pos[0], ' '.join(pos[1:])
    qs = load_questions(cfg)
    if qid not in qs:
        raise SystemExit(f'coordinator: no question {qid}')
    append_question(cfg, {'id': qid, 'answer': ans, 'answered': now()})
    asker = qs[qid].get('from')
    if asker and asker != 'coordinator':
        r = bus(cfg, 'send', asker, f"Answer to {qid} ({qs[qid]['text'][:120]}): {ans}", '--from', 'coordinator')
        if r is None:
            print(f'(no bus) tell {asker}: {ans}')
    print(f'{qid} answered')
    return 0


# ---- preflight: may a heavy job start now?

def free_ram_gb():
    if os.name == 'nt':
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 2 ** 30
    try:
        for ln in open('/proc/meminfo'):
            if ln.startswith('MemAvailable:'):
                return int(ln.split()[1]) / 2 ** 20
    except OSError:
        pass
    try:
        out = subprocess.run(['vm_stat'], capture_output=True, **TEXT).stdout
        page = int(re.search(r'page size of (\d+)', out).group(1))
        pages = sum(int(re.search(rf'{k}:\s+(\d+)', out).group(1)) for k in ('Pages free', 'Pages inactive'))
        return pages * page / 2 ** 30
    except (OSError, AttributeError, ValueError):
        return None


def battery():
    """(percent, on_ac) or (None, None) when the machine has no battery or it cannot be read."""
    if os.name == 'nt':
        import ctypes

        class PS(ctypes.Structure):
            _fields_ = [('ACLineStatus', ctypes.c_ubyte), ('BatteryFlag', ctypes.c_ubyte),
                        ('BatteryLifePercent', ctypes.c_ubyte), ('SystemStatusFlag', ctypes.c_ubyte),
                        ('BatteryLifeTime', ctypes.c_ulong), ('BatteryFullLifeTime', ctypes.c_ulong)]
        s = PS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) or s.BatteryFlag == 128 \
                or s.BatteryLifePercent == 255:
            return None, None
        return s.BatteryLifePercent, s.ACLineStatus == 1
    for d in Path('/sys/class/power_supply').glob('BAT*') if Path('/sys/class/power_supply').exists() else []:
        try:
            pct = int((d / 'capacity').read_text())
            st = (d / 'status').read_text().strip()
            return pct, st in ('Charging', 'Full')
        except (OSError, ValueError):
            pass
    if shutil.which('pmset'):
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, **TEXT).stdout
        m = re.search(r'(\d+)%', out)
        if m:
            return int(m.group(1)), "AC Power" in out
    return None, None


def user_idle_s():
    if os.name == 'nt':
        import ctypes

        class LII(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]
        li = LII()
        li.cbSize = ctypes.sizeof(LII)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0
    return None


def cmd_preflight(cfg, args):
    min_ram = float(take(args, '--min-ram-gb') or cfg['minFreeRamGb'])
    min_bat = int(take(args, '--min-battery') or cfg['minBattery'])
    busy_s = take(args, '--max-user-idle-s')
    reasons, facts = [], []
    ram = free_ram_gb()
    if ram is not None:
        facts.append(f'free RAM {ram:.1f} GB')
        if ram < min_ram:
            reasons.append(f'free RAM {ram:.1f} GB < {min_ram} GB')
    pct, ac = battery()
    if pct is not None:
        facts.append(f'battery {pct}%{" on AC" if ac else ""}')
        if pct < min_bat and not ac:
            reasons.append(f'battery {pct}% < {min_bat}% and not charging')
    if busy_s is not None:
        idle = user_idle_s()
        if idle is not None:
            facts.append(f'user idle {int(idle)} s')
            if idle < float(busy_s):
                reasons.append(f'the user was active {int(idle)} s ago (< {busy_s} s)')
    verdict = 'WAIT' if reasons else 'GO'
    print(f"{verdict}: {'; '.join(reasons) if reasons else ', '.join(facts) or 'nothing measurable'}")
    return 1 if reasons else 0



# ---- usage: readings, the probe, the watcher

USAGE_HEADINGS = {'session': re.compile(r'current session', re.I),
                  'week': re.compile(r'current week \(all models\)', re.I)}
USED_RE = re.compile(r'(\d{1,3}(?:\.\d+)?)\s*%\s*used', re.I)
PROBE_START_S = 20      # claude's start-up before /usage is typed
PROBE_TIMEOUT_S = 60    # how long the screen is polled for the week line
PROBE_POLL_S = 2


def parse_usage(text):
    """{'session': int|None, 'week': int|None} from the text of Claude Code's /usage screen."""
    lines = [ln.strip() for ln in (text or '').replace('\r', '').split('\n')]
    out = {}
    for k, heading in USAGE_HEADINGS.items():
        out[k] = None
        for i, ln in enumerate(lines):
            if not heading.search(ln):
                continue
            m = USED_RE.search(ln)  # same line, else the next non-empty line (the bar and `N% used`)
            for nxt in lines[i + 1:i + 4]:
                if m or (nxt and heading.search(nxt)):
                    break
                m = USED_RE.search(nxt) if nxt else None
            if m:
                out[k] = int(float(m.group(1)))
                break
    return out


def wait_for_usage(read_screen, timeout=None):
    """Poll `read_screen()` until the week's `N% used` is on it (or the timeout); returns the last screen text."""
    deadline = time.time() + (PROBE_TIMEOUT_S if timeout is None else timeout)
    screen = ''
    while True:
        screen = read_screen() or ''
        if parse_usage(screen)['week'] is not None or time.time() >= deadline:
            return screen
        time.sleep(PROBE_POLL_S)


PROBE_DEBUG = {'dir': None, 'n': 0}  # `--debug`: every raw screen read is saved here


def save_debug(raw, parsed_from=None):
    d = PROBE_DEBUG['dir']
    if not d:
        return
    d.mkdir(parents=True, exist_ok=True)
    PROBE_DEBUG['n'] += 1
    stem = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{PROBE_DEBUG['n']:02d}"
    stem.with_suffix('.raw.txt').write_text(raw or '', encoding='utf-8')
    if parsed_from is not None:
        stem.with_suffix('.screen.txt').write_text(parsed_from, encoding='utf-8')
    print(f'usage probe: debug read {PROBE_DEBUG["n"]} -> {stem}.*  parsed {parse_usage(parsed_from or raw)}')


def tee_debug(text):
    save_debug(text)
    return text


def orca_screen(h, dry=False):
    """The rendered screen of orca terminal `h` as text."""
    out = run(['orca', 'terminal', 'read', '--terminal', h, '--screen', '--json'], dry).stdout
    text = screen_text(out)
    save_debug(out, text)
    return text


def screen_text(out):
    """The screen lines of `orca terminal read --screen --json` output (the raw text when it is not JSON)."""
    try:
        tail = ((json.loads(out or '{}').get('result') or {}).get('terminal') or {}).get('tail')
    except (ValueError, AttributeError):
        return out or ''
    if isinstance(tail, list):
        return '\n'.join(str(x) for x in tail)
    return tail if isinstance(tail, str) else (out or '')


def usage_file(cfg):
    return cfg.path('reportsDir') / 'usage.json'


def read_usage(cfg):
    try:
        return json.loads(usage_file(cfg).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_usage(cfg, reading, source):
    rec = {'session': reading.get('session'), 'week': reading.get('week'), 'source': source, 'time': time.time(),
           'at': now()}
    p = usage_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding='utf-8')
    with open(cfg.path('reportsDir') / 'usage_history.log', 'a', encoding='utf-8') as f:
        f.write(f"{rec['at']} week={rec['week']}% session={rec['session']}% ({source})\n")
    return rec


def record_probe(cfg, reading):
    """Store a probe's reading. A probe that read no numbers is logged but never replaces the last good reading,
    so it can neither hide a real one nor be reused as if it were fresh."""
    source = 'probe:' + cfg.backend
    if reading.get('week') is None and reading.get('session') is None:
        p = cfg.path('reportsDir') / 'usage_history.log'
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(f"{now()} probe read no usage numbers ({source}); kept the last reading\n")
        print(f'{now()} usage probe: the /usage screen had no numbers (run with --debug to save the screens)')
        return None
    return write_usage(cfg, reading, source)


def usable_reading(cfg, last):
    """The last reading when it has a weekly number and is younger than the current probe interval."""
    wk = last.get('week')
    return bool(last) and wk is not None and time.time() - last.get('time', 0) < probe_interval_min(cfg, wk) * 60


def probe_interval_min(cfg, week):
    """Minutes until the next probe: the step of `probeIntervals` (pct:min,...) that the weekly % has reached."""
    steps = []
    for part in str(cfg['probeIntervals']).split(','):
        if ':' in part:
            a, b = part.split(':', 1)
            steps.append((float(a), float(b)))
    steps.sort()
    mins = steps[0][1] if steps else 60.0
    for pct, m in steps:
        if week is not None and week >= pct:
            mins = m
    return mins


def probe_usage(cfg, dry=False):
    """Read /usage from a throwaway Claude Code session in a terminal the backend can read back (orca, herdr, tmux).

    The session runs `probeCommand` (a cheap model), gets `/usage` typed, has its screen read, and is closed.
    No task work happens in it. Returns {'session','week'} or raises SystemExit when the backend cannot read a screen.
    """
    b = cfg.backend
    repo = cfg.repos()[0]
    if b == 'herdr':
        ws = herdr_workspace(repo, dry)
        if dry:
            herdr_open_tab(ws, repo, 'usage-probe', dry=True)
            run(['herdr', 'pane', 'run', '<pane>', cfg['probeCommand']], dry)
            return {'session': None, 'week': None}
        pane = {}
        before = herdr_tabs_labelled(ws, 'usage-probe')
        try:
            pane = herdr_open_tab(ws, repo, 'usage-probe')
            h = pane['pane_id']
            run(['herdr', 'pane', 'run', h, cfg['probeCommand']])
            time.sleep(PROBE_START_S)
            run(['herdr', 'pane', 'run', h, '/usage'])
            screen = wait_for_usage(lambda: herdr_screen(h))
            run(['herdr', 'pane', 'send-keys', h, 'esc'])
            run(['herdr', 'pane', 'run', h, '/exit'])
            time.sleep(2)
        finally:  # whatever failed, the probe tab never outlives the probe
            closed = bool(pane.get('tab_id')) and run(['herdr', 'tab', 'close', pane['tab_id']]).returncode == 0
            if not closed:
                herdr_close_by_label(ws, 'usage-probe', before)
        return parse_usage(screen)
    if b == 'orca':
        if dry:
            run(['orca', 'terminal', 'create', '--worktree', f'path:{repo}', '--title', 'usage-probe', '--json'], dry)
            return {'session': None, 'week': None}
        h = None
        before = orca_titled(repo, 'usage-probe')
        try:
            r = run(['orca', 'terminal', 'create', '--worktree', f'path:{repo}', '--title', 'usage-probe', '--json'])
            h = orca_handle(r.stdout)
            if not h:
                raise SystemExit('coordinator: usage probe: orca could not open a terminal')
            run(['orca', 'terminal', 'send', '--terminal', h, '--text', cfg['probeCommand'], '--enter'])
            time.sleep(PROBE_START_S)
            run(['orca', 'terminal', 'send', '--terminal', h, '--text', '/usage', '--enter'])
            screen = wait_for_usage(lambda: orca_screen(h))
            run(['orca', 'terminal', 'send', '--terminal', h, '--text', '\x1b'])
            run(['orca', 'terminal', 'send', '--terminal', h, '--text', '/exit', '--enter'])
            time.sleep(2)
        finally:  # whatever failed, the probe tab never outlives the probe
            closed = h is not None and run(['orca', 'terminal', 'close', '--terminal', h, '--tab']).returncode == 0
            if not closed:
                orca_close_by_title(repo, 'usage-probe', before)
        return parse_usage(screen)
    if b == 'tmux':
        target = [] if os.environ.get('TMUX') else ['-t', 'coord']
        if target and run(['tmux', 'has-session', '-t', 'coord'], dry).returncode != 0:
            run(['tmux', 'new-session', '-d', '-s', 'coord', '-n', 'coord'], dry)
        r = run(['tmux', 'new-window', '-d', *target, '-P', '-F', '#{window_id}', '-n', 'usage-probe', '-c', repo,
                 cfg['probeCommand']], dry)
        w = (r.stdout or '').strip()
        if dry:
            return {'session': None, 'week': None}
        try:
            time.sleep(PROBE_START_S)
            run(['tmux', 'send-keys', '-t', w, '/usage', 'Enter'], dry)
            screen = wait_for_usage(lambda: tee_debug(run(['tmux', 'capture-pane', '-p', '-t', w], dry).stdout))
        finally:
            run(['tmux', 'kill-window', '-t', w], dry)
        return parse_usage(screen)
    raise SystemExit(f"coordinator: the {b} backend cannot read a terminal back, so it cannot probe /usage. The plugin "
                     'still records the coordinator session\'s own usage readings; run `usage` to see the latest.')


def alert_coordinator(cfg, text):
    """Urgent to the coordinator: over the bus when there is one, else into the alerts file the plugin shows."""
    r = bus(cfg, 'send', 'coordinator', text, '--prio', 'urgent', '--topic', 'usage', '--from', 'usage-watch')
    p = cfg.path('reportsDir') / 'alerts.jsonl'
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'at': now(), 'time': time.time(), 'text': text, 'bus': r is not None and r.returncode == 0})
                + '\n')
    print(text)


def usage_alerts(cfg, rec, state):
    """Alert once per crossing of each threshold; `state` remembers what was alerted. Returns the texts sent."""
    sent = []
    wk, ss = rec.get('week'), rec.get('session')
    if wk is not None and wk >= cfg['pauseWeeklyPercent'] and not state.get('week'):
        t = (f"usage-watch: WEEKLY usage {wk}% >= {cfg['pauseWeeklyPercent']}%: start the graceful pause "
             '(`coord pause`; every lane checkpoints its card and stops, nothing is lost).')
        alert_coordinator(cfg, t)
        sent.append(t)
        state['week'] = True
    if wk is not None and wk < cfg['pauseWeeklyPercent']:
        state['week'] = False
    if ss is not None and ss >= cfg['pauseSessionPercent'] and not state.get('session'):
        t = f"usage-watch: 5-HOUR session usage {ss}% >= {cfg['pauseSessionPercent']}%: pause now (`coord pause`)."
        alert_coordinator(cfg, t)
        sent.append(t)
        state['session'] = True
    if ss is not None and ss < cfg['pauseSessionPercent']:
        state['session'] = False
    return sent


def debug_flag(cfg, args):
    if '--debug' in args:
        PROBE_DEBUG['dir'] = cfg.path('reportsDir') / 'usage-debug'


def cmd_usage(cfg, args):
    debug_flag(cfg, args)
    if '--probe' in args:
        rec = record_probe(cfg, probe_usage(cfg, '--dry-run' in args)) or read_usage(cfg)
    else:
        rec = read_usage(cfg)
    if '--json' in args:
        print(json.dumps(rec))
        return 0
    if not rec:
        print('no usage reading yet: the plugin records the coordinator session\'s readings; `usage --probe` reads '
              '/usage through the backend (orca, herdr, tmux)')
        return 1
    age = int((time.time() - rec.get('time', 0)) / 60)
    print(f"week {rec.get('week')}% (pause at {cfg['pauseWeeklyPercent']}%), session {rec.get('session')}% "
          f"(pause at {cfg['pauseSessionPercent']}%), {age} min old, from {rec.get('source')}")
    return 0


def cmd_record_usage(cfg, args):
    """record-usage [--week N] [--session N] [--source S]: a reading the plugin took from its own session."""
    wk, ss = take(args, '--week'), take(args, '--session')
    rec = write_usage(cfg, {'week': int(float(wk)) if wk else None, 'session': int(float(ss)) if ss else None},
                      take(args, '--source') or 'session')
    state_p = cfg.path('reportsDir') / 'usage_alerted.json'
    try:
        state = json.loads(state_p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    if '--alert' in args:
        usage_alerts(cfg, rec, state)
        state_p.write_text(json.dumps(state), encoding='utf-8')
    return 0


def cmd_usage_watch(cfg, args):
    """Probe on a schedule and alert at the thresholds. A reading the plugin recorded recently counts as a probe."""
    debug_flag(cfg, args)
    if '--start' in args:
        cmd = f'python "{Path(__file__).resolve().as_posix()}" usage-watch'
        handle, how = open_terminal(cfg, 'usage-watch', cfg.repos()[0], cmd, '--dry-run' in args)
        print(f'usage-watch: started ({how}{" " + handle if handle else ""})')
        return 0
    state = {}
    while True:
        last = read_usage(cfg)
        fresh = usable_reading(cfg, last) and '--probe' not in args
        try:
            rec = last if fresh else (record_probe(cfg, probe_usage(cfg)) or last)
        except SystemExit as ex:
            print(f'{now()} {ex}')
            rec = last
        if rec:
            age = f" (reading of {rec.get('at')}, {rec.get('source')})" if rec is last else ''
            print(f"{now()} week={rec.get('week')}% session={rec.get('session')}%{age}", flush=True)
            usage_alerts(cfg, rec, state)
        if '--once' in args:
            return 0
        time.sleep(probe_interval_min(cfg, (rec or {}).get('week')) * 60)


# ---- the graceful pause

def pause_file(cfg):
    return cfg.path('cardsDir') / 'pause.json'


def load_pause(cfg):
    try:
        return json.loads(pause_file(cfg).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def save_pause(cfg, st):
    p = pause_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True), encoding='utf-8')
    os.replace(tmp, p)


def clock():
    return time.time()


PAUSE_TEXT = ('PAUSE (usage limit): finish your current atomic step (or park it so nothing is half-done), update your '
              'lane card {card} (state, next 3 actions, anchors, open messages), release any scheduler slot, then run '
              '`coord paused` and end with SAFE TO CLOSE. Do not start anything new. Going a little over the limit '
              'to finish cleanly is fine.')


def tell(cfg, role, text, prio='urgent'):
    r = bus(cfg, 'send', role, text, '--prio', prio, '--topic', 'pause', '--from', 'coordinator')
    if r is None or r.returncode != 0:
        print(f'(no bus) tell {role}: {text}')


def interrupt(cfg, lane, dry=False):
    """Stop a lane that did not answer: an Escape to the agent first (orca, herdr, tmux), then close its terminal."""
    h, how = lane.get('handle'), lane.get('backend')
    if how == 'orca' and h:
        run(['orca', 'terminal', 'send', '--terminal', h, '--text', '\x1b'], dry)
    elif how == 'herdr' and h:
        pane = {'pane_id': lane.get('handle')} if dry else herdr_locate(lane)
        if pane:
            run(['herdr', 'pane', 'send-keys', pane['pane_id'], 'esc'], dry)
    elif how == 'tmux' and h:
        run(['tmux', 'send-keys', '-t', h, 'Escape'], dry)
    return close_terminal(cfg, lane, dry)


def write_resume_list(cfg, st):
    lines = [f"# Resume list (paused {st.get('started_at')}, finished {now()})", '',
             'Run `coord resume` to respawn these lanes from their cards.', '']
    for role, l in sorted(st['lanes'].items()):
        lines.append(f"- {role}: {l['state']} -- card {rel(cfg, cfg.path('cardsDir') / f'{role}.md')}")
    p = cfg.path('reportsDir') / 'RESUME_LIST.md'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return p


def pause_tick(cfg, st, dry=False):
    """One pass: close lanes that checkpointed, remind once, interrupt past the grace. True when all are done."""
    lanes = load_lanes(cfg)
    t = clock()
    for role, pl in st['lanes'].items():
        if pl['state'] in ('paused', 'interrupted'):
            continue
        lane = lanes.get(role, {'role': role})
        if pl['state'] == 'checkpointed' or last_word(cfg, role) == 'safe':
            close_terminal(cfg, lane, dry)
            pl['state'], pl['closed_at'] = 'paused', now()
        elif t - pl['asked'] >= cfg['pauseGraceMin'] * 60:
            interrupt(cfg, lane, dry)
            pl['state'], pl['closed_at'] = 'interrupted', now()
        elif t - pl['asked'] >= cfg['pauseReminderMin'] * 60 and not pl.get('reminded'):
            tell(cfg, role, 'REMINDER: ' + PAUSE_TEXT.format(card=rel(cfg, cfg.path('cardsDir') / f'{role}.md'))
                 + f" You will be interrupted in {int(cfg['pauseGraceMin'] - cfg['pauseReminderMin'])} min.")
            pl['reminded'] = now()
        if pl['state'] in ('paused', 'interrupted') and role in lanes:
            lanes[role]['state'] = 'paused'
    if not dry:
        save_lanes(cfg, lanes)
    done = all(pl['state'] in ('paused', 'interrupted') for pl in st['lanes'].values())
    if done and st.get('state') != 'complete':
        st['state'] = 'complete'
        st['finished_at'] = now()
        st['resume_list'] = rel(cfg, write_resume_list(cfg, st))
    return done


def cmd_pause(cfg, args):
    dry = '--dry-run' in args
    st = load_pause(cfg)
    if not ('--tick' in args or '--wait' in args) or st is None or st.get('state') == 'complete':
        if st and st.get('state') == 'pausing' and '--now' not in args:
            print('a pause is already running; `pause --tick` advances it')
        else:
            lanes = load_lanes(cfg)
            live = sorted(r for r, l in lanes.items() if l.get('state') in ('live', 'retiring')
                          and r not in ('usage-watch',) and not is_coordinator(r, l))
            st = {'state': 'pausing', 'started_at': now(), 'started': clock(),
                  'lanes': {r: {'state': 'asked', 'asked': clock()} for r in live}}
            for r in live:
                tell(cfg, r, PAUSE_TEXT.format(card=rel(cfg, cfg.path('cardsDir') / f'{r}.md')))
            print(f"pause: asked {len(live)} lane(s) to checkpoint: {', '.join(live) or 'none'}. Reminder after "
                  f"{cfg['pauseReminderMin']:g} min, interrupt after {cfg['pauseGraceMin']:g} min.")
    while True:
        done = pause_tick(cfg, st, dry)
        if not dry:
            save_pause(cfg, st)
        counts = {}
        for pl in st['lanes'].values():
            counts[pl['state']] = counts.get(pl['state'], 0) + 1
        print('pause: ' + (', '.join(f'{v} {k}' for k, v in sorted(counts.items())) or 'no lanes')
              + (f" -- complete; resume list {st.get('resume_list')}" if done else ''), flush=True)
        if done or '--wait' not in args:
            return 0
        time.sleep(30)


def cmd_paused(cfg, args):
    role = take(args, '--role') or os.environ.get('AGENT_BUS_ROLE')
    if not role:
        raise SystemExit('coordinator: paused: who are you? pass --role or set AGENT_BUS_ROLE')
    st = load_pause(cfg)
    if not st or role not in st.get('lanes', {}):
        print(f'{role}: no pause is running for this lane; nothing recorded')
        return 1
    st['lanes'][role].update({'state': 'checkpointed', 'checkpointed_at': now()})
    save_pause(cfg, st)
    bus(cfg, 'send', 'coordinator', f'{role} paused: card updated, stopping.', '--prio', 'fyi', '--topic', 'pause',
        '--from', role)
    print(f'{role}: checkpointed. End your turn with SAFE TO CLOSE; the coordinator closes this terminal.')
    return 0


def cmd_resume(cfg, args):
    dry = '--dry-run' in args
    st = load_pause(cfg)
    if not st:
        print('nothing to resume: no pause on record')
        return 1
    if st.get('state') == 'pausing':
        print('the pause is still running (`pause --tick`); resuming anyway respawns the lanes already stopped')
    started = []
    for role, pl in sorted(st['lanes'].items()):
        if pl['state'] not in ('paused', 'interrupted'):
            continue
        code = cmd_respawn(cfg, [role] + (['--dry-run'] if dry else []))
        if code == 0:
            started.append(role)
            pl['state'] = 'resumed'
    if not dry:
        st['state'] = 'resumed' if all(pl['state'] in ('resumed',) for pl in st['lanes'].values()) else st['state']
        st['resumed_at'] = now()
        save_pause(cfg, st)
    print(f"resume: {', '.join(started) or 'nothing'}")
    return 0


def take(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return None


def cmd_context(cfg, args):
    """JSON for the plugin's session-start hook: is this project coordinated, and who is this session."""
    role = os.environ.get('AGENT_BUS_ROLE') or ''
    configured = cfg.file.exists() and 'coordinator' in json.loads(cfg.file.read_text(encoding='utf-8') or '{}')
    lanes = load_lanes(cfg) if configured else {}
    card = cfg.path('cardsDir') / f'{role}.md'
    out = {
        'configured': configured,
        'role': role,
        'isCoordinator': role in ('', 'coordinator'),
        'rules': rel(cfg, cfg.path('rulesFile')),
        'card': rel(cfg, card) if role else None,
        'cardExists': bool(role) and card.exists(),
        'live': sorted(r for r, l in lanes.items() if l.get('state') in ('live', 'retiring')),
        'questions': sum(1 for q in load_questions(cfg).values() if not q.get('answer')) if configured else 0,
        'retireTokens': cfg['retireTokens'],
        'pauseSessionPercent': cfg['pauseSessionPercent'],
        'pauseWeeklyPercent': cfg['pauseWeeklyPercent'],
        'pause': (load_pause(cfg) or {}).get('state') if configured else None,
        'maxLanes': cfg['maxLanes'],
    }
    print(json.dumps(out))
    return 0


VERBS = {
    'context': cmd_context,
    'init': cmd_init, 'spawn': cmd_spawn, 'retire': cmd_retire, 'respawn': cmd_respawn, 'lanes': cmd_lanes,
    'status': cmd_status, 'close-idle': cmd_close_idle, 'reap': cmd_reap, 'ask': cmd_ask, 'questions': cmd_questions,
    'answer': cmd_answer, 'preflight': cmd_preflight, 'usage': cmd_usage, 'usage-watch': cmd_usage_watch,
    'succeed': cmd_succeed, 'handed-over': cmd_handed_over, 'close-predecessor': cmd_close_predecessor,
    'pause': cmd_pause, 'paused': cmd_paused, 'resume': cmd_resume, 'record-usage': cmd_record_usage,
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help', 'help'):
        print(__doc__)
        return 0
    flags = {}
    for k in ('backend', 'repos', 'workerModel', 'maxLanes'):
        v = take(args, '--' + re.sub(r'(?<!^)(?=[A-Z])', '-', k).lower())
        if v:
            flags[k] = v
    cfg = Config(project_root(), flags)
    HERDR_PIN['session'] = take(args, '--herdr-session') or None
    verb = args.pop(0)
    if verb == 'config':
        print(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=1))
        return 0
    if verb not in VERBS:
        print(f'coordinator: unknown verb {verb!r}\n' + __doc__, file=sys.stderr)
        return 2
    return VERBS[verb](cfg, args)


if __name__ == '__main__':
    sys.exit(main())
