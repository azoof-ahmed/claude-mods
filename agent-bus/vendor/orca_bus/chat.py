"""Group-chat layer of the bus: receipts, reactions, threads, mentions, subscriptions, pins, edits.

Nothing here costs an agent a turn. Notifications follow the message's addressing only:
    to, cc, @mentions, the author of the message replied to, and the thread's subscribers.
Everyone else can browse the stream (the web UI, `thread`), but gets nothing in the inbox.

Files (next to log.jsonl):
    chat.jsonl   react / sub / pin / edit / delete events, append-only
A thread is its root message's group id; a reply carries `reply_to` and inherits `thread`.
"""
import json
import os
import re

from .store import BusError, now_iso, check_name

DEFAULT_ADMINS = ('user', 'coordinator', 'scheduler')
MENTION_RE = re.compile(r'(?<![\w@.])@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})')
# rcpt kinds, in routing order: the first reason a name is reached wins
RCPT_KINDS = ('to', 'cc', 'mention', 'reply', 'sub', 'watch', 'all')
EDIT_WINDOW_S = 300
# per-recipient receipt: what the sender sees
TICKS = {'queued': '.', 'delivered': 'v', 'seen': 'vv', 'failed': '!', 'expired': 'x', 'posted': '#'}


def base_id(mid):
    """The group id a per-recipient id belongs to (m...abc.3 -> m...abc)."""
    return re.sub(r'\.\d+$', '', mid or '')


def admins(bus):
    return tuple(bus.config().get('admins') or DEFAULT_ADMINS)


def chat_path(bus):
    return bus.root / 'chat.jsonl'


def chat_events(bus):
    p = chat_path(bus)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding='utf-8').splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _chat_append(bus, ev):
    ev.setdefault('time', now_iso())
    with bus.lock():
        with open(chat_path(bus), 'a', encoding='utf-8') as f:
            f.write(json.dumps(ev, ensure_ascii=False) + '\n')
    return ev


# ---------------------------------------------------------------- finding messages without folding the whole log
def _log_lines_reverse(path, chunk=1 << 18):
    """Yield the log's lines newest first, reading backwards in chunks (recent messages are found at once)."""
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        pos, rest = f.tell(), b''
        while pos > 0:
            n = min(chunk, pos)
            pos -= n
            f.seek(pos)
            buf = f.read(n) + rest
            lines = buf.split(b'\n')
            rest = lines.pop(0)
            for line in reversed(lines):
                if line.strip():
                    yield line
        if rest.strip():
            yield rest


def find_msg(bus, mid):
    """The first 'msg' record of the message `mid` (a group id or a per-recipient id), or None."""
    if not mid or not bus.log_path.exists():
        return None
    base = base_id(mid)
    key = base.encode()
    found = None
    for line in _log_lines_reverse(bus.log_path):
        if key not in line or b'"ev": "msg"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get('ev') == 'msg' and (ev.get('id') == mid or base_id(ev.get('id')) == base):
            found = ev  # keep walking back to the .1 record: it is the group's first
            if ev.get('id') in (base, base + '.1'):
                break
    return found


def records(bus, mid):
    """Every per-recipient record of a message with its folded state, and the seen sets: (records, state_events)."""
    base = base_id(mid)
    key = base.encode()
    recs, order = {}, []
    if not bus.log_path.exists():
        return []
    with open(bus.log_path, 'rb') as f:
        for line in f:
            if key not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            i = ev.get('id', '')
            if base_id(i) != base:
                continue
            if ev.get('ev') == 'msg':
                r = dict(ev)
                r['history'] = [[ev['time'], ev['state'], '']]
                recs[i] = r
                order.append(i)
            elif ev.get('ev') == 'state' and i in recs:
                recs[i]['state'] = ev['state']
                recs[i]['history'].append([ev['time'], ev['state'], ev.get('detail', '')])
    return [recs[i] for i in order]


