"""Task tracker writes. Every function: permission check, one atomic transaction, an event row, then notes.

Each returns (result, notes): notes are (to, text, prio, issue), delivered by send_notes() AFTER the commit, so a slow
notify command never holds the database lock.
"""
from __future__ import annotations

import re
from typing import Optional

from taskdb import (PRIORITIES, STATUSES, Actor, Denied, TaskError, can, check_labels, check_person,
                    find_duplicates, fmt_key, get_issue, load_roles, log_event, notify, now, parse_key,
                    review_setting, touch_conflicts, write_tx)


def send_notes(conn, actor: Actor, notes):
    for to, text, prio, issue in notes:
        notify(conn, actor, to, text, prio, issue)


def _admins():
    roles = load_roles() or {}
    return list(((roles.get('levels') or {}).get('admin')) or [])


def _check_choice(value, allowed, what):
    if value is not None and value not in allowed:
        raise TaskError(f'{what} must be one of {", ".join(allowed)} (got {value!r})')


def _parse_link(x):
    if isinstance(x, (tuple, list)):
        kind, value = x
    else:
        kind, _, value = str(x).partition(':')
    kind = kind.strip()
    if not re.fullmatch(r'[a-z][\w-]*', kind or '') or not str(value).strip():
        raise TaskError(f'a link is kind:value, e.g. ref:issue-42, url:https://..., proof:logs/run.txt '
                        f'(got {x!r})')
    return kind, str(value).strip()


def _set_links(conn, n, links):
    for x in links:
        kind, value = _parse_link(x)
        conn.execute('INSERT OR IGNORE INTO links(issue, kind, value) VALUES (?,?,?)', (n, kind, value))


def _conflict_gate(conn, actor, n, title, force_reason, notes, globs=None):
    hits = touch_conflicts(conn, n, globs)
    if not hits:
        return []
    if not force_reason:
        lines = '\n'.join(f'  {h["key"]} ({h["assignee"] or "unassigned"}) "{h["title"][:60]}": '
                          f'{h["my_glob"]} overlaps {h["their_glob"]}' for h in hits)
        raise Denied('conflict.touches', f'{fmt_key(n)} would edit files another in-progress task owns:\n{lines}\n'
                                         f'  Talk to that assignee, or pass --force "<reason>" (logged, and they '
                                         f'are told).')
    log_event(conn, actor.tag, n, 'conflict_override', reason=force_reason, hits=hits)
    for h in hits:
        notes.append((h['assignee'], f'{actor.name} started {fmt_key(n)} "{title[:60]}" over your {h["key"]}: '
                                     f'{h["my_glob"]} overlaps {h["their_glob"]}. Reason: {force_reason}',
                      'normal', n))
    return hits


