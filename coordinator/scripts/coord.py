"""coordinator: run many worker agents, each in its own terminal with a brief, from one coordinator session.

    python coord.py init [--roles a,b] [--force]       scaffold rules, templates, the question queue and config
    python coord.py spawn <role> [<brief>] [--repo P] [--model M] [--dry-run]
                                                        start a worker on a brief (default: the resume brief)
    python coord.py retire <role> [--close]             ask a worker to update its card and stop; --close its terminal
    python coord.py respawn <role> [--dry-run]          close the worker and start a successor on its lane card
    python coord.py lanes [--json]                      the lanes, their terminals, cards and tasks
    python coord.py status                              a status report: lanes, open questions, safe-to-close
    python coord.py close-idle [--dry-run]              close the terminals of lanes whose last word is SAFE TO CLOSE
    python coord.py ask "<question>" [--from ROLE] [--options "a|b"]   queue a question for the user
    python coord.py questions [--all]                   the open (or all) questions
    python coord.py answer <qid> "<answer>"             answer a question; the asker gets it on the bus
    python coord.py preflight [--min-ram-gb N] [--min-battery P] [--max-user-idle-s S]
                                                        GO (exit 0) or WAIT (exit 1) for a heavy job, with reasons
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
    'backend': 'manual',              # orca | tmux | wt | manual
    'shell': 'auto',                  # auto | posix | powershell | cmd: how the env prefix is written
    'workerCommand': 'claude --model {model} "{prompt}"',
    'workerPrompt': 'Read {brief} and execute it. Your role is {role}.',
    'workerModel': 'opus',
    'maxLanes': 6,
    'retireTokens': 250000,
    'pauseAtPercent': 97,
    'busDir': '.claude/bus',
    'trackerDb': '.claude/tasks/tasks.db',
    'rulesFile': '.claude/coord/RULES_CORE.md',
    'briefsDir': '.claude/coord/briefs',
    'cardsDir': '.claude/coord/lanes',
    'reportsDir': '.claude/coord/reports',
    'minFreeRamGb': 4,
    'minBattery': 40,
}
INTS = ('maxLanes', 'retireTokens', 'pauseAtPercent', 'minBattery')
FLOATS = ('minFreeRamGb',)
PATHS = ('busDir', 'trackerDb', 'rulesFile', 'briefsDir', 'cardsDir', 'reportsDir')


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
                self.update(json.loads(self.file.read_text(encoding='utf-8')).get('coordinator', {}))
            except (ValueError, OSError) as ex:
                print(f'coordinator: ignoring {self.file}: {ex}', file=sys.stderr)
        for k, v in (flags or {}).items():
            if v not in (None, ''):
                self[k] = v
        for k in INTS:
            self[k] = int(self[k])
        for k in FLOATS:
            self[k] = float(self[k])
        if self['backend'] not in ('orca', 'tmux', 'wt', 'manual'):
            raise SystemExit(f"coordinator: backend must be orca, tmux, wt or manual, not {self['backend']!r}")

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

def bus_cli():
    """The agent-bus CLI as an argv prefix, or None. Found via AGENT_BUS_CLI or a sibling plugin folder."""
    cands = [os.environ.get('AGENT_BUS_CLI', ''), str(HERE.parent.parent / 'agent-bus' / 'scripts' / 'bus.py')]
    for c in cands:
        if c and Path(c).is_file():
            return [sys.executable, c]
    return None


def bus(cfg, *args, check=False):
    cli = bus_cli()
    if not cli or not cfg.path('busDir').is_dir():
        return None
    r = subprocess.run(cli + ['--dir', str(cfg.path('busDir'))] + list(args), capture_output=True, text=True,
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


def with_env(cfg, role, cmd):
    kind = shell_kind(cfg)
    if kind == 'powershell':
        return f"$env:AGENT_BUS_ROLE='{role}'; {cmd}"
    if kind == 'cmd':
        return f'set AGENT_BUS_ROLE={role}&& {cmd}'
    return f'AGENT_BUS_ROLE={shlex.quote(role)} {cmd}'


def worker_command(cfg, role, brief, model=None):
    vals = {'role': role, 'brief': brief, 'model': model or cfg['workerModel'], 'repo': cfg.repos()[0]}
    vals['prompt'] = cfg['workerPrompt'].format(**vals)
    return cfg['workerCommand'].format(**vals)


def run(argv, dry, **kw):
    if dry:
        print('would run: ' + ' '.join(shlex.quote(a) for a in argv))
        return subprocess.CompletedProcess(argv, 0, '{}', '')
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def open_terminal(cfg, role, repo, cmd, dry=False):
    """Start `cmd` for `role` in a new terminal; returns (handle, how). The worker's env carries AGENT_BUS_ROLE."""
    b = cfg['backend']
    if b == 'orca':
        r = run(['orca', 'terminal', 'create', '--worktree', f'path:{repo}', '--title', role,
                 '--command', with_env(cfg, role, cmd), '--json'], dry)
        try:
            res = json.loads(r.stdout or '{}')
            handle = (res.get('result') or {}).get('handle') or res.get('handle')
        except ValueError:
            handle = None
        if not handle and not dry:
            raise SystemExit(f'coordinator: orca terminal create failed: {r.stderr.strip() or r.stdout.strip()}')
        return handle, 'orca'
    if b == 'tmux':
        target = []
        if not os.environ.get('TMUX'):
            has = run(['tmux', 'has-session', '-t', 'coord'], dry)
            if has.returncode != 0:
                run(['tmux', 'new-session', '-d', '-s', 'coord', '-n', 'coord'], dry)
            target = ['-t', 'coord']
        r = run(['tmux', 'new-window', *target, '-P', '-F', '#{window_id}', '-n', role, '-c', repo,
                 '-e', f'AGENT_BUS_ROLE={role}', cmd], dry)
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
    h, how = lane.get('handle'), lane.get('backend')
    if how == 'orca' and h:
        r = run(['orca', 'terminal', 'close', '--terminal', h, '--tab'], dry)
        return r.returncode == 0
    if how == 'tmux' and h:
        r = run(['tmux', 'kill-window', '-t', h], dry)
        return r.returncode == 0
    print(f'Close the terminal of {lane.get("role")} yourself ({how}: no handle to close).')
    return False