def thread_messages(bus, mid):
    """The thread `mid` belongs to: one entry per message (group), oldest first, with `notified` names."""
    m = find_msg(bus, mid)
    if m is None:
        raise BusError(f'no message {mid}')
    root = m.get('thread') or base_id(m['id'])
    key = root.encode()
    groups = {}
    with open(bus.log_path, 'rb') as f:
        for line in f:
            if key not in line or b'"ev": "msg"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            b = base_id(ev.get('id'))
            if ev.get('ev') != 'msg' or (b != root and ev.get('thread') != root):
                continue
            g = groups.setdefault(b, {**{k: v for k, v in ev.items() if k != 'ev'}, 'id': b, 'notified': []})
            if not ev['to'].startswith('#'):
                g['notified'].append(ev['to'])
    return sorted(groups.values(), key=lambda g: (g['id'] != root, g['time']))


def receipt(bus, rec, seen_cache=None):
    """queued | delivered | seen | failed | expired | posted, plus the detail, for one per-recipient record."""
    st = rec.get('state')
    detail = rec['history'][-1][2] if rec.get('history') else ''
    if st == 'posted':
        return 'posted', 'channel post: nobody notified'
    seen = (seen_cache or {}).get(rec['to'])
    if seen is None:
        try:
            seen = bus.seen(rec['to'])
        except BusError:
            seen = set()
        if seen_cache is not None:
            seen_cache[rec['to']] = seen
    return classify(st, detail, rec['id'] in seen), detail


def classify(state, detail, seen):
    """The receipt a per-recipient state maps to (the CLI and the web UI share this)."""
    if state == 'posted':
        return 'posted'
    if seen or state == 'acked':
        return 'seen'
    if state == 'failed':
        return 'expired' if 'tab closed for' in (detail or '') else 'failed'
    if state in ('inbox', 'delivered'):
        return 'delivered'
    return 'queued'


# ---------------------------------------------------------------- routing
def mentions(text, names):
    """Registered names (and 'all') @mentioned in the text, in order."""
    out = []
    text = re.sub(r'`[^`\n]*`', ' ', text or '')  # `@role` in code quotes is talk about a role, not a ping
    for m in MENTION_RE.finditer(text):
        n = m.group(1).rstrip('.-')
        if (n in names or n == 'all') and n not in out:
            out.append(n)
    return out


def subscribers(bus, thread, events=None):
    subs = {}
    for ev in events if events is not None else chat_events(bus):
        if ev.get('ev') == 'sub' and ev.get('thread') == thread:
            subs[ev['name']] = ev.get('on', True)
    return [n for n, on in subs.items() if on]


def route(bus, frm, to, cc, text, reply_to=None, body=None):
    """Who gets notified, and why: (tos, extra[(name, rcpt)], thread, chan).

    `to` may be a channel ('#train', '#all'): a post to the stream that notifies nobody by itself."""
    tos = [t.strip() for t in (to.split(',') if isinstance(to, str) else to or ()) if t.strip()]
    chans = [t[1:] for t in tos if t.startswith('#')]
    tos = [t for t in tos if not t.startswith('#')]
    ccs = [c.strip() for c in (cc.split(',') if isinstance(cc, str) else cc or ()) if c.strip()]
    reg = bus.registry()
    taken = set(tos) | set(ccs)
    extra = []

    def add(n, why):
        if n and n != frm and n not in taken and n in reg:
            taken.add(n)
            extra.append((n, why))

    # in a body, @all is prose (docs, quotes): only named roles count there
    for n in mentions(text, reg) + [n for n in mentions(body, reg) if n != 'all']:
        if n == 'all':
            if frm not in admins(bus):
                raise BusError(f'@all is for {", ".join(admins(bus))} only: name the roles you need')
            for name, e in sorted(reg.items()):
                if e.get('kind') != 'shell' or name == 'user':
                    add(name, 'all')
        else:
            add(n, 'mention')
    thread = None
    if reply_to:
        parent = find_msg(bus, reply_to)
        if parent is None:
            raise BusError(f'--re {reply_to}: no such message')
        thread = parent.get('thread') or base_id(parent['id'])
        add(parent.get('from'), 'reply')
    if thread:
        for n in subscribers(bus, thread):
            add(n, 'sub')
    if frm == 'user':  # the coordinator gets everything the user posts
        add('coordinator', 'watch')
    chan = chans[0] if chans else None
    return tos, extra, thread, (None if chan == 'all' else chan)


