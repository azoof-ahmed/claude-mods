"""The herdr side of agent-bus: a drop-in for orca-bus's `Orca` terminal wrapper, so the deliver daemon, `send --now`,
`who` and the web chat's liveness work in herdr panes exactly as they do in Orca tabs.

A terminal handle is a herdr pane id (w1:p3, what HERDR_PANE_ID holds inside the pane). bus.py swaps this class in
for `orca_bus.orca.Orca` when the backend is herdr; every caller imports `Orca` from that module lazily.
"""
import json
import os
import shutil
import subprocess

# orca-bus reads an agent's state from its terminal title (vendor/orca_bus/tui.py): U+2733 when idle, a spinner glyph
# while working. herdr reports the title the agent set, and its own reading of the agent's state; when the title
# carries neither glyph (an agent inside tmux, whose title does not reach herdr) the state stands in for it.
IDLE_TITLE = '✳'
BUSY_TITLE = '◐'
TITLE_GLYPHS = set('✳◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
# raw keys orca-bus types: herdr sends these as named keys
KEYS = {'\r': 'enter', '\n': 'enter', '\t': 'tab', '\x1b': 'esc'}


def find_herdr():
    for c in (os.environ.get('AGENT_BUS_HERDR'), shutil.which('herdr'), os.environ.get('HERDR_BIN_PATH')):
        if c and os.path.exists(c):
            return c
    raise RuntimeError('herdr CLI not found: set AGENT_BUS_HERDR to its path')


class HerdrError(RuntimeError):
    pass


def title_for(p):
    """The title orca-bus reads for pane info `p` (see TITLE_GLYPHS)."""
    t = (p.get('terminal_title') or p.get('title') or '').strip()
    if t[:1] in TITLE_GLYPHS or not p.get('agent'):
        return t
    st = p.get('agent_status')
    if st in ('idle', 'done'):
        return f'{IDLE_TITLE} {t}'.strip()
    if st == 'working':
        return f'{BUSY_TITLE} {t}'.strip()
    return t


class Herdr:
    def __init__(self, exe=None, timeout=60):
        self.exe = exe or find_herdr()
        self.timeout = timeout

    def _run(self, *args):
        return subprocess.run([self.exe, *args], capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=self.timeout)

    def _json(self, *args):
        r = self._run(*args)
        try:
            d = json.loads(r.stdout)
        except ValueError:
            raise HerdrError(f'herdr {args[:2]} rc={r.returncode}: {(r.stdout + r.stderr)[-300:]}')
        if not isinstance(d, dict) or d.get('error') or not isinstance(d.get('result'), dict):
            raise HerdrError(f'herdr {args[:2]}: {json.dumps(d.get("error", d) if isinstance(d, dict) else d)[:300]}')
        return d['result']

    def terminals(self):
        """{pane id: {'title', 'agent', 'cwd'}} for every live pane."""
        out = {}
        for p in self._json('pane', 'list').get('panes') or []:
            if isinstance(p, dict) and p.get('pane_id'):
                out[p['pane_id']] = {'title': title_for(p), 'agent': p.get('agent'),
                                     'cwd': p.get('foreground_cwd') or p.get('cwd')}
        return out

    def _read(self, handle, *args):
        r = self._run('pane', 'read', handle, *args)
        if r.returncode != 0:
            raise HerdrError(f'herdr pane read {handle}: {(r.stderr or r.stdout).strip()[-300:]}')
        return r.stdout.rstrip('\n').split('\n')

    def screen(self, handle):
        """(lines, draft): the visible screen. herdr reports no composer text, so draft is None and orca-bus reads
        the input box from the screen itself."""
        return self._read(handle, '--source', 'visible'), None

    def scrollback(self, handle, limit=400):
        return self._read(handle, '--source', 'recent', '--lines', str(limit))

    def send(self, handle, text):
        """Type text (or a raw key such as '\\r') into a pane. Never presses Enter itself."""
        key = KEYS.get(text)
        r = self._run('pane', 'send-keys', handle, key) if key else self._run('pane', 'send-text', handle, text)
        return r.returncode == 0
