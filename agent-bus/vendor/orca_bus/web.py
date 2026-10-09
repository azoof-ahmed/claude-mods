"""bus-chat: a localhost web view of the bus as a group chat (stdlib only: http.server + Server-Sent Events).

    python -m orca_bus.web --dir <bus dir> [--port 8790] [--tasks-db path/to/tasks.db]

Binds 127.0.0.1 only. The page is one file (chat.html next to this module). The index tails the append-only files
(log.jsonl, chat.jsonl, inbox/*.seen) from saved offsets: nothing is reread on an event.
The browser posts as the role `user`; routing is the bus's own (store.send(route=True)).
"""
import argparse
import json
import os
import queue
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import chat
from .store import Bus, BusError

HERE = Path(__file__).resolve().parent
ME = 'user'
WORKING = set('◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')


class Tail:
    """Complete new lines of an append-only file since the last call."""

    def __init__(self, path, pos=0):
        self.path, self.pos = Path(path), pos

    def lines(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.pos:  # truncated / replaced: start over
            self.pos = 0
        if size == self.pos:
            return []
        with open(self.path, 'rb') as f:
            f.seek(self.pos)
            data = f.read(size - self.pos)
        cut = data.rfind(b'\n') + 1  # a torn last line waits for its newline
        self.pos += cut
        return data[:cut].splitlines()


class Index:
    def __init__(self, bus):
        self.bus = bus
        self.lock = threading.Lock()
        self.groups, self.rid = {}, {}          # base -> group; per-recipient id -> base
        self.seen = {}                          # name -> set of seen ids
        self.rx, self.pins, self.edits, self.deleted, self.subs = {}, {}, {}, set(), {}
        self.log = Tail(bus.log_path)
        self.chat = Tail(chat.chat_path(bus))
        self.seen_tails = {}
        self.reg, self.reg_mtime = {}, 0
        self.version = 0
        self.poll()

    # ---- ingest
    def _msg(self, ev, changed):
        rid, base = ev['id'], chat.base_id(ev['id'])
        g = self.groups.get(base)
        if g is None:
            g = {'id': base, 'from': ev['from'], 'time': ev['time'], 'text': ev.get('text', ''),
                 'body': ev.get('body'), 'prio': ev.get('prio', 'normal'), 'topic': ev.get('topic'),
                 'chan': ev.get('chan'), 're': chat.base_id(ev['reply_to']) if ev.get('reply_to') else None,
                 'thread': ev.get('thread'), 'rc': {}, 'last': ev['time']}
            if not g['thread'] and g['re'] in self.groups:  # pre-chat replies: thread from the parent
                p = self.groups[g['re']]
                g['thread'] = p['thread'] or p['id']
            self.groups[base] = g
        g['rc'][ev['to']] = {'rid': rid, 'why': ev.get('rcpt', 'to'), 'st': ev.get('state', 'queued'), 'd': ''}
        self.rid[rid] = base
        changed.add(base)
        root = self.groups.get(g['thread']) if g['thread'] else None
        if root is not None and g['time'] > root['last']:
            root['last'] = g['time']
            changed.add(root['id'])

    def _state(self, ev, changed):
        base = self.rid.get(ev.get('id'))
        if base is None:
            return
        g = self.groups[base]
        for r in g['rc'].values():
            if r['rid'] == ev['id']:
                r['st'], r['d'] = ev['state'], ev.get('detail', '')
                changed.add(base)

    def _chat(self, ev, changed):
        k, i = ev.get('ev'), ev.get('id')
        if k == 'react':
            who = self.rx.setdefault(i, {}).setdefault(ev['emoji'], [])
            if ev.get('on', True) and ev['by'] not in who:
                who.append(ev['by'])
            elif not ev.get('on', True) and ev['by'] in who:
                who.remove(ev['by'])
        elif k == 'pin':
            if ev.get('on', True):
                self.pins[i] = ev['by']
            else:
                self.pins.pop(i, None)
        elif k == 'edit':
            self.edits[i] = ev['text']
        elif k == 'delete':
            self.deleted.add(i)
        elif k == 'sub':
            s = self.subs.setdefault(ev['thread'], set())
            (s.add if ev.get('on', True) else s.discard)(ev['name'])
            i = ev['thread']
        if i in self.groups:
            changed.add(i)

    def poll(self):
        """Read what was appended since the last poll; return the changed group ids."""
        changed = set()
        with self.lock:
            for line in self.log.lines():
                if b'"ev": "msg"' not in line and b'"ev": "state"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                (self._msg if ev['ev'] == 'msg' else self._state)(ev, changed)
            for line in self.chat.lines():
                try:
                    self._chat(json.loads(line), changed)
                except (ValueError, KeyError):
                    pass
            try:
                with os.scandir(self.bus.root / 'inbox') as it:
                    seen_files = [e for e in it if e.name.endswith('.seen')]
            except OSError:
                seen_files = []
            for e in seen_files:
                name = e.name[:-5]
                t = self.seen_tails.setdefault(name, Tail(e.path))
                ids = [l.decode('utf-8', 'replace').strip() for l in t.lines()]
                if ids:
                    self.seen.setdefault(name, set()).update(ids)
                    changed.update(self.rid[i] for i in ids if i in self.rid)
            try:
                m = self.bus.reg_path.stat().st_mtime
                if m != self.reg_mtime:
                    self.reg, self.reg_mtime = self.bus.registry(), m
            except OSError:
                pass
            if changed:
                self.version += 1
        return changed

    # ---- views
    def view(self, base):
        g = self.groups[base]
        seen = self.seen
        rc = []
        for to, r in g['rc'].items():
            if to.startswith('#'):
                continue
            rc.append({'to': to, 'why': r['why'],
                       'r': chat.classify(r['st'], r['d'], r['rid'] in seen.get(to, ())), 'd': r['d'][:160]})
        out = {k: g[k] for k in ('id', 'from', 'time', 'prio', 'topic', 'chan', 're', 'thread', 'last')}
        out.update(text=self.edits.get(base, g['text']), edited=base in self.edits, deleted=base in self.deleted,
                   body=bool(g['body']), rc=rc, rx=self.rx.get(base, {}), pin=base in self.pins,
                   subs=sorted(self.subs.get(base, ())))
        if out['deleted']:
            out['text'], out['body'] = '', False
        return out

    def views(self, bases):
        with self.lock:
            return [self.view(b) for b in bases if b in self.groups]

    def recent(self, limit=600, before=None):
        """The newest `limit` threads by last activity (with all their replies), optionally before a time."""
        with self.lock:
            roots = [g for g in self.groups.values() if not g['thread'] or g['thread'] not in self.groups]
            if before:
                roots = [g for g in roots if g['last'] < before]
            roots.sort(key=lambda g: g['last'])
            roots = roots[-limit:]
            keep = {g['id'] for g in roots}
            ids = [g['id'] for g in self.groups.values() if g['id'] in keep or g['thread'] in keep]
            return [self.view(b) for b in ids], len(roots) == limit

    def unread(self, name):
        s = self.seen.get(name, ())
        n = 0
        for g in self.groups.values():
            r = g['rc'].get(name)
            if r and r['rid'] not in s and g['prio'] != 'fyi' and r['st'] not in ('acked', 'failed'):
                n += 1
        return n

    def body(self, base):
        g = self.groups.get(base)
        if not g or not g['body'] or base in self.deleted:
            return None
        p = Path(g['body'])
        try:
            p.resolve().relative_to((self.bus.root / 'bodies').resolve())
        except ValueError:
            return None
        try:
            return p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return None


class Lanes:
    """Who is live (registry + Orca), working (spinner in the tab title), their current task, unread counts."""

    def __init__(self, index, tasks_db=None, every=15):
        self.index, self.tasks_db, self.every = index, tasks_db, every
        self.terms, self.terms_at, self.err = None, 0, None
        self.cache = []

    def _terms(self):
        if time.time() - self.terms_at < self.every:
            return self.terms
        self.terms_at = time.time()
        try:
            from .orca import Orca
            self.terms, self.err = Orca(timeout=10).terminals(), None
        except Exception as ex:  # Orca not reachable: liveness unknown, everything else still shown
            self.terms, self.err = None, str(ex)[:200]
        return self.terms

    def _tasks(self):
        if not self.tasks_db or not Path(self.tasks_db).exists():
            return {}
        try:
            con = sqlite3.connect(f'file:{Path(self.tasks_db).as_posix()}?mode=ro', uri=True, timeout=2)
            rows = con.execute("select assignee, id, title, status from issues where status='in_progress' "
                               "order by updated desc").fetchall()
            con.close()
        except sqlite3.Error:
            return {}
        out = {}
        for a, i, t, s in rows:
            out.setdefault(a, []).append({'id': i, 'title': t, 'status': s})
        return out

    def snapshot(self):
        terms, tasks, idx = self._terms(), self._tasks(), self.index
        last_from = {}
        with idx.lock:
            reg = dict(idx.reg)
            for g in idx.groups.values():
                if g['time'] > last_from.get(g['from'], ''):
                    last_from[g['from']] = g['time']
            unread = {n: idx.unread(n) for n in reg}
        rows = []
        for n, e in sorted(reg.items()):
            h = e.get('handle')
            t = (terms or {}).get(h) if h else None
            title = (t or {}).get('title', '')
            live = None if terms is None or not h else t is not None
            rows.append({'name': n, 'kind': e.get('kind'), 'mode': e.get('mode'), 'live': live,
                         'working': bool(title) and title[0] in WORKING, 'title': title.lstrip(''.join(WORKING) + '✳ ')[:80],
                         'tasks': tasks.get(n, [])[:3], 'unread': unread.get(n, 0), 'last': last_from.get(n)})
        self.cache = rows
        return {'lanes': rows, 'orca': self.err}

    def task(self, tid):
        if not self.tasks_db:
            return None
        try:
            con = sqlite3.connect(f'file:{Path(self.tasks_db).as_posix()}?mode=ro', uri=True, timeout=2)
            # agent-bus: the task-tracker plugin has no `kind`; its `area` fills that chip
            r = con.execute("select id, title, status, assignee, priority, coalesce(area, '') from issues where id=?",
                            (int(tid),)).fetchone()
            con.close()
        except (sqlite3.Error, ValueError):
            return None
        return r and dict(zip(('id', 'title', 'status', 'assignee', 'priority', 'kind'), r))


class Hub:
    def __init__(self):
        self.clients, self.lock = set(), threading.Lock()

    def add(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.add(q)
        return q

    def drop(self, q):
        with self.lock:
            self.clients.discard(q)

    def push(self, event, data):
        payload = f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8')
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(payload)
                except queue.Full:  # a stalled tab: drop it, its EventSource reconnects and refetches
                    self.clients.discard(q)


def task_prefix():
    """The tracker's id prefix shown as task chips (agent-bus: from AGENT_BUS_TASK_PREFIX, default TASK)."""
    p = os.environ.get('AGENT_BUS_TASK_PREFIX') or 'TASK'
    return re.sub(r'[^A-Za-z0-9_]', '', p) or 'TASK'


def make_handler(app):
    page = HERE / 'chat.html'
    prefix = task_prefix().encode()

    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            pass

        def _host_ok(self):
            host = (self.headers.get('Host') or '').split(':')[0]
            return host in ('127.0.0.1', 'localhost')

        def _send(self, code, obj=None, ctype='application/json', raw=None):
            data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {'error': 'host'})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ('/', '/index.html'):
                return self._send(200, ctype='text/html', raw=page.read_bytes().replace(b'__TASK_PREFIX__', prefix))
            if u.path == '/api/init':
                ms, more = app.index.recent(int(q.get('limit', 600)))
                return self._send(200, {'me': ME, 'admins': list(chat.admins(app.bus)), 'msgs': ms, 'more': more,
                                        'lanes': app.lanes.cache or app.lanes.snapshot()['lanes'],
                                        'edit_window': chat.EDIT_WINDOW_S})
            if u.path == '/api/older':
                ms, more = app.index.recent(int(q.get('limit', 400)), q.get('before'))
                return self._send(200, {'msgs': ms, 'more': more})
            if u.path == '/api/body':
                b = app.index.body(q.get('id', ''))
                return self._send(200 if b is not None else 404, {'body': b})
            if u.path == '/api/task':
                return self._send(200, app.lanes.task(q.get('id', '0')) or {})
            if u.path == '/api/lanes':
                return self._send(200, app.lanes.snapshot())
            if u.path == '/api/events':
                return self._sse()
            self._send(404, {'error': 'not found'})

        def _sse(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            q = app.hub.add()
            try:
                self.wfile.write(b'retry: 2000\n\n')
                self.wfile.flush()
                while not app.stopping:
                    try:
                        data = q.get(timeout=15)
                    except queue.Empty:
                        data = b': ping\n\n'
                    self.wfile.write(data)
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass
            finally:
                app.hub.drop(q)
                self.close_connection = True

        def do_POST(self):
            # a custom header forces a CORS preflight, which this server never grants: other sites cannot post here
            if not self._host_ok() or self.headers.get('X-Bus-Chat') != '1':
                return self._send(403, {'error': 'forbidden'})
            try:
                n = int(self.headers.get('Content-Length') or 0)
                d = json.loads(self.rfile.read(min(n, 1 << 20)) or b'{}')
                res = app.post(urlparse(self.path).path, d)
            except BusError as ex:
                return self._send(400, {'error': str(ex)})
            except (ValueError, KeyError, TypeError) as ex:
                return self._send(400, {'error': f'bad request: {ex}'})
            app.wake.set()
            self._send(200, res)

    return H


class App:
    def __init__(self, root, tasks_db=None):
        self.bus = Bus(root)
        self.index = Index(self.bus)
        self.lanes = Lanes(self.index, tasks_db)
        self.hub = Hub()
        self.wake = threading.Event()
        self.stopping = False

    def ensure_user(self):
        if ME not in self.bus.registry():
            self.bus.register(ME, None, 'shell', note='the human, via bus-chat', mode='inbox')

    def post(self, path, d):
        b = self.bus
        if path == '/api/send':
            text = (d.get('text') or '').strip()
            if not text:
                raise BusError('empty message')
            to = [t for t in d.get('to') or [] if t] or [f'#{d.get("chan") or "all"}']
            first = text.splitlines()[0]
            body = text if (text != first or not text.isascii() or len(text) > 600) else None
            if body is not None:
                first = first.encode('ascii', 'replace').decode()[:200] or '(see body)'
            prio = 'urgent' if d.get('urgent') else (d.get('prio') if d.get('prio') in ('normal', 'fyi') else 'normal')
            ms = b.send(ME, to, first, d.get('re') or None, cc=d.get('cc') or [], prio=prio,
                        topic=(d.get('topic') or None), body=body, route=True)
            ms = ms if isinstance(ms, list) else [ms]
            return {'id': chat.base_id(ms[0]['id']), 'to': [m['to'] for m in ms]}
        if path == '/api/react':
            return chat.react(b, ME, d['id'], d['emoji'], on=d.get('on', True))
        if path == '/api/pin':
            return chat.pin(b, ME, d['id'], on=d.get('on', True))
        if path == '/api/sub':
            return chat.subscribe(b, ME, d['id'], on=d.get('on', True))
        if path == '/api/edit':
            return chat.edit(b, ME, d['id'], d['text'])
        if path == '/api/delete':
            return chat.delete(b, ME, d['id'])
        if path == '/api/seen':
            with self.index.lock:  # group ids in, the user's own per-recipient ids out
                s = self.index.seen.get(ME, set())
                rcs = [self.index.groups[i]['rc'].get(ME) for i in d.get('ids', []) if i in self.index.groups]
                ids = [r['rid'] for r in rcs if r and r['rid'] not in s]
            b.mark_seen(ME, ids)
            return {'marked': len(ids)}
        raise BusError(f'unknown action {path}')

    def loop(self, interval=1.0, idle_interval=5.0, lanes_every=15):
        last_lanes = 0
        while not self.stopping:
            changed = self.index.poll()
            if changed:
                self.hub.push('msgs', self.index.views(changed))
            if self.hub.clients and time.time() - last_lanes > lanes_every:
                last_lanes = time.time()
                self.hub.push('lanes', self.lanes.snapshot())
            self.wake.wait(interval if self.hub.clients else idle_interval)
            self.wake.clear()


def serve(root, port=8790, tasks_db=None, host='127.0.0.1'):
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit('bus-chat binds to localhost only')
    app = App(root, tasks_db)
    app.ensure_user()
    t = threading.Thread(target=app.loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    srv.daemon_threads = True
    n = len(app.index.groups)
    print(f'bus-chat on http://{host}:{port}  ({n} messages indexed from {app.bus.root})', flush=True)
    try:
        srv.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping = True
        srv.server_close()


def main(argv=None, root=None, tasks_db=None):
    p = argparse.ArgumentParser(prog='bus-chat')
    p.add_argument('--dir', default=root, required=root is None)
    p.add_argument('--port', type=int, default=int(os.environ.get('BUS_CHAT_PORT', 8790)))
    p.add_argument('--tasks-db', default=tasks_db)
    a = p.parse_args(argv)
    serve(a.dir, a.port, a.tasks_db)


if __name__ == '__main__':
    main()