def create(conn, actor: Actor, title: str, body: str = '', status: Optional[str] = None, priority: str = 'med',
           assignee: Optional[str] = None, reviewer: Optional[str] = None, area: Optional[str] = None,
           parent=None, due: Optional[str] = None, labels=(), links=(), touches=(), blocked_by=(),
           force_dup: bool = False, force_reason: Optional[str] = None):
    can(actor, 'new')
    title = (title or '').strip()
    if not title:
        raise TaskError('a title is required')
    _check_choice(priority, PRIORITIES, 'priority')
    _check_choice(status, STATUSES, 'status')
    check_labels(labels)
    links = [_parse_link(x) for x in links]
    proposed = None
    if assignee:
        check_person(assignee)
        if actor.level == 'member' and assignee != actor.name:
            proposed, assignee = assignee, None
    reviewer = reviewer or review_setting('default_reviewer') or None
    if reviewer:
        check_person(reviewer)
    status = status or ('todo' if assignee else 'backlog')
    notes = []
    with write_tx(conn):
        if not force_dup:
            dups = find_duplicates(conn, title, links)
            if dups:
                lines = '\n'.join(f'  {d["key"]} [{d["status"]}] "{d["title"][:70]}" ({d["why"]})' for d in dups)
                raise Denied('dup.detect', f'this looks like an existing task:\n{lines}\n  Comment on it, or pass '
                                           f'--dup-ok if it really is new.')
        parent_id = parse_key(parent) if parent else None
        if parent_id:
            get_issue(conn, parent_id)
        ts = now()
        cur = conn.execute('INSERT INTO issues(title, body, status, priority, assignee, proposed, creator, reviewer, '
                           'area, parent, due, created, updated, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (title, body, status, priority, assignee, proposed, actor.name, reviewer, area,
                            parent_id, due, ts, ts, ts if status in ('done', 'cancelled') else None))
        n = cur.lastrowid
        for lb in labels:
            conn.execute('INSERT OR IGNORE INTO labels VALUES (?,?)', (n, lb))
        _set_links(conn, n, links)
        for g in touches:
            conn.execute('INSERT OR IGNORE INTO touches VALUES (?,?)', (n, g))
        for b in blocked_by:
            bn = parse_key(b)
            get_issue(conn, bn)
            conn.execute('INSERT OR IGNORE INTO blocks VALUES (?,?)', (n, bn))
        if status == 'in_progress':
            _conflict_gate(conn, actor, n, title, force_reason, notes)
        log_event(conn, actor.tag, n, 'create', title=title, status=status, assignee=assignee, proposed=proposed)
    if assignee:
        notes.append((assignee, f'{actor.name} assigned you {fmt_key(n)} "{title[:80]}" [{priority}]', 'normal', n))
    if proposed:
        for adm in _admins():
            notes.append((adm, f'{actor.name} proposes {proposed} for new {fmt_key(n)} "{title[:70]}"', 'fyi', n))
    return {'key': fmt_key(n), 'id': n, 'proposed': proposed}, notes


def claim(conn, actor: Actor, key, start: bool = True, force_reason: Optional[str] = None):
    can(actor, 'claim')
    n = parse_key(key)
    notes = []
    with write_tx(conn):
        row = get_issue(conn, n)
        if row['status'] in ('done', 'cancelled'):
            raise TaskError(f'{fmt_key(n)} is {row["status"]}')
        if row['assignee'] and row['assignee'] != actor.name:
            raise Denied('claim.one-assignee', f'{fmt_key(n)} is already {row["assignee"]}\'s. Comment to them, or '
                                               f'ask a lead to reassign.')
        cur = conn.execute("UPDATE issues SET assignee=?, proposed=NULL, updated=? WHERE id=? AND "
                           "(assignee IS NULL OR assignee='' OR assignee=?)", (actor.name, now(), n, actor.name))
        if cur.rowcount != 1:
            raise Denied('claim.one-assignee', f'{fmt_key(n)} was claimed by someone else just now')
        log_event(conn, actor.tag, n, 'claim')
        if start and row['status'] in ('backlog', 'todo', 'blocked'):
            _conflict_gate(conn, actor, n, row['title'], force_reason, notes)
            conn.execute("UPDATE issues SET status='in_progress', updated=? WHERE id=?", (now(), n))
            log_event(conn, actor.tag, n, 'move', **{'from': row['status'], 'to': 'in_progress'})
    return {'key': fmt_key(n), 'assignee': actor.name}, notes


def release(conn, actor: Actor, key, note: Optional[str] = None):
    """Give a task back: no assignee, status todo. The assignee, a lead or an admin."""
    n = parse_key(key)
    with write_tx(conn):
        row = get_issue(conn, n)
        if row['assignee'] != actor.name:
            can(actor, 'move', row)
        conn.execute("UPDATE issues SET assignee=NULL, status=CASE WHEN status IN ('in_progress','blocked') "
                     "THEN 'todo' ELSE status END, updated=? WHERE id=?", (now(), n))
        log_event(conn, actor.tag, n, 'release', **{'from': row['assignee'], 'note': note})
        if note:
            conn.execute('INSERT INTO comments(issue, author, ts, body) VALUES (?,?,?,?)', (n, actor.name, now(), note))
    notes = []
    if row['assignee'] and row['assignee'] != actor.name:
        notes.append((row['assignee'], f'{actor.name} released {fmt_key(n)} from you', 'normal', n))
    return {'key': fmt_key(n), 'assignee': None}, notes


