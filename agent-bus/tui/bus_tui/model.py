"""Bus state for the terminal client: the web chat's incremental Index plus the views the client needs.

Reads go through orca_bus.web.Index (tails log.jsonl / chat.jsonl / inbox/*.seen from saved offsets).
Writes go through the same store/chat functions the CLI and the web chat use; nothing here writes bus files.
"""
import json
import os
import time
from pathlib import Path

from orca_bus import chat
from orca_bus.store import Bus, BusError
from orca_bus.web import Index

REACTIONS = ('✅', '👀', '👍', '❌', '🙏', '🎉')
PRIOS = ('normal', 'urgent', 'fyi')
WATCH_STALE_S = 90  # the plugin's watch writes inbox/<role>.watch.json every 30 s; older than this = dead


def fuzzy_score(query, text):
    """Subsequence match score (higher is better), or None when `query` is not a subsequence of `text`."""
    q, t = query.lower().strip(), text.lower()
    if not q:
        return 0
    score, pos, streak = 0, 0, 0
    for word in q.split():
        if word in t:  # whole-word hits rank first
            score += 10 * len(word)
            continue
        for ch in word:
            i = t.find(ch, pos)
            if i < 0:
                return None
            streak = streak + 1 if i == pos else 0
            score += 1 + 2 * streak
            pos = i + 1
    return score


def age_text(seconds):
    if seconds is None:
        return '-'
    s = int(max(0, seconds))
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m'
    return f'{s // 3600}h'


