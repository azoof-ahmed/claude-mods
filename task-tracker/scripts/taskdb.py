"""Task tracker core: storage, identity, permissions, conflicts, notifications.

Storage is SQLite in WAL mode.
- A claim is a read-check-write ("is it unassigned? then take it"). BEGIN IMMEDIATE makes that one atomic step across
  processes, so two agents cannot claim the same task.
- WAL lets readers (the board, the Claude Code pane polling every few seconds) read while a writer commits.
- Every change also lands in the `events` table (who, what, when): that table is the history.

Pure Python stdlib (>= 3.9), so any agent or shell uses the same CLI (task.py).
"""
from __future__ import annotations

import datetime as _dt
import difflib
import fnmatch
import json
import os
import re
import shlex
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

import config as _config
import minitoml

STATUSES = ['backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled']
OPEN_STATUSES = ['backlog', 'todo', 'in_progress', 'in_review', 'blocked']
PRIORITIES = ['urgent', 'high', 'med', 'low']
LEVELS = ['admin', 'lead', 'member', 'viewer']

CFG: _config.Config = None  # set by configure()

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'todo',
  priority TEXT NOT NULL DEFAULT 'med',
  assignee TEXT,
  proposed TEXT,
  creator TEXT NOT NULL,
  reviewer TEXT,
  area TEXT,
  parent INTEGER REFERENCES issues(id),
  due TEXT,
  ext_key TEXT UNIQUE,
  import_snapshot TEXT,
  created TEXT NOT NULL,
  updated TEXT NOT NULL,
  closed_at TEXT
);
CREATE TABLE IF NOT EXISTS labels (issue INTEGER NOT NULL, label TEXT NOT NULL, PRIMARY KEY (issue, label));
CREATE TABLE IF NOT EXISTS links (issue INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (issue, kind, value));
CREATE TABLE IF NOT EXISTS touches (issue INTEGER NOT NULL, glob TEXT NOT NULL, PRIMARY KEY (issue, glob));
CREATE TABLE IF NOT EXISTS blocks (issue INTEGER NOT NULL, blocked_by INTEGER NOT NULL,
  PRIMARY KEY (issue, blocked_by));
CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, issue INTEGER NOT NULL,
  author TEXT NOT NULL, ts TEXT NOT NULL, body TEXT NOT NULL, ext_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
  issue INTEGER, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, recipient TEXT NOT NULL,
  sender TEXT NOT NULL, issue INTEGER, prio TEXT NOT NULL DEFAULT 'normal', body TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS ix_issues_assignee ON issues(assignee);
CREATE INDEX IF NOT EXISTS ix_events_issue ON events(issue);
CREATE INDEX IF NOT EXISTS ix_comments_issue ON comments(issue);
CREATE INDEX IF NOT EXISTS ix_notes_recipient ON notes(recipient, seen);
"""

ROLES_TEMPLATE = """\
# task-tracker roles and permissions.
# Levels: admin > lead > member > viewer.
#   admin   everything
#   lead    create, assign anyone, review, move any task, import
#   member  create, claim, assign only themselves, update their own tasks, comment anywhere
#   viewer  read only
# A name not listed below gets `default_level`. Delete this file and everyone may do everything.
# Who you are: --as <name>, else $TASKS_AS, else "user".

default_level = "member"

[levels]
admin = ["user", "coordinator"]
lead = ["reviewer"]
member = ["worker-a"]
viewer = []

[identity]
# Names that are tools, not workers: they are never assigned tasks.
no_assign = []
# true: assigning or reviewing needs a name listed in [levels] (catches typos).
strict_names = false