def assign(conn, actor: Actor, key, who: Optional[str], propose: bool = False):
    n = parse_key(key)
    notes = []
    if who in ('none', '-'):
        who = None
    if who:
        check_person(who)
    with write_tx(conn):
        row = get_issue(conn, n)
        if propose:
            can(actor, 'propose', row)
            conn.execute('UPDATE issues SET proposed=?, updated=? WHERE id=?', (who, now(), n))
            log_event(conn, actor.tag, n, 'propose', who=who)
            conn.execute('INSERT INTO comments(issue, author, ts, body) VALUES (?,?,?,?)',
                         (n, actor.name, now(), f'proposes {who} as assignee'))
            for adm in _admins():
                notes.append((adm, f'{actor.name} proposes {who} for {fmt_key(n)} "{row["title"][:70]}"', 'fyi', n))
            return {'key': fmt_key(n), 'proposed': who}, notes
        can(actor, 'assign', row, target=who)
        conn.execute('UPDATE issues SET assignee=?, proposed=NULL, updated=? WHERE id=?', (who, now(), n))
        log_event(conn, actor.tag, n, 'assign', **{'from': row['assignee'], 'to': who})
    if who:
        notes.append((who, f'{actor.name} assigned you {fmt_key(n)} "{row["title"][:80]}" [{row["priority"]}]',
                      'normal', n))
    if row['assignee'] and row['assignee'] != who:
        notes.append((row['assignee'], f'{actor.name} moved {fmt_key(n)} from you to {who or "nobody"}', 'normal', n))
    return {'key': fmt_key(n), 'assignee': who}, notes


def move(conn, actor: Actor, key, status: str, force_reason: Optional[str] = None, proof: Optional[str] = None,
         note: Optional[str] = None, reviewer: Optional[str] = None):
    _check_choice(status, STATUSES, 'status')
    n = parse_key(key)
    notes = []
    with write_tx(conn):
        row = get_issue(conn, n)
        can(actor, 'move', row)
        old = row['status']
        if old == status:
            return {'key': fmt_key(n), 'status': status, 'unchanged': True}, notes
        if proof:
            _set_links(conn, n, [('proof', proof)])
        if reviewer:
            check_person(reviewer)
            conn.execute('UPDATE issues SET reviewer=? WHERE id=?', (reviewer, n))
        rev = reviewer or row['reviewer']
        if status == 'in_progress':
            _conflict_gate(conn, actor, n, row['title'], force_reason, notes)
        if status == 'in_review':
            if review_setting('require_proof', False):
                has_proof = conn.execute("SELECT 1 FROM links WHERE issue=? AND kind='proof'", (n,)).fetchone()
                if not has_proof:
                    raise Denied('review.needs-proof', 'in_review needs a proof link: --proof <path or url>')
            if not rev:
                raise Denied('review.needs-reviewer', 'no reviewer set: --reviewer <name>')
            notes.append((rev, f'review requested on {fmt_key(n)} "{row["title"][:70]}" by '
                               f'{row["assignee"] or actor.name}', 'normal', n))
        if status == 'done' and rev and actor.level == 'member' and actor.name != rev:
            raise Denied('done.needs-review', f'{fmt_key(n)} has reviewer {rev}: move it to in_review; the reviewer '
                                              f'approves it with `task.py review {fmt_key(n)} --approve`')
        closed = now() if status in ('done', 'cancelled') else None
        conn.execute('UPDATE issues SET status=?, updated=?, closed_at=? WHERE id=?', (status, now(), closed, n))
        log_event(conn, actor.tag, n, 'move', **{'from': old, 'to': status, 'note': note, 'proof': proof})
        if note:
            conn.execute('INSERT INTO comments(issue, author, ts, body) VALUES (?,?,?,?)',
                         (n, actor.name, now(), note))
        if status == 'blocked':
            for who in {row['creator'], rev} - {None, actor.name}:
                notes.append((who, f'{fmt_key(n)} "{row["title"][:70]}" is BLOCKED ({actor.name}): {note or ""}',
                              'normal', n))
        if status in ('done', 'cancelled'):
            notes += _release_dependents(conn, actor, n)
    return {'key': fmt_key(n), 'from': old, 'status': status}, notes


