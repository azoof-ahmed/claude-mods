"""`task.py import`: seed or refresh tasks from your own sources. Idempotent.

The `importers` option lists the sources, comma-separated. Each entry is one of:
  - a Python file (ends in .py, no spaces, relative to the project root): it defines `records(ctx)`;
  - a Python module name (dotted, importable with the project root on sys.path): same `records(ctx)`;
  - anything else is a command, run in the project root without a shell; its stdout is JSON: one array of records,
    or one record per line.

`ctx` is a dict: {"root": <project root>, "prefix": <id prefix>, "labels": [...]}.

A record is a dict:
  ext_key   required, stable and unique per source item (e.g. "issues:42"); the tracker matches on it
  title     required
  body, status, priority, assignee, reviewer, area, due     optional fields
  parent    the ext_key of another imported record
  labels    ["bug", ...]            added, never removed
  links     ["url:https://...", {"kind": "ref", "value": "x"}]   added, never removed
  touches   ["src/**"]              added, never removed
  comments  ["text", {"author": "bot", "body": "text"}]   each added once (keyed by its text)

Each imported task keeps an import snapshot: the source values at the last import. A re-import changes a field only
while the tracker still holds the snapshot value, so a change made IN the tracker (a claim, a move) is never
overwritten by a stale source.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from taskdb import PRIORITIES, STATUSES, TaskError, cfg, log_event, now, write_tx

FIELDS = ('title', 'body', 'status', 'priority', 'assignee', 'reviewer', 'area', 'due', 'parent')


def _ctx() -> dict:
    c = cfg()
    return {'root': str(c.root), 'prefix': c.prefix, 'labels': list(c.labels)}


def _load_records(entry: str) -> list:
    root = cfg().root
    if entry.endswith('.py') and not re.search(r'\s', entry):
        path = Path(entry)
        path = path if path.is_absolute() else root / path
        if not path.exists():
            raise TaskError(f'importer {entry}: no such file {path}')
        spec = importlib.util.spec_from_file_location(f'task_importer_{abs(hash(str(path)))}', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return _call(entry, mod)
    if re.fullmatch(r'[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*', entry):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            mod = importlib.import_module(entry)
        except ImportError as e:
            raise TaskError(f'importer {entry}: {e}')
        return _call(entry, mod)
    argv = shlex.split(entry, posix=os.name != 'nt')
    if os.name == 'nt':
        argv = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in '"\'' else p for p in argv]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=str(root),
                           env={**os.environ, 'TASKS_IMPORT_CTX': json.dumps(_ctx())})
    except (OSError, subprocess.SubprocessError) as e:
        raise TaskError(f'importer {entry}: {e}')
    if r.returncode != 0:
        raise TaskError(f'importer {entry} exited {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}')
    out = r.stdout.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except ValueError:
        try:
            return [json.loads(ln) for ln in out.splitlines() if ln.strip()]
        except ValueError as e:
            raise TaskError(f'importer {entry}: stdout is not JSON ({e})')


def _call(entry, mod):
    fn = getattr(mod, 'records', None)
    if not callable(fn):
        raise TaskError(f'importer {entry}: defines no records(ctx) function')
    return list(fn(_ctx()) or [])


class Importer:
    def __init__(self, conn, actor, dry=False):
        self.conn, self.actor, self.dry = conn, actor, dry
        self.stats = {}

    def _bump(self, src, what):
        self.stats.setdefault(src, {}).setdefault(what, 0)
        self.stats[src][what] += 1

    def upsert(self, src, rec: dict):
        if not isinstance(rec, dict) or not rec.get('ext_key') or not rec.get('title'):
            raise TaskError(f'importer {src}: every record needs ext_key and title (got {str(rec)[:120]})')
        ext_key = str(rec['ext_key'])
        fields = {k: rec[k] for k in FIELDS if k in rec}
        if fields.get('status') not in (None, *STATUSES):
            raise TaskError(f'importer {src}: {ext_key}: status {fields["status"]!r} is not one of {STATUSES}')
        if fields.get('priority') not in (None, *PRIORITIES):
            raise TaskError(f'importer {src}: {ext_key}: priority {fields["priority"]!r} is not one of {PRIORITIES}')
        c = self.conn
        if fields.get('parent'):
            p = c.execute('SELECT id FROM issues WHERE ext_key=?', (str(fields['parent']),)).fetchone()
            fields['parent'] = p[0] if p else None
        row = c.execute('SELECT * FROM issues WHERE ext_key=?', (ext_key,)).fetchone()
        ts = now()
        if row is None:
            if self.dry:
                self._bump(src, 'new')
                return None
            fields.setdefault('status', 'backlog')
            cols = ['ext_key', 'import_snapshot', 'creator', 'created', 'updated', 'closed_at'] + list(fields)
            vals = [ext_key, json.dumps(fields), 'import', ts, ts, None] + list(fields.values())
            n = c.execute(f'INSERT INTO issues({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                          vals).lastrowid
            log_event(c, self.actor.tag, n, 'import', source=src, ext_key=ext_key)
            self._bump(src, 'new')
        else:
            n = row['id']
            snap = json.loads(row['import_snapshot'] or '{}')
            changed = {}
            for k, v in fields.items():
                if row[k] != v and row[k] == snap.get(k, row[k]):
                    changed[k] = {'from': row[k] if k != 'body' else '...', 'to': v if k != 'body' else '...'}
            kept = [k for k, v in fields.items() if row[k] != v and k not in changed]
            if self.dry:
                self._bump(src, 'update' if changed else 'same')
                return n
            for k in changed:
                c.execute(f'UPDATE issues SET {k}=?, updated=? WHERE id=?', (fields[k], ts, n))
            if 'status' in changed:
                c.execute('UPDATE issues SET closed_at=? WHERE id=?',
                          (ts if fields['status'] in ('done', 'cancelled') else None, n))
            c.execute('UPDATE issues SET import_snapshot=? WHERE id=?', (json.dumps(fields), n))
            if changed:
                log_event(c, self.actor.tag, n, 'import_update', source=src, **changed)
            self._bump(src, 'update' if changed else 'same')
            if kept:
                self._bump(src, 'kept-tracker-value')
        for lb in rec.get('labels') or []:
            c.execute('INSERT OR IGNORE INTO labels VALUES (?,?)', (n, str(lb)))
        for x in rec.get('links') or []:
            kind, value = (x.get('kind'), x.get('value')) if isinstance(x, dict) else str(x).split(':', 1)
            c.execute('INSERT OR IGNORE INTO links VALUES (?,?,?)', (n, kind, str(value)))
        for g in rec.get('touches') or []:
            c.execute('INSERT OR IGNORE INTO touches VALUES (?,?)', (n, str(g)))
        for cm in rec.get('comments') or []:
            author, body = (cm.get('author', 'import'), cm.get('body', '')) if isinstance(cm, dict) else ('import', cm)
            h = 'import:' + hashlib.sha1(f'{ext_key}\n{body}'.encode()).hexdigest()[:16]
            c.execute('INSERT OR IGNORE INTO comments(issue, author, ts, body, ext_key) VALUES (?,?,?,?,?)',
                      (n, author, ts, body, h))
        return n


def run_all(conn, actor, only=None, dry=False) -> dict:
    entries = cfg().importers
    if not entries:
        raise TaskError('no importers configured: set the `importers` option, TASKS_IMPORTERS or --importers '
                        '(comma-separated .py files, module names or commands; see importers.py)')
    if only:
        want = [x.strip() for x in only.split(',')]
        entries = [e for e in entries if e in want]
    loaded = [(e, _load_records(e)) for e in entries]  # read every source before taking the write lock
    imp = Importer(conn, actor, dry)
    with write_tx(conn):
        for entry, recs in loaded:
            imp.stats.setdefault(entry, {})
            for rec in recs:
                imp.upsert(entry, rec)
        if not dry:  # a parent listed after its child resolves now
            for _, recs in loaded:
                for rec in recs:
                    if rec.get('parent'):
                        conn.execute('UPDATE issues SET parent=(SELECT id FROM issues WHERE ext_key=?) '
                                     'WHERE ext_key=? AND parent IS NULL', (str(rec['parent']), str(rec['ext_key'])))
            log_event(conn, actor.tag, None, 'import_run', sources=entries, stats=imp.stats)
    return imp.stats