[review]
# Reviewer set on every new task unless --reviewer is given ("" = none).
default_reviewer = ""
# true: moving a task to in_review needs a proof link (--proof <path or url>).
require_proof = false
"""


class TaskError(Exception):
    """A refusal or a bad input. The message is printed to the caller as is."""


class Denied(TaskError):
    def __init__(self, rule: str, why: str):
        super().__init__(f'DENIED by rule [{rule}]: {why}')
        self.rule = rule


class NoTracker(TaskError):
    pass


def configure(cfg: _config.Config) -> _config.Config:
    global CFG
    CFG = cfg
    return cfg


def cfg() -> _config.Config:
    if CFG is None:
        configure(_config.load())
    return CFG


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec='seconds')


def today() -> str:
    return _dt.date.today().isoformat()


# --------------------------------------------------------------------------------------------- keys

def parse_key(key) -> int:
    if isinstance(key, int):
        return key
    prefix = re.escape(cfg().prefix)
    m = re.fullmatch(rf'(?i)(?:{prefix}-?)?(\d+)', str(key).strip())
    if not m:
        raise TaskError(f'not a task id: {key!r} (expected {cfg().prefix}-<n> or <n>)')
    return int(m.group(1))


def fmt_key(n: Optional[int]) -> str:
    return f'{cfg().prefix}-{n}' if n is not None else ''


# --------------------------------------------------------------------------------------------- db

def connect(path: Optional[Path] = None, create: bool = False) -> sqlite3.Connection:
    path = Path(path or cfg().db_path)
    if not path.exists() and not create:
        raise NoTracker(f'no tracker at {path}. Create it with `task.py init`.')
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA)
    return conn


def init(write_roles: bool = True) -> dict:
    c = cfg()
    existed = c.db_path.exists()
    conn = connect(create=True)
    conn.close()
    roles_written = False
    if write_roles and not c.roles_path.exists():
        c.roles_path.parent.mkdir(parents=True, exist_ok=True)
        c.roles_path.write_text(ROLES_TEMPLATE, encoding='utf-8')
        roles_written = True
    return {'db': str(c.db_path), 'db_created': not existed, 'roles': str(c.roles_path),
            'roles_written': roles_written}


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """One atomic write: BEGIN IMMEDIATE takes the write lock up front, so a check-then-set cannot race."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield conn
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    else:
        conn.execute('COMMIT')


def log_event(conn, actor: str, issue: Optional[int], action: str, **detail):
    conn.execute('INSERT INTO events(ts, actor, issue, action, detail) VALUES (?,?,?,?,?)',
                 (now(), actor, issue, action, json.dumps(detail, ensure_ascii=False)))


# --------------------------------------------------------------------------------------------- roles + identity

_ROLES_CACHE: dict = {}


def load_roles() -> Optional[dict]:
    """The parsed roles file, or None when there is none (everyone may do everything)."""
    p = cfg().roles_path
    try:
        text = p.read_text(encoding='utf-8')
    except OSError:
        return None
    key = (str(p), text)
    if key not in _ROLES_CACHE:
        try:
            _ROLES_CACHE.clear()
            _ROLES_CACHE[key] = minitoml.loads(text)
        except minitoml.TomlError as e:
            raise TaskError(f'{p}: {e}')
    return _ROLES_CACHE[key]


def known_names(roles: Optional[dict] = None) -> set:
    roles = roles if roles is not None else (load_roles() or {})
    out = set()
    for names in (roles.get('levels') or {}).values():
        out.update(names or [])
    return out


def level_of(name: str, roles: Optional[dict] = None) -> str:
    roles = roles if roles is not None else load_roles()
    if roles is None:
        return 'admin'
    levels = roles.get('levels') or {}
    for lvl in LEVELS:
        if name in (levels.get(lvl) or []):
            return lvl
    lvl = roles.get('default_level', 'member')
    if lvl not in LEVELS:
        raise TaskError(f'{cfg().roles_path}: default_level must be one of {", ".join(LEVELS)}')
    return lvl


def review_setting(key: str, default=None):
    return ((load_roles() or {}).get('review') or {}).get(key, default)


class Actor:
    def __init__(self, name: str, level: str, via: str):
        self.name, self.level, self.via = name, level, via

    def __repr__(self):
        return f'{self.name} ({self.level}, via {self.via})'

    @property
    def tag(self) -> str:
        return self.name