def _release_dependents(conn, actor, n):
    notes = []
    for r in conn.execute('SELECT i.* FROM blocks b JOIN issues i ON i.id=b.issue WHERE b.blocked_by=?', (n,)):
        left = conn.execute("SELECT COUNT(*) FROM blocks b JOIN issues i ON i.id=b.blocked_by WHERE b.issue=? AND "
                            "i.status NOT IN ('done','cancelled')", (r['id'],)).fetchone()[0]
        if left == 0:
            log_event(conn, actor.tag, r['id'], 'unblocked', by=fmt_key(n))
            notes.append((r['assignee'], f'{fmt_key(r["id"])} "{r["title"][:70]}" is UNBLOCKED: {fmt_key(n)} closed',
                          'normal', r['id']))
    return notes


def review(conn, actor: Actor, key, verdict: str, note: Optional[str] = None):
    n = parse_key(key)
    notes = []
    with write_tx(conn):
        row = get_issue(conn, n)
        can(actor, 'review', row)
        if row['status'] != 'in_review':
            raise TaskError(f'{fmt_key(n)} is {row["status"]}, not in_review')
        new = 'done' if verdict == 'approve' else 'in_progress'
        closed = now() if new == 'done' else None
        conn.execute('UPDATE issues SET status=?, updated=?, closed_at=? WHERE id=?', (new, now(), closed, n))
        log_event(conn, actor.tag, n, 'review', verdict=verdict, note=note)
        conn.execute('INSERT INTO comments(issue, author, ts, body) VALUES (?,?,?,?)',
                     (n, actor.name, now(), f'REVIEW {verdict.upper()}' + (f': {note}' if note else '')))
        if new == 'done':
            notes += _release_dependents(conn, actor, n)
    notes.append((row['assignee'], f'{fmt_key(n)} review {verdict.upper()} by {actor.name}'
                                   + (f': {note}' if note else ''), 'normal', n))
    return {'key': fmt_key(n), 'verdict': verdict, 'status': new}, notes


def comment(conn, actor: Actor, key, body: str):
    can(actor, 'comment')
    n = parse_key(key)
    with write_tx(conn):
        row = get_issue(conn, n)
        conn.execute('INSERT INTO comments(issue, author, ts, body) VALUES (?,?,?,?)', (n, actor.name, now(), body))
        log_event(conn, actor.tag, n, 'comment', body=body[:200])
    notes = []
    targets = {row['assignee'], row['reviewer']} - {None, actor.name}
    for who in targets:
        notes.append((who, f'{actor.name} on {fmt_key(n)}: {body[:300]}', 'fyi', n))
    for who in set(re.findall(r'@([\w.-]+)', body)) - targets - {actor.name}:
        notes.append((who, f'{actor.name} mentions you on {fmt_key(n)} "{row["title"][:50]}": {body[:300]}',
                      'normal', n))
    return {'key': fmt_key(n)}, notes


def link(conn, actor: Actor, key, kind: str, value: str, remove: bool = False):
    n = parse_key(key)
    kind, value = _parse_link((kind, value))
    with write_tx(conn):
        row = get_issue(conn, n)
        can(actor, 'link', row)
        if remove:
            conn.execute('DELETE FROM links WHERE issue=? AND kind=? AND value=?', (n, kind, value))
        else:
            _set_links(conn, n, [(kind, value)])
        log_event(conn, actor.tag, n, 'unlink' if remove else 'link', kind=kind, value=value)
    return {'key': fmt_key(n)}, []