# ---------------------------------------------------------------- reactions, subscriptions, pins, edits
def react(bus, by, mid, emoji, on=True):
    check_name(by)
    emoji = (emoji or '').strip()
    if not emoji or len(emoji) > 16 or any(ord(c) < 32 for c in emoji):
        raise BusError('one emoji (or a short :word:) please')
    m = find_msg(bus, mid)
    if m is None:
        raise BusError(f'no message {mid}')
    return _chat_append(bus, {'ev': 'react', 'id': base_id(m['id']), 'author': m['from'], 'emoji': emoji,
                              'by': by, 'on': bool(on)})


def subscribe(bus, name, mid, on=True):
    check_name(name)
    m = find_msg(bus, mid)
    if m is None:
        raise BusError(f'no message {mid}')
    thread = m.get('thread') or base_id(m['id'])
    return _chat_append(bus, {'ev': 'sub', 'thread': thread, 'name': name, 'on': bool(on)})


def pin(bus, by, mid, on=True):
    m = find_msg(bus, mid)
    if m is None:
        raise BusError(f'no message {mid}')
    return _chat_append(bus, {'ev': 'pin', 'id': base_id(m['id']), 'by': by, 'on': bool(on)})


def _own_recent(bus, by, mid, what, clock=None):
    import datetime
    m = find_msg(bus, mid)
    if m is None:
        raise BusError(f'no message {mid}')
    if m['from'] != by:
        raise BusError(f'only {m["from"]} may {what} {base_id(m["id"])}')
    age = (clock or (lambda: datetime.datetime.now().astimezone()))() - datetime.datetime.fromisoformat(m['time'])
    if age.total_seconds() > EDIT_WINDOW_S:
        raise BusError(f'{what} only within {EDIT_WINDOW_S // 60} minutes of sending')
    return m


def edit(bus, by, mid, text, clock=None):
    m = _own_recent(bus, by, mid, 'edit', clock)
    if not (text or '').strip():
        raise BusError('empty message')
    return _chat_append(bus, {'ev': 'edit', 'id': base_id(m['id']), 'by': by, 'text': text.strip()})


def delete(bus, by, mid, clock=None):
    m = _own_recent(bus, by, mid, 'delete', clock)
    return _chat_append(bus, {'ev': 'delete', 'id': base_id(m['id']), 'by': by})


def reactions_for(bus, name):
    """Reactions others added to `name`'s messages since its digest cursor: (events, new cursor)."""
    p, cur = chat_path(bus), bus.root / 'inbox' / f'{check_name(name)}.chatpos'
    try:
        pos = int(cur.read_text().strip() or 0)
    except (OSError, ValueError):
        pos = 0
    if not p.exists():
        return [], pos
    with open(p, 'rb') as f:
        f.seek(pos)
        data = f.read()
    data = data[:data.rfind(b'\n') + 1]
    out = []
    for line in data.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get('ev') == 'react' and ev.get('on', True) and ev.get('author') == name and ev.get('by') != name:
            out.append(ev)
    return out, pos + len(data)


def mark_reactions(bus, name, pos):
    (bus.root / 'inbox' / f'{check_name(name)}.chatpos').write_text(str(pos))


def reactions(events, ids=None):
    """{group id: {emoji: [names]}} folded from chat events (toggle semantics)."""
    out = {}
    for ev in events:
        if ev.get('ev') != 'react' or (ids is not None and ev['id'] not in ids):
            continue
        who = out.setdefault(ev['id'], {}).setdefault(ev['emoji'], [])
        if ev.get('on', True) and ev['by'] not in who:
            who.append(ev['by'])
        elif not ev.get('on', True) and ev['by'] in who:
            who.remove(ev['by'])
    return {i: {e: w for e, w in d.items() if w} for i, d in out.items()}