def resolve_actor(as_name: Optional[str] = None, env: Optional[dict] = None) -> Actor:
    """Who is calling: --as, else $TASKS_AS, else "user". The roles file gives the level."""
    env = env if env is not None else os.environ
    if as_name:
        name, via = as_name, '--as'
    elif env.get('TASKS_AS'):
        name, via = env['TASKS_AS'], 'TASKS_AS'
    else:
        name, via = 'user', 'default'
    name = name.strip()
    if not re.fullmatch(r'[\w.@-]+', name):
        raise TaskError(f'not a usable name: {name!r} (letters, digits, . _ - @)')
    roles = load_roles()
    return Actor(name, level_of(name, roles), via + ('' if roles is not None else ', no roles file'))


# --------------------------------------------------------------------------------------------- permissions

def can(actor: Actor, action: str, issue: Optional[sqlite3.Row] = None, target: Optional[str] = None):
    """Raise Denied with the refusing rule, or return."""
    lvl = actor.level
    if lvl == 'admin':
        return
    if lvl == 'viewer':
        raise Denied('viewer.read-only', f'{actor.name} is a viewer: read commands only')
    if action == 'import' and lvl != 'lead':
        raise Denied('lead.import', 'only a lead or an admin may import')
    if lvl == 'lead':
        return
    me = actor.name  # member
    if action in ('new', 'comment', 'propose', 'claim'):
        return  # claim: the atomic UPDATE checks the assignee
    if action == 'assign':
        if target != me:
            raise Denied('member.assign-self-only', f'a member assigns only to themselves; use `task.py assign '
                                                    f'<id> {target} --propose` to ask a lead or admin')
        if issue is not None and issue['assignee'] and issue['assignee'] != me:
            raise Denied('member.no-steal', f'{fmt_key(issue["id"])} belongs to {issue["assignee"]}; comment to '
                                            f'them or ask a lead')
        return
    owner = issue['assignee'] if issue is not None else None
    creator = issue['creator'] if issue is not None else None
    if action in ('move', 'edit', 'link', 'block', 'touch'):
        if owner == me or (not owner and creator == me):
            return
        raise Denied('member.own-tasks-only', f'{fmt_key(issue["id"])} is owned by {owner or "nobody"} '
                                              f'(creator {creator}); a member updates only their own tasks')
    if action == 'review':
        if issue is not None and issue['reviewer'] == me:
            return
        raise Denied('member.review', f'only the reviewer ({issue["reviewer"] if issue is not None else "-"}), '
                                      f'a lead or an admin may approve or reject')
    raise Denied('unknown', f'no rule allows {action!r} for a member')


def check_person(name: str):
    roles = load_roles()
    if roles is None or name == 'user':
        return
    if name in ((roles.get('identity') or {}).get('no_assign') or []):
        raise Denied('identity.no-assign', f'{name} is a tool, not a worker')
    if (roles.get('identity') or {}).get('strict_names') and name not in known_names(roles):
        raise TaskError(f'unknown name {name!r}: not listed in {cfg().roles_path}')


def check_labels(labels):
    vocab = cfg().labels
    bad = [lb for lb in labels if vocab and lb not in vocab]
    if bad:
        raise TaskError(f'unknown label(s) {", ".join(bad)}; the vocabulary is {", ".join(vocab)} '
                        f'(set it with the `labels` option, TASKS_LABELS or --labels)')


# --------------------------------------------------------------------------------------------- reads

def get_issue(conn, key) -> sqlite3.Row:
    n = parse_key(key)
    row = conn.execute('SELECT * FROM issues WHERE id=?', (n,)).fetchone()
    if row is None:
        raise TaskError(f'no such task: {fmt_key(n)}')
    return row


