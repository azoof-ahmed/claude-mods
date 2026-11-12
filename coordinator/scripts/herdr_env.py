"""Find the herdr pane a session runs in when its own environment doesn't say so.

herdr gives every pane HERDR_ENV=1, HERDR_PANE_ID, HERDR_SOCKET_PATH and friends. A `claude` wrapper that runs the
agent inside its own tmux session (claude-auto-retry) only attaches the herdr pane to that session, so the agent's
environment is the tmux server's: often none of the HERDR_ variables, plus a leftover ORCA_TERMINAL_HANDLE when the
server or the session was started in an Orca terminal. `recover` puts the herdr variables back, from:

1. the tmux client attached to this session (it runs in the herdr pane, so it has them), or
2. the running herdr sessions: the pane herdr reports for this Claude session id, else the one session with a pane
   in this project (no pane id then, but the session's socket, so new tabs open beside it).

Shipped twice, as coordinator/scripts/herdr_env.py and agent-bus/scripts/herdr_env.py; the two copies are identical
(a test checks it).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

KEYS = ('HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_TAB_ID', 'HERDR_WORKSPACE_ID', 'HERDR_SESSION', 'HERDR_SOCKET_PATH')
TEXT = {'text': True, 'encoding': 'utf-8', 'errors': 'replace'}


def run(argv, env=None, timeout=5):
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, env=env, **TEXT)
    except (OSError, subprocess.SubprocessError):
        return None


def in_pane(env):
    """The environment names a herdr pane."""
    return env.get('HERDR_ENV') == '1' and bool(env.get('HERDR_PANE_ID'))


def here(env):
    """The environment is herdr's: a pane, or (recovered without a pane) a herdr session's socket."""
    return env.get('HERDR_ENV') == '1' and bool(env.get('HERDR_PANE_ID') or env.get('HERDR_SOCKET_PATH'))


def parse_env_words(text):
    """HERDR_ variables from `ps -E` output (the command, then the environment, space separated)."""
    out = {}
    for w in text.split():
        k, eq, v = w.partition('=')
        if eq and k in KEYS:
            out[k] = v
    return out


def proc_env(pid):
    """The HERDR_ variables process `pid` started with ({} when they can't be read)."""
    p = Path(f'/proc/{pid}/environ')
    if p.exists():
        try:
            pairs = p.read_bytes().decode('utf-8', 'replace').split('\0')
        except OSError:
            return {}
        return {k: v for k, _, v in (e.partition('=') for e in pairs) if k in KEYS}
    r = run(['ps', '-E', '-ww', '-o', 'command=', '-p', str(pid)])  # macOS: a process of the same user
    return parse_env_words(r.stdout) if r and r.returncode == 0 else {}


def from_tmux(env, read_env=proc_env):
    """The herdr pane of the tmux client attached to this tmux session (the most recently active one)."""
    tm, pane = env.get('TMUX'), env.get('TMUX_PANE')
    if not tm or not pane or os.name == 'nt' or not shutil.which('tmux'):
        return {}
    parts = tm.split(',')
    r = run(['tmux', 'display-message', '-p', '-t', pane, '#{pid}\t#{session_id}'])
    if not r or r.returncode != 0:
        return {}
    pid, _, sid = r.stdout.strip().partition('\t')
    if len(parts) > 1 and parts[1] != pid:
        return {}  # another tmux server took over the socket: this session's server can't be asked
    r = run(['tmux', 'list-clients', '-t', sid, '-F', '#{client_activity}\t#{client_pid}'])
    if not r or r.returncode != 0:
        return {}
    clients = sorted((ln.split('\t') for ln in r.stdout.splitlines() if '\t' in ln), reverse=True,
                     key=lambda c: int(c[0]) if c[0].isdigit() else 0)
    for _, cpid in clients:
        e = read_env(cpid) if cpid.isdigit() else {}
        if in_pane(e):
            return e
    return {}


def same_path(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (TypeError, ValueError, OSError):
        return a == b


def herdr_sessions():
    """[{name, socket_path}] of the running herdr sessions ([] without herdr)."""
    if not shutil.which('herdr'):
        return []
    r = run(['herdr', 'session', 'list', '--json'])
    try:
        data = json.loads(r.stdout) if r and r.returncode == 0 else {}
    except ValueError:
        return []
    return [s for s in data.get('sessions') or [] if isinstance(s, dict) and s.get('running') and s.get('socket_path')]


def session_panes(sock):
    env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_')}
    env['HERDR_SOCKET_PATH'] = sock
    r = run(['herdr', 'pane', 'list'], env=env)
    try:
        res = (json.loads(r.stdout) if r and r.returncode == 0 else {}).get('result') or {}
    except (ValueError, AttributeError):
        return []
    return [p for p in res.get('panes') or [] if isinstance(p, dict)]


def from_sessions(env, cwd, sessions=None, panes=None):
    """This Claude session's pane in a running herdr session, else the one session with a pane in `cwd`."""
    sessions = herdr_sessions() if sessions is None else sessions
    panes = panes or session_panes
    me = env.get('CLAUDE_CODE_SESSION_ID') or env.get('CLAUDE_SESSION_ID')
    near = {}
    for s in sessions:
        ctx = {'HERDR_ENV': '1', 'HERDR_SESSION': s.get('name') or '', 'HERDR_SOCKET_PATH': s['socket_path']}
        for p in panes(s['socket_path']):
            if me and (p.get('agent_session') or {}).get('value') == me:
                return {**ctx, 'HERDR_PANE_ID': p.get('pane_id') or '', 'HERDR_TAB_ID': p.get('tab_id') or '',
                        'HERDR_WORKSPACE_ID': p.get('workspace_id') or ''}
            if cwd and any(p.get(k) and same_path(p[k], cwd) for k in ('cwd', 'foreground_cwd')):
                near.setdefault(s['socket_path'], (ctx, set()))[1].add(p.get('workspace_id'))
    if len(near) != 1:
        return {}
    ctx, ws = next(iter(near.values()))
    return {**ctx, **({'HERDR_WORKSPACE_ID': next(iter(ws))} if len(ws) == 1 and None not in ws else {})}


def orca_running():
    """Whether the Orca app runs (macOS writes its runtime file); unknown platforms count as running."""
    if sys.platform != 'darwin':
        return True
    return (Path.home() / 'Library' / 'Application Support' / 'orca' / 'orca-runtime.json').exists()


def recover(env=None, cwd=None, orca_up=None, tmux=from_tmux, sessions=from_sessions):
    """Put this session's herdr variables into `env` (os.environ) when they are missing; returns how they were found
    ('env', 'tmux', 'session'), or None outside herdr. A recovered session drops ORCA_TERMINAL_HANDLE: it is left
    over from the terminal the session started in. An Orca terminal whose app runs is left alone (Orca on a PC)."""
    env = os.environ if env is None else env
    if in_pane(env):
        return 'env'
    if env.get('HERDR_DISCOVER') == '0':
        return None
    how, ctx = 'tmux', tmux(env)
    if not ctx:
        if env.get('ORCA_TERMINAL_HANDLE') and (orca_up or orca_running)():
            return None
        how, ctx = 'session', sessions(env, cwd or os.getcwd())
    if not ctx:
        return None
    for k in KEYS:
        env.pop(k, None)
    env.update({k: v for k, v in ctx.items() if k in KEYS and v})
    env['HERDR_ENV'] = '1'
    env.pop('ORCA_TERMINAL_HANDLE', None)
    return how


if __name__ == '__main__':
    found = recover()
    print(found or 'not in herdr')
    for k in KEYS:
        if os.environ.get(k):
            print(f'{k}={os.environ[k]}')