class Model:
    def __init__(self, root, role):
        self.bus = Bus(root)
        self.me = role
        self.registered = self.ensure_role()
        self.index = Index(self.bus)
        self.msgs = {}
        views, _ = self.index.recent(limit=1 << 30)
        for m in views:
            self.msgs[m['id']] = m

    def ensure_role(self):
        """The web chat registers `user` on first use; mirror it. Other roles must already be registered."""
        reg = self.bus.registry()
        if self.me in reg:
            return True
        if self.me == 'user':
            self.bus.register('user', None, 'shell', note='the human, via bus-chat', mode='inbox')
            return True
        return False

    # ---- live updates
    def poll(self):
        """Ingest what was appended since the last poll: (changed ids, new ids)."""
        changed = self.index.poll()
        new = []
        for v in self.index.views(changed):
            if v['id'] not in self.msgs:
                new.append(v['id'])
            self.msgs[v['id']] = v
        return changed, new

    # ---- message classification (same rules as chat.html)
    def to_me(self, m):
        return m['from'] != self.me and any(r['to'] == self.me and r['why'] != 'post' for r in m['rc'])

    def unread_me(self, m):
        return m['from'] != self.me and any(r['to'] == self.me and r['r'] != 'seen' for r in m['rc'])

    def mine(self, m):
        return m['from'] == self.me or self.to_me(m)

    def notice(self, m):
        """Why a new message deserves a toast for this role: 'mention', 'reply', or None."""
        if m['from'] == self.me:
            return None
        why = {r['to']: r['why'] for r in m['rc']}.get(self.me)
        if why in ('mention', 'all'):
            return 'mention'
        if why == 'reply':
            return 'reply'
        if self.me in chat.mentions(m.get('text', ''), {self.me: 1}):
            return 'mention'
        parent = self.msgs.get(m.get('re') or '')
        if parent and parent['from'] == self.me:
            return 'reply'
        return None

    @staticmethod
    def chan(m):
        return m.get('topic') or 'all'

    def root_of(self, m):
        t = m.get('thread')
        return t if t and t in self.msgs else m['id']

    def roots(self):
        out = [m for m in self.msgs.values() if self.root_of(m) == m['id']]
        return sorted(out, key=lambda m: (m['last'], m['time']))

    def replies(self, root):
        out = [m for m in self.msgs.values() if m['id'] != root and self.root_of(m) == root]
        return sorted(out, key=lambda m: m['time'])

    def channels(self):
        counts = {}
        for m in self.msgs.values():
            c = self.chan(m)
            if c != 'all':
                counts[c] = counts.get(c, 0) + 1
        return sorted(counts, key=lambda c: (-counts[c], c))

    def roles(self):
        return sorted(self.index.reg)

    def unread_count(self, name=None):
        return self.index.unread(name or self.me)

    def matches(self, m, f):
        """The web chat's filter: f = {chan, role, mentions, unread, q}."""
        if f.get('chan') and self.chan(m) != f['chan']:
            return False
        if f.get('role') and m['from'] != f['role'] and not any(r['to'] == f['role'] for r in m['rc']):
            return False
        if f.get('mentions') and not self.to_me(m):
            return False
        if f.get('unread') and not self.unread_me(m):
            return False
        if f.get('q'):
            hay = ' '.join([m['text'], m['from'], ' '.join(r['to'] for r in m['rc']), m.get('topic') or '',
                            m['id']]).lower()
            if not all(w in hay for w in f['q'].lower().split()):
                return False
        return True

    def rows(self, view, f=None):
        """The flat list for a view: [(message, depth)], threads by last activity, replies under their root.

        view: 'all' | 'mine' | '#<chan>' | 'thread:<root id>'."""
        f = dict(f or {})
        if view == 'mine':
            f['mentions_or_from'] = True
        elif view.startswith('#'):
            f['chan'] = view[1:]
        if view.startswith('thread:'):
            root = view[7:]
            if root not in self.msgs:
                return []
            return [(self.msgs[root], 0)] + [(m, 1) for m in self.replies(root)]

        def ok(m):
            if f.get('mentions_or_from') and not self.mine(m):
                return False
            return self.matches(m, f)

        out = []
        for r in self.roots():
            reps = self.replies(r['id'])
            if ok(r) or any(ok(x) for x in reps):
                out.append((r, 0))
                out.extend((x, 1) for x in reps)
        return out

    def body(self, mid):
        return self.index.body(mid)

    # ---- receipts, as `orca-bus status <id>` computes them
    def receipts(self, mid):
        cache, rows = {}, []
        for r in chat.records(self.bus, mid):
            tick, detail = chat.receipt(self.bus, r, cache)
            rows.append({'id': r['id'], 'to': r['to'], 'rcpt': r.get('rcpt', 'to'), 'receipt': tick,
                         'tick': chat.TICKS.get(tick, '?'), 'detail': detail, 'since': r['history'][-1][0]})
        return rows

    # ---- bus health for the statusline
    def health(self, now=None):
        now = now or time.time()
        root = self.bus.root
        out = {'deliver': None, 'log': None, 'watches': 0, 'my_watch': False}
        try:
            out['deliver'] = now - os.stat(root / 'deliver.heartbeat').st_mtime
        except OSError:
            pass
        try:
            out['log'] = now - os.stat(self.bus.log_path).st_mtime
        except OSError:
            pass
        try:
            with os.scandir(root / 'inbox') as it:
                files = [e for e in it if e.name.endswith('.watch.json')]
        except OSError:
            files = []
        for e in files:
            try:
                beat = float(json.loads(Path(e.path).read_text(encoding='utf-8')).get('beat', 0))
            except (OSError, ValueError, TypeError):
                continue
            if now - beat < WATCH_STALE_S:
                out['watches'] += 1
                if e.name == f'{self.me}.watch.json':
                    out['my_watch'] = True
        return out

    def watch_live(self, name, now=None):
        try:
            hb = json.loads((self.bus.root / 'inbox' / f'{name}.watch.json').read_text(encoding='utf-8'))
            return (now or time.time()) - float(hb.get('beat', 0)) < WATCH_STALE_S
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    # ---- writes: the calls web.App.post / the CLI make, as this role
    def send(self, to, text, re=None, cc=(), prio='normal', topic=None):
        text = (text or '').strip()
        if not text:
            raise BusError('empty message')
        if isinstance(to, str):
            to = [t.strip() for t in to.replace(' ', ',').split(',')]
        to = [t for t in to if t] or ['#all']
        first = text.splitlines()[0]
        body = text if (text != first or not text.isascii() or len(text) > 600) else None
        if body is not None:
            first = first.encode('ascii', 'replace').decode()[:200] or '(see body)'
        prio = prio if prio in PRIOS else 'normal'
        ms = self.bus.send(self.me, to, first, re or None, cc=list(cc or []), prio=prio, topic=topic or None,
                           body=body, route=True)
        ms = ms if isinstance(ms, list) else [ms]
        return {'id': chat.base_id(ms[0]['id']), 'to': [m['to'] for m in ms]}

    def has_reacted(self, mid, emoji):
        return self.me in (self.msgs.get(mid, {}).get('rx', {}).get(emoji) or [])

    def react(self, mid, emoji, on=None):
        if on is None:
            on = not self.has_reacted(mid, emoji)
        return chat.react(self.bus, self.me, mid, emoji, on=on)

    def pin(self, mid, on=None):
        if on is None:
            on = not self.msgs.get(mid, {}).get('pin')
        return chat.pin(self.bus, self.me, mid, on=on)

    def subscribe(self, mid, on=None):
        if on is None:
            root = self.msgs.get(self.root_of(self.msgs[mid]), {}) if mid in self.msgs else {}
            on = self.me not in (root.get('subs') or [])
        return chat.subscribe(self.bus, self.me, mid, on=on)

    def edit(self, mid, text):
        return chat.edit(self.bus, self.me, mid, text)

    def delete(self, mid):
        return chat.delete(self.bus, self.me, mid)

    def mark_seen(self, mids):
        """Group ids in, this role's own per-recipient ids marked seen (as /api/seen does)."""
        with self.index.lock:
            s = self.index.seen.get(self.me, set())
            rcs = [self.index.groups[i]['rc'].get(self.me) for i in mids if i in self.index.groups]
            ids = [r['rid'] for r in rcs if r and r['rid'] not in s]
        if ids:
            self.bus.mark_seen(self.me, ids)
        return len(ids)