def issue_dict(conn, row: sqlite3.Row, full: bool = False) -> dict:
    n = row['id']
    d = {k: row[k] for k in row.keys() if k != 'import_snapshot'}
    d['key'] = fmt_key(n)
    d['parent'] = fmt_key(row['parent']) if row['parent'] else None
    d['labels'] = [r[0] for r in conn.execute('SELECT label FROM labels WHERE issue=? ORDER BY label', (n,))]
    d['links'] = [{'kind': r[0], 'value': r[1]} for r in
                  conn.execute('SELECT kind, value FROM links WHERE issue=? ORDER BY kind, value', (n,))]
    d['touches'] = [r[0] for r in conn.execute('SELECT glob FROM touches WHERE issue=? ORDER BY glob', (n,))]
    d['blocked_by'] = [fmt_key(r[0]) for r in conn.execute('SELECT blocked_by FROM blocks WHERE issue=?', (n,))]
    if full:
        d['blocks'] = [fmt_key(r[0]) for r in conn.execute('SELECT issue FROM blocks WHERE blocked_by=?', (n,))]
        d['children'] = [{'key': fmt_key(r['id']), 'title': r['title'], 'status': r['status'],
                          'assignee': r['assignee']}
                         for r in conn.execute('SELECT id, title, status, assignee FROM issues WHERE parent=? '
                                               'ORDER BY id', (n,))]
        d['comments'] = [dict(r) for r in conn.execute('SELECT author, ts, body FROM comments WHERE issue=? '
                                                       'ORDER BY id', (n,))]
        d['history'] = [{'ts': r['ts'], 'actor': r['actor'], 'action': r['action'],
                         'detail': json.loads(r['detail'])}
                        for r in conn.execute('SELECT * FROM events WHERE issue=? ORDER BY id', (n,))]
    return d


def query(conn, status=None, area=None, assignee=None, priority=None, label=None, text=None,
          include_closed=False, limit=None) -> list:
    sql, args = ['SELECT * FROM issues WHERE 1=1'], []
    if status:
        st = status if isinstance(status, (list, tuple)) else [s.strip() for s in status.split(',')]
        sql.append(f'AND status IN ({",".join("?" * len(st))})')
        args += st
    elif not include_closed:
        sql.append("AND status NOT IN ('done','cancelled')")
    for col, val in (('area', area), ('assignee', assignee), ('priority', priority)):
        if val:
            vals = val.split(',')
            if col == 'assignee' and vals == ['none']:
                sql.append("AND (assignee IS NULL OR assignee='')")
                continue
            sql.append(f'AND {col} IN ({",".join("?" * len(vals))})')
            args += vals
    if label:
        sql.append('AND id IN (SELECT issue FROM labels WHERE label=?)')
        args.append(label)
    if text:
        sql.append('AND (title LIKE ? OR body LIKE ? OR ext_key LIKE ?)')
        args += [f'%{text}%'] * 3
    sql.append("ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'med' THEN 2 ELSE 3 END, id")
    if limit:
        sql.append(f'LIMIT {int(limit)}')
    return conn.execute(' '.join(sql), args).fetchall()


# --------------------------------------------------------------------------------------------- conflicts

def _norm_glob(g: str) -> str:
    g = g.strip().replace('\\', '/')
    while g.startswith('./'):
        g = g[2:]
    if g.endswith('/'):
        g += '**'
    return g.lower()


def _literal_prefix(g: str) -> str:
    m = re.search(r'[*?\[]', g)
    return g if m is None else g[:m.start()]


def globs_overlap(a: str, b: str) -> bool:
    """Conservative: True when some path could match both. False positives are acceptable, misses are not."""
    a, b = _norm_glob(a), _norm_glob(b)
    if a == b or fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    pa, pb = _literal_prefix(a), _literal_prefix(b)
    if pa != a and pb != b:
        return pa.startswith(pb) or pb.startswith(pa)
    return False


def touch_conflicts(conn, issue_id: int, globs: Optional[List[str]] = None) -> list:
    """Other in-progress tasks whose `touches` overlap this task's (or the given) globs."""
    mine = globs if globs is not None else [r[0] for r in
                                            conn.execute('SELECT glob FROM touches WHERE issue=?', (issue_id,))]
    if not mine:
        return []
    out = []
    rows = conn.execute("SELECT i.id, i.title, i.assignee, t.glob FROM issues i JOIN touches t ON t.issue=i.id "
                        "WHERE i.status='in_progress' AND i.id<>?", (issue_id,)).fetchall()
    for r in rows:
        for g in mine:
            if globs_overlap(g, r['glob']):
                out.append({'key': fmt_key(r['id']), 'title': r['title'], 'assignee': r['assignee'],
                            'their_glob': r['glob'], 'my_glob': g})
    return out