# ---- verbs

def render(text, cfg, role='<role>'):
    vals = {
        'role': role, 'rules': rel(cfg, cfg.path('rulesFile')), 'cards': rel(cfg, cfg.path('cardsDir')),
        'briefs': rel(cfg, cfg.path('briefsDir')), 'reports': rel(cfg, cfg.path('reportsDir')),
        'retire_tokens': f"{cfg['retireTokens'] // 1000}k", 'pause_percent': cfg['pauseAtPercent'],
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
                           text=True, cwd=cfg.root)
        made.append('bus: ' + (r.stdout.strip() or r.stderr.strip()))
    else:
        made.append('bus: agent-bus plugin not found; messages fall back to files in ' + rel(cfg, cfg.path('reportsDir')))
    print('coordinator init:\n  ' + '\n  '.join(made))
    print('Next: edit section 1 of the rules file (the project), then `spawn <role> <brief>`.')
    return 0


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
    live = [r for r, l in lanes.items() if l.get('state') == 'live' and r != role]
    if len(live) >= cfg['maxLanes']:
        raise SystemExit(f"coordinator: {len(live)} lanes live (max {cfg['maxLanes']}): retire one first "
                         f"({', '.join(sorted(live))})")
    if lanes.get(role, {}).get('state') == 'live':
        raise SystemExit(f'coordinator: {role} is already live; use respawn')
    cmd = worker_command(cfg, role, brief, model)
    handle, how = open_terminal(cfg, role, repo, cmd, dry)
    if dry:
        return 0
    if handle and how == 'orca':
        bus(cfg, 'register', role, '--handle', handle, '--kind', 'claude', '--mode', 'file')
    elif bus_cli() and cfg.path('busDir').is_dir():
        bus(cfg, 'register', role, '--kind', 'claude', '--mode', 'file')
    gen = lanes.get(role, {}).get('generation', 0) + 1
    lanes[role] = {'role': role, 'brief': brief, 'repo': repo, 'backend': how, 'handle': handle, 'state': 'live',
                   'started': now(), 'generation': gen, 'model': model or cfg['workerModel']}
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


def cmd_respawn(cfg, args):
    dry = '--dry-run' in args
    pos = [a for a in args if not a.startswith('--')]
    if not pos:
        raise SystemExit('usage: respawn <role>')
    role = pos[0]
    lanes = load_lanes(cfg)
    lane = lanes.get(role)
    if lane and lane.get('state') != 'closed':
        close_terminal(cfg, lane, dry)
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


def cmd_status(cfg, args):
    lanes = load_lanes(cfg)
    live = [r for r, l in lanes.items() if l.get('state') in ('live', 'retiring')]
    print(f'# Status {now()}\n')
    print(f"## Lanes ({len(live)} live of max {cfg['maxLanes']})")
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
    r = bus(cfg, 'digest', '--as', 'coordinator')
    if r is not None and r.stdout.strip():
        print('\n## Coordinator inbox\n' + r.stdout.strip())
    return 0


def cmd_close_idle(cfg, args):
    dry = '--dry-run' in args
    lanes = load_lanes(cfg)
    closed = []
    for role, lane in sorted(lanes.items()):
        if lane.get('state') in ('live', 'retiring') and last_word(cfg, role) == 'safe':
            close_terminal(cfg, lane, dry)
            if not dry:
                lane['state'] = 'closed'
                lane['retired'] = now()
            closed.append(role)
    if not dry:
        save_lanes(cfg, lanes)
    print(('would close: ' if dry else 'closed: ') + (', '.join(closed) or 'none'))
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
        out = subprocess.run(['vm_stat'], capture_output=True, text=True).stdout
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
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True).stdout
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
        'pauseAtPercent': cfg['pauseAtPercent'],
        'maxLanes': cfg['maxLanes'],
    }
    print(json.dumps(out))
    return 0


VERBS = {
    'context': cmd_context,
    'init': cmd_init, 'spawn': cmd_spawn, 'retire': cmd_retire, 'respawn': cmd_respawn, 'lanes': cmd_lanes,
    'status': cmd_status, 'close-idle': cmd_close_idle, 'ask': cmd_ask, 'questions': cmd_questions,
    'answer': cmd_answer, 'preflight': cmd_preflight,
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