def block(conn, actor: Actor, key, by, remove: bool = False):
    n, b = parse_key(key), parse_key(by)
    if n == b:
        raise TaskError('a task cannot block itself')
    with write_tx(conn):
        row = get_issue(conn, n)
        blocker = get_issue(conn, b)
        can(actor, 'block', row)
        if remove:
            conn.execute('DELETE FROM blocks WHERE issue=? AND blocked_by=?', (n, b))
        else:
            seen, stack = set(), [b]  # refuse a cycle: does b (transitively) wait on n?
            while stack:
                x = stack.pop()
                if x == n:
                    raise TaskError(f'{fmt_key(b)} already waits on {fmt_key(n)}: that would be a cycle')
                if x in seen:
                    continue
                seen.add(x)
                stack += [r[0] for r in conn.execute('SELECT blocked_by FROM blocks WHERE issue=?', (x,))]
            conn.execute('INSERT OR IGNORE INTO blocks VALUES (?,?)', (n, b))
        log_event(conn, actor.tag, n, 'unblock_by' if remove else 'blocked_by', other=fmt_key(b))
    notes = []
    if not remove and blocker['status'] not in ('done', 'cancelled'):
        notes.append((blocker['assignee'], f'{fmt_key(n)} "{row["title"][:60]}" now waits on your {fmt_key(b)}',
                      'fyi', n))
    return {'key': fmt_key(n), 'blocked_by': fmt_key(b), 'removed': remove}, notes


def touch(conn, actor: Actor, key, add=(), remove=(), force_reason: Optional[str] = None):
    n = parse_key(key)
    notes = []
    with write_tx(conn):
        row = get_issue(conn, n)
        can(actor, 'touch', row)
        for g in remove:
            conn.execute('DELETE FROM touches WHERE issue=? AND glob=?', (n, g))
        if add and row['status'] == 'in_progress':
            _conflict_gate(conn, actor, n, row['title'], force_reason, notes, globs=list(add))
        for g in add:
            conn.execute('INSERT OR IGNORE INTO touches VALUES (?,?)', (n, g))
        log_event(conn, actor.tag, n, 'touches', add=list(add), remove=list(remove))
    return {'key': fmt_key(n)}, notes


EDITABLE = ('title', 'body', 'priority', 'area', 'due', 'reviewer', 'parent')


def edit(conn, actor: Actor, key, labels_add=(), labels_remove=(), **fields):
    n = parse_key(key)
    fields = {k: v for k, v in fields.items() if v is not None}
    _check_choice(fields.get('priority'), PRIORITIES, 'priority')
    check_labels(labels_add)
    if fields.get('reviewer'):
        check_person(fields['reviewer'])
    with write_tx(conn):
        row = get_issue(conn, n)
        can(actor, 'edit', row)
        if 'parent' in fields:
            fields['parent'] = parse_key(fields['parent']) if fields['parent'] not in ('', 'none') else None
        changes = {}
        for k, v in fields.items():
            if k not in EDITABLE:
                raise TaskError(f'cannot edit {k}')
            if row[k] != v:
                changes[k] = {'from': row[k] if k != 'body' else '...', 'to': v if k != 'body' else '...'}
                conn.execute(f'UPDATE issues SET {k}=?, updated=? WHERE id=?', (v, now(), n))
        for lb in labels_add:
            conn.execute('INSERT OR IGNORE INTO labels VALUES (?,?)', (n, lb))
        for lb in labels_remove:
            conn.execute('DELETE FROM labels WHERE issue=? AND label=?', (n, lb))
        if labels_add or labels_remove:
            changes['labels'] = {'add': list(labels_add), 'remove': list(labels_remove)}
        if changes:
            log_event(conn, actor.tag, n, 'edit', **changes)
    return {'key': fmt_key(n), 'changed': sorted(changes)}, []