def all_conflicts(conn) -> list:
    rows = conn.execute("SELECT i.id, i.title, i.assignee, t.glob FROM issues i JOIN touches t ON t.issue=i.id "
                        "WHERE i.status='in_progress' ORDER BY i.id").fetchall()
    out, seen = [], set()
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a['id'] == b['id'] or (a['id'], b['id']) in seen:
                continue
            if globs_overlap(a['glob'], b['glob']):
                seen.add((a['id'], b['id']))
                out.append({'a': fmt_key(a['id']), 'a_title': a['title'], 'a_assignee': a['assignee'],
                            'a_glob': a['glob'], 'b': fmt_key(b['id']), 'b_title': b['title'],
                            'b_assignee': b['assignee'], 'b_glob': b['glob']})
    return out


# --------------------------------------------------------------------------------------------- duplicates

def _norm_title(t: str) -> str:
    return ' '.join(re.sub(r'[^a-z0-9 ]+', ' ', t.lower()).split())


def find_duplicates(conn, title: str, links=(), exclude: Optional[int] = None) -> list:
    """Open-or-done tasks with the same reference link (any link but proof/doc) or a title >= 85% similar."""
    out = {}
    for kind, value in links:
        if kind in ('proof', 'doc'):
            continue
        for r in conn.execute("SELECT i.id, i.title, i.status FROM links l JOIN issues i ON i.id=l.issue "
                              "WHERE l.kind=? AND l.value=? AND i.status<>'cancelled'", (kind, value)):
            if r['id'] != exclude:
                out[r['id']] = {'key': fmt_key(r['id']), 'title': r['title'], 'status': r['status'],
                                'why': f'same {kind} {value}'}
    want = _norm_title(title)
    for r in conn.execute("SELECT id, title, status FROM issues WHERE status<>'cancelled'"):
        if r['id'] == exclude or r['id'] in out:
            continue
        ratio = difflib.SequenceMatcher(None, want, _norm_title(r['title'])).ratio()
        if ratio >= 0.85:
            out[r['id']] = {'key': fmt_key(r['id']), 'title': r['title'], 'status': r['status'],
                            'why': f'title {ratio:.0%} similar'}
    return list(out.values())


# --------------------------------------------------------------------------------------------- notifications

def _notify_argv(template: str, values: dict) -> list:
    parts = shlex.split(template, posix=os.name != 'nt')
    if os.name == 'nt':
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in '"\'' else p for p in parts]
    return [p.format(**values) for p in parts]


def notify(conn, actor: Actor, to: Optional[str], text: str, prio: str = 'normal', issue: Optional[int] = None):
    """Leave a note in the recipient's inbox (`task.py inbox`), and run the notify command if one is configured.

    The command template takes {to} {sender} {key} {prio} {text}; it runs without a shell, one argv per note.
    """
    if not to or to == actor.name or os.environ.get('TASKS_NO_NOTIFY'):
        return
    with write_tx(conn):
        conn.execute('INSERT INTO notes(ts, recipient, sender, issue, prio, body) VALUES (?,?,?,?,?,?)',
                     (now(), to, actor.name, issue, prio, text))
    tmpl = cfg().notify_command
    if not tmpl:
        return
    try:
        argv = _notify_argv(tmpl, {'to': to, 'sender': actor.name, 'key': fmt_key(issue) if issue else '',
                                   'prio': prio, 'text': text[:580]})
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20, cwd=str(cfg().root))
        ok, err = r.returncode == 0, (r.stderr or r.stdout).strip()[-200:]
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, IndexError) as e:
        ok, err = False, str(e)
    if not ok:
        with write_tx(conn):
            log_event(conn, actor.name, issue, 'notify_failed', to=to, error=err)
