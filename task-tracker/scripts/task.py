"""task: a task tracker for a team of agents and people sharing one repository.

  task.py init                                  # create the tracker (and a roles.toml template)
  task.py mine                                  # your open tasks + reviews waiting on you (check between steps)
  task.py new "title" --touches "src/api/**" [--label bug --area api --priority high]
  task.py claim TASK-12                         # take it + start it (refuses on file conflicts)
  task.py touch TASK-12 "src/api/**"            # declare the files you will edit
  task.py move TASK-12 in_review --reviewer reviewer [--proof logs/run.txt]
  task.py review TASK-12 --approve              # reviewer / lead / admin
  task.py list --status in_progress,blocked     # every read takes --json
  task.py show TASK-12 | board | conflicts | stats | log | inbox | where

Who you are: --as <name>, else $TASKS_AS, else "user". Permissions: the roles file (see `task.py where`).
Global options go before the command: --db, --roles, --prefix, --labels, --importers, --notify-cmd, --as.
Settings: these flags > <project>/.claude/claude-mods.json ("task-tracker") > TASKS_* env vars > defaults.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import ops  # noqa: E402
import taskdb as db  # noqa: E402
from taskdb import Denied, TaskError, fmt_key  # noqa: E402

# ------------------------------------------------------------------------------------------------ colour

_COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
STATUS_COLOR = {'backlog': '90', 'todo': '37', 'in_progress': '33', 'in_review': '35', 'blocked': '31',
                'done': '32', 'cancelled': '90'}
STATUS_ICON = {'backlog': '◌', 'todo': '○', 'in_progress': '◐', 'in_review': '◑', 'blocked': '⊘', 'done': '●',
               'cancelled': '✕'}
PRIO_ICON = {'urgent': '!!', 'high': '▲', 'med': '■', 'low': '▽'}
PRIO_COLOR = {'urgent': '91', 'high': '33', 'med': '37', 'low': '90'}


def c(text, code):
    return f'\x1b[{code}m{text}\x1b[0m' if _COLOR and code else str(text)


def _enc_safe():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except AttributeError:
            pass


def short(s, n: int) -> str:
    s = (s or '').replace('\n', ' ')
    return s if len(s) <= n else s[:max(0, n - 1)] + '…'


def line(d: dict, width: int, show_area=True) -> str:
    st = d['status']
    who = d['assignee'] or (f'?{d["proposed"]}' if d.get('proposed') else '-')
    kw = max(8, len(d['key']))
    head = f'{d["key"]:<{kw}} {STATUS_ICON[st]} {st:<11} {PRIO_ICON[d["priority"]]:<2} {who:<12} '
    tail = f' [{d["area"]}]' if show_area and d.get('area') else ''
    room = max(10, width - len(head) - len(tail))
    return (c(f'{d["key"]:<{kw}}', '1') + ' ' + c(f'{STATUS_ICON[st]} {st:<11}', STATUS_COLOR[st]) + ' '
            + c(f'{PRIO_ICON[d["priority"]]:<2}', PRIO_COLOR[d['priority']]) + ' ' + c(f'{who:<12}', '36') + ' '
            + short(d['title'], room) + c(tail, '90'))


def out_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def width() -> int:
    return int(os.environ.get('TASKS_WIDTH') or shutil.get_terminal_size((120, 40)).columns)


def _done(a, res, text):
    if a.json:
        out_json(res)
    else:
        print(text)


def _labels(a):
    out = []
    for x in a or []:
        out += [y.strip() for y in x.split(',') if y.strip()]
    return out


# ------------------------------------------------------------------------------------------------ commands

def cmd_init(conn, me, a):
    res = db.init(write_roles=not a.no_roles)
    if a.json:
        return out_json(res)
    print(('created ' if res['db_created'] else 'already there: ') + res['db'])
    if res['roles_written']:
        print(f'wrote roles template {res["roles"]} (edit it, or delete it to let everyone do everything)')


def cmd_where(conn, me, a):
    d = db.cfg().as_dict()
    d['me'] = {'name': me.name, 'level': me.level, 'via': me.via}
    if a.json:
        return out_json(d)
    for k in ('root', 'db', 'roles', 'prefix'):
        print(f'{k:<8} {d[k]}' + ('' if k not in ('db', 'roles') else ('' if d[f"{k}_exists"] else '  (missing)')))
    print(f'{"labels":<8} {", ".join(d["labels"]) or "(any)"}')
    print(f'{"import":<8} {", ".join(d["importers"]) or "(none)"}')
    print(f'{"you":<8} {me.name} ({me.level}, via {me.via})')


def cmd_new(conn, me, a):
    body = a.body or ''
    if a.body_file:
        body = Path(a.body_file).read_text(encoding='utf-8')
    res, notes = ops.create(conn, me, a.title, body=body, status=a.status, priority=a.priority,
                            assignee=a.assign, reviewer=a.reviewer, area=a.area, parent=a.parent, due=a.due,
                            labels=_labels(a.label), links=a.link or [], touches=a.touches or [],
                            blocked_by=a.blocked_by or [], force_dup=a.dup_ok, force_reason=a.force)
    ops.send_notes(conn, me, notes)
    if a.json:
        return out_json(res)
    extra = f' (proposed for {res["proposed"]}: a lead or admin assigns)' if res['proposed'] else ''
    print(f'created {c(res["key"], "1")}{extra}')


def cmd_list(conn, me, a):
    rows = db.query(conn, status=a.status, area=a.area, assignee=a.assignee, priority=a.priority, label=a.label,
                    text=a.grep, include_closed=a.all, limit=a.limit)
    ds = [db.issue_dict(conn, r) for r in rows]
    if a.json:
        return out_json(ds)
    w = width()
    for d in ds:
        print(line(d, w))
    if not ds:
        print(c('(no tasks match)', '90'))


def _unread(conn, name):
    return conn.execute('SELECT COUNT(*) FROM notes WHERE recipient=? AND seen=0', (name,)).fetchone()[0]


def cmd_mine(conn, me, a):
    who = a.who or me.name
    ds = [db.issue_dict(conn, r) for r in db.query(conn, assignee=who)]
    rev = [db.issue_dict(conn, r) for r in conn.execute(
        "SELECT * FROM issues WHERE reviewer=? AND status='in_review' ORDER BY id", (who,))]
    unread = _unread(conn, who)
    if a.json:
        return out_json({'mine': ds, 'to_review': rev, 'unread_notes': unread, 'as': who})
    w = width()
    for d in ds:
        blk = f' ⊘waits {",".join(d["blocked_by"])}' if d['blocked_by'] and d['status'] != 'done' else ''
        print(line(d, w - len(blk), show_area=False) + c(blk, '31'))
    for d in rev:
        print(c('review ', '35') + line(d, w - 7, show_area=False))
    if not ds and not rev:
        print(c(f'{who}: no open tasks', '90'))
    if unread:
        print(c(f'{unread} unread note{"s" if unread > 1 else ""}: task.py inbox', '33'))


def cmd_inbox(conn, me, a):
    who = a.who or me.name
    sql = 'SELECT * FROM notes WHERE recipient=?' + ('' if a.all else ' AND seen=0') + ' ORDER BY id DESC LIMIT ?'
    rows = conn.execute(sql, (who, a.limit)).fetchall()[::-1]
    notes = [{'id': r['id'], 'ts': r['ts'], 'from': r['sender'], 'key': fmt_key(r['issue']) if r['issue'] else None,
              'prio': r['prio'], 'body': r['body'], 'seen': bool(r['seen'])} for r in rows]
    if not a.keep and rows and who == me.name:
        with db.write_tx(conn):
            conn.execute(f'UPDATE notes SET seen=1 WHERE id IN ({",".join("?" * len(rows))})', [r['id'] for r in rows])
    if a.json:
        return out_json(notes)
    for n in notes:
        print(f'{c(n["ts"][5:16], "90")} {c(n["from"], "36")} {c(n["key"] or "", "1")} {n["body"]}')
    if not notes:
        print(c(f'{who}: no {"" if a.all else "unread "}notes', '90'))


def cmd_show(conn, me, a):
    d = db.issue_dict(conn, db.get_issue(conn, a.id), full=True)
    if a.json:
        return out_json(d)
    st = d['status']
    print(c(f'{d["key"]}  {d["title"]}', '1'))
    print(f'  {c(STATUS_ICON[st] + " " + st, STATUS_COLOR[st])}   '
          f'{c(PRIO_ICON[d["priority"]] + " " + d["priority"], PRIO_COLOR[d["priority"]])}   area {d["area"] or "-"}')
    print(f'  assignee {c(d["assignee"] or "-", "36")}   proposed {d["proposed"] or "-"}   reviewer '
          f'{d["reviewer"] or "-"}   creator {d["creator"]}')
    print(f'  created {d["created"][:16]}   updated {d["updated"][:16]}'
          + (f'   closed {d["closed_at"][:16]}' if d['closed_at'] else '') + (f'   due {d["due"]}' if d['due'] else ''))
    if d['parent']:
        print(f'  parent {d["parent"]}')
    if d['labels']:
        print('  labels ' + ', '.join(d['labels']))
    if d['links']:
        print('  links  ' + ', '.join(f'{x["kind"]}:{x["value"]}' for x in d['links']))
    if d['touches']:
        print('  touches ' + ', '.join(d['touches']))
    if d['blocked_by'] or d['blocks']:
        print(f'  blocked by {", ".join(d["blocked_by"]) or "-"}   blocks {", ".join(d["blocks"]) or "-"}')
    if d['body']:
        print()
        for ln in d['body'].strip().splitlines()[:None if a.full else 40]:
            print('  ' + ln)
    if d['children']:
        print(c('\n  sub-tasks', '1'))
        for ch in d['children']:
            print(f'    {ch["key"]:<8} {STATUS_ICON[ch["status"]]} {ch["status"]:<11} {ch["assignee"] or "-":<12} '
                  f'{short(ch["title"], 70)}')
    if d['comments']:
        print(c('\n  comments', '1'))
        for cm in d['comments'][-(a.last or 10):]:
            print(f'    {c(cm["ts"][5:16], "90")} {c(cm["author"], "36")}: {cm["body"]}')
    print(c('\n  history', '1'))
    for h in d['history'][-(a.last or 15):]:
        det = h['detail']
        txt = ' '.join(f'{k}={v}' for k, v in det.items() if v not in (None, '', [], {})) if det else ''
        print(f'    {c(h["ts"][5:16], "90")} {c(h["actor"], "36")} {h["action"]} {short(txt, 90)}')


def cmd_claim(conn, me, a):
    res, notes = ops.claim(conn, me, a.id, start=not a.no_start, force_reason=a.force)
    ops.send_notes(conn, me, notes)
    _done(a, res, f'{res["key"]} is yours' + ('' if a.no_start else ' (in_progress)'))


def cmd_release(conn, me, a):
    res, notes = ops.release(conn, me, a.id, note=a.note)
    ops.send_notes(conn, me, notes)
    _done(a, res, f'{res["key"]} released')


def cmd_assign(conn, me, a):
    res, notes = ops.assign(conn, me, a.id, a.who, propose=a.propose)
    ops.send_notes(conn, me, notes)
    _done(a, res, f'{res["key"]} ' + (f'proposed for {a.who}' if a.propose else f'-> {res["assignee"] or "nobody"}'))


def cmd_move(conn, me, a):
    res, notes = ops.move(conn, me, a.id, a.status, force_reason=a.force, proof=a.proof, note=a.note,
                          reviewer=a.reviewer)
    ops.send_notes(conn, me, notes)
    _done(a, res, f'{res["key"]}: {res.get("from", res["status"])} -> {res["status"]}')


def cmd_review(conn, me, a):
    if a.request:
        res, notes = ops.move(conn, me, a.id, 'in_review', proof=a.proof, note=a.note, reviewer=a.request)
    else:
        if not (a.approve or a.reject):
            raise TaskError('review needs --approve, --reject or --request <name>')
        res, notes = ops.review(conn, me, a.id, 'approve' if a.approve else 'reject', note=a.note)
    ops.send_notes(conn, me, notes)
    _done(a, res, f'{res["key"]}: {res["status"]}')


def cmd_comment(conn, me, a):
    res, notes = ops.comment(conn, me, a.id, ' '.join(a.text))
    ops.send_notes(conn, me, notes)
    _done(a, res, f'commented on {res["key"]}')


def cmd_link(conn, me, a):
    kind, _, value = a.link.partition(':')
    res, _ = ops.link(conn, me, a.id, kind, value, remove=a.remove)
    _done(a, res, f'{res["key"]}: {"-" if a.remove else "+"}{a.link}')


def cmd_block(conn, me, a):
    res, notes = ops.block(conn, me, a.id, a.by, remove=a.remove)
    ops.send_notes(conn, me, notes)
    _done(a, res, f'{res["key"]} {"no longer waits" if a.remove else "waits"} on {res["blocked_by"]}')


def cmd_touch(conn, me, a):
    res, notes = ops.touch(conn, me, a.id, add=a.globs or [], remove=a.remove or [], force_reason=a.force)
    ops.send_notes(conn, me, notes)
    _done(a, res, f'{res["key"]}: touches updated')


def cmd_edit(conn, me, a):
    body = Path(a.body_file).read_text(encoding='utf-8') if a.body_file else a.body
    res, _ = ops.edit(conn, me, a.id, labels_add=_labels(a.label), labels_remove=_labels(a.unlabel), title=a.title,
                      body=body, priority=a.priority, area=a.area, due=a.due, reviewer=a.reviewer, parent=a.parent)
    _done(a, res, f'{res["key"]}: changed {", ".join(res["changed"]) or "nothing"}')


def cmd_conflicts(conn, me, a):
    cs = db.all_conflicts(conn)
    if a.json:
        return out_json(cs)
    if not cs:
        print(c('no file conflicts between in-progress tasks', '32'))
    for x in cs:
        print(c('⚠ ', '31') + f'{c(x["a"], "1")} ({x["a_assignee"]}) {short(x["a_title"], 40)}  ×  '
              f'{c(x["b"], "1")} ({x["b_assignee"]}) {short(x["b_title"], 40)}')
        print(c(f'    {x["a_glob"]}  overlaps  {x["b_glob"]}', '90'))


def cmd_labels(conn, me, a):
    used = dict(conn.execute('SELECT label, COUNT(*) FROM labels GROUP BY label').fetchall())
    vocab = db.cfg().labels
    res = {'vocabulary': vocab, 'used': used}
    if a.json:
        return out_json(res)
    print('vocabulary: ' + (', '.join(vocab) or '(any label)'))
    for k, v in sorted(used.items()):
        print(f'  {k:<20} {v}')


def stats(conn) -> dict:
    by = {s: 0 for s in db.STATUSES}
    for r in conn.execute('SELECT status, COUNT(*) FROM issues GROUP BY status'):
        by[r[0]] = r[1]
    done_today = conn.execute("SELECT COUNT(*) FROM issues WHERE status='done' AND substr(closed_at,1,10)=?",
                              (db.today(),)).fetchone()[0]
    areas, labels = {}, {}
    for r in conn.execute("SELECT COALESCE(area,'-') a, status, COUNT(*) n FROM issues GROUP BY a, status"):
        areas.setdefault(r['a'], {s: 0 for s in db.STATUSES})[r['status']] = r['n']
    for r in conn.execute('SELECT l.label, i.status, COUNT(*) n FROM labels l JOIN issues i ON i.id=l.issue '
                          'GROUP BY l.label, i.status'):
        labels.setdefault(r['label'], {s: 0 for s in db.STATUSES})[r['status']] = r['n']
    last = conn.execute('SELECT MAX(id) FROM events').fetchone()[0] or 0
    return {'by_status': by, 'done_today': done_today, 'conflicts': len(db.all_conflicts(conn)),
            'proposed': conn.execute("SELECT COUNT(*) FROM issues WHERE proposed IS NOT NULL AND proposed<>'' "
                                     "AND status NOT IN ('done','cancelled')").fetchone()[0],
            'areas': areas, 'labels': labels, 'last_event': last}


def pct(d):
    tot = sum(v for k, v in d.items() if k != 'cancelled')
    return (100 * d.get('done', 0) // tot) if tot else 0, tot


def bar(p, n):
    full = p * n // 100
    return c('█' * full, '32') + c('░' * (n - full), '90')


def cmd_stats(conn, me, a):
    s = stats(conn)
    if a.json:
        return out_json(s)
    b = s['by_status']
    print(f'{c("◐ " + str(b["in_progress"]) + " in progress", "33")}  {c("◑ " + str(b["in_review"]) + " in review", "35")}'
          f'  {c("⊘ " + str(b["blocked"]) + " blocked", "31")}  {c("● " + str(s["done_today"]) + " done today", "32")}'
          f'  {b["todo"]} todo  {b["backlog"]} backlog'
          + (c(f'  ⚠ {s["conflicts"]} conflicts', '91') if s['conflicts'] else ''))
    for title, group in (('areas', s['areas']), ('labels', s['labels'])):
        if not group:
            continue
        print(c(f'\n{title}', '1'))
        for k, d in sorted(group.items()):
            p, tot = pct(d)
            print(f'  {k:<16} {bar(p, 20)} {p:>3}%  {d["done"]}/{tot} done, {d["in_progress"]} active, '
                  f'{d["in_review"]} review, {d["blocked"]} blocked')


def cmd_board(conn, me, a):
    cols = a.columns.split(',') if a.columns else ['todo', 'in_progress', 'in_review', 'blocked', 'done']
    rows = db.query(conn, status=cols, area=a.area, assignee=a.assignee, priority=a.priority, label=a.label)
    ds = [db.issue_dict(conn, r) for r in rows]
    if a.json:
        return out_json({s: [d for d in ds if d['status'] == s] for s in cols})
    w = width()

    def group(s):
        grp = [d for d in ds if d['status'] == s]
        return [d for d in grp if (d['closed_at'] or '')[:10] == db.today()] if s == 'done' else grp

    if w < 100:  # narrow: grouped list
        for s in cols:
            grp = group(s)
            print(c(f'\n{STATUS_ICON[s]} {s.upper()}  {len(grp)}', STATUS_COLOR[s] + ';1'))
            for d in grp[:a.per]:
                print('  ' + line(d, w - 2))
        return
    cw = (w - len(cols) + 1) // len(cols)
    grid = []
    for s in cols:
        grp = group(s)
        cells = [c(f'{STATUS_ICON[s]} {s.upper()} {len(grp)}'.ljust(cw), STATUS_COLOR[s] + ';1'), c('─' * cw, '90')]
        for d in grp[:a.per]:
            head = f'{d["key"]} {PRIO_ICON[d["priority"]]} {d["assignee"] or "-"}'
            cells.append(c(short(head, cw).ljust(cw), PRIO_COLOR[d['priority']]))
            cells.append(short(' ' + d['title'], cw).ljust(cw))
        if len(grp) > a.per:
            cells.append(c(f' +{len(grp) - a.per} more'.ljust(cw), '90'))
        grid.append(cells)
    for i in range(max(len(g) for g in grid)):
        print(c('│', '90').join(g[i] if i < len(g) else ' ' * cw for g in grid))


def _events(conn, rows):
    titles = {r[0]: r[1] for r in conn.execute('SELECT id, title FROM issues')}
    return [{'id': r['id'], 'ts': r['ts'], 'actor': r['actor'], 'key': fmt_key(r['issue']) if r['issue'] else None,
             'title': titles.get(r['issue']), 'action': r['action'], 'detail': json.loads(r['detail'])} for r in rows]


def cmd_log(conn, me, a):
    sql, args = 'SELECT * FROM events WHERE id>?', [a.since or 0]
    if a.id:
        sql += ' AND issue=?'
        args.append(db.parse_key(a.id))
    if a.actor:
        sql += ' AND actor=?'
        args.append(a.actor)
    evs = _events(conn, conn.execute(sql + ' ORDER BY id DESC LIMIT ?', args + [a.limit]).fetchall()[::-1])
    if a.json:
        return out_json(evs)
    for e in evs:
        det = ' '.join(f'{k}={v}' for k, v in e['detail'].items() if v not in (None, '', [], {}))
        print(f'{c(e["ts"][5:16], "90")} {c(e["actor"], "36"):<14} {e["action"]:<10} {c(e["key"] or "", "1"):<8} '
              f'{short(e["title"], 40)} {c(short(det, 60), "90")}')


def cmd_whoami(conn, me, a):
    res = {'name': me.name, 'level': me.level, 'via': me.via}
    if a.json:
        return out_json(res)
    print(f'{me.name} ({me.level}, via {me.via})')


def snapshot(conn, me, area=None, priority=None, assignee=None, label=None, since=None, per=40) -> dict:
    """Everything a live view needs in one read (the Claude Code mod polls this)."""
    cols = ['todo', 'in_progress', 'in_review', 'blocked', 'done']
    rows = db.query(conn, status=cols, area=area, assignee=assignee, priority=priority, label=label)
    board = {s: [] for s in cols}
    counts = {s: 0 for s in cols}
    for r in rows:
        if r['status'] == 'done' and (r['closed_at'] or '')[:10] != db.today():
            continue
        counts[r['status']] += 1
        if len(board[r['status']]) < per:
            board[r['status']].append({'key': fmt_key(r['id']), 'title': r['title'], 'priority': r['priority'],
                                       'assignee': r['assignee'], 'area': r['area'], 'proposed': r['proposed']})
    st = stats(conn)
    since = st['last_event'] if since is None else since
    events = _events(conn, conn.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT 200', (since,)))
    return {'me': {'name': me.name, 'level': me.level, 'via': me.via}, 'prefix': db.cfg().prefix, 'stats': st,
            'counts': counts, 'board': board, 'events': events, 'conflicts': db.all_conflicts(conn),
            'areas': sorted(k for k in st['areas'] if k != '-'), 'labels': db.cfg().labels or sorted(st['labels']),
            'review_for_me': [fmt_key(r[0]) for r in conn.execute(
                "SELECT id FROM issues WHERE reviewer=? AND status='in_review'", (me.name,))],
            'unread_notes': _unread(conn, me.name)}


def cmd_snapshot(conn, me, a):
    out_json(snapshot(conn, me, area=a.area, priority=a.priority, assignee=a.assignee, label=a.label, since=a.since,
                      per=a.per))


def cmd_import(conn, me, a):
    import importers
    db.can(me, 'import')
    res = importers.run_all(conn, me, only=a.only, dry=a.dry_run)
    if a.json:
        return out_json(res)
    for k, v in res.items():
        print(f'{k}: ' + (', '.join(f'{kk} {vv}' for kk, vv in v.items()) or 'no records'))


def cmd_export(conn, me, a):
    import views
    path = views.write_board_md(conn, Path(a.out) if a.out else None)
    print(f'wrote {path}')


# ------------------------------------------------------------------------------------------------ parser

def build_parser():
    p = argparse.ArgumentParser(prog='task.py', description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--as', dest='as_name', help='act as this name (default $TASKS_AS, else "user")')
    p.add_argument('--db', help='tracker database (default .claude/tasks/tasks.db under the project root)')
    p.add_argument('--roles', help='roles file (default .claude/tasks/roles.toml)')
    p.add_argument('--prefix', help='task id prefix (default TASK)')
    p.add_argument('--labels', help='label vocabulary, comma-separated ("" = any label)')
    p.add_argument('--importers', help='import sources, comma-separated')
    p.add_argument('--notify-cmd', help='command run per notification: {to} {sender} {key} {prio} {text}')
    sub = p.add_subparsers(dest='cmd', required=True)

    def sp(name, fn, help, read=False):
        s = sub.add_parser(name, help=help)
        s.set_defaults(fn=fn, read=read)
        s.add_argument('--json', action='store_true')
        return s

    def filters(s):
        s.add_argument('--area')
        s.add_argument('--assignee', help="a name, or 'none'")
        s.add_argument('--priority')
        s.add_argument('--label')

    s = sp('init', cmd_init, 'create the tracker database and a roles.toml template')
    s.add_argument('--no-roles', action='store_true', help='do not write the roles template')
    sp('where', cmd_where, 'the resolved configuration: root, db, roles, prefix, labels', read=True)

    s = sp('new', cmd_new, 'create a task')
    s.add_argument('title')
    s.add_argument('--body', '-d')
    s.add_argument('--body-file')
    s.add_argument('--status', choices=db.STATUSES)
    s.add_argument('--priority', '-p', default='med', choices=db.PRIORITIES)
    s.add_argument('--assign', '-a', help='a member may only name themselves; another name becomes a proposal')
    s.add_argument('--reviewer', '-r')
    s.add_argument('--area')
    s.add_argument('--parent')
    s.add_argument('--due')
    s.add_argument('--label', '-l', action='append', help='repeatable or comma-separated')
    s.add_argument('--link', action='append', help='kind:value (ref:x url:https://... doc:path proof:path)')
    s.add_argument('--touches', '-t', action='append', help='file glob this task will edit (repeatable)')
    s.add_argument('--blocked-by', action='append')
    s.add_argument('--dup-ok', action='store_true', help='create even if it looks like a duplicate')
    s.add_argument('--force', metavar='REASON', help='start despite a file conflict (logged)')

    s = sp('list', cmd_list, 'list tasks (open by default)', read=True)
    filters(s)
    s.add_argument('--status', help='comma list')
    s.add_argument('--grep', help='substring in title or body')
    s.add_argument('--all', action='store_true', help='include done and cancelled')
    s.add_argument('--limit', type=int)

    s = sp('mine', cmd_mine, 'your open tasks + reviews waiting on you (one line each)', read=True)
    s.add_argument('who', nargs='?')

    s = sp('inbox', cmd_inbox, 'notes addressed to you (marks them read)', read=True)
    s.add_argument('who', nargs='?')
    s.add_argument('--all', action='store_true', help='include read notes')
    s.add_argument('--keep', action='store_true', help='do not mark them read')
    s.add_argument('--limit', type=int, default=50)

    s = sp('show', cmd_show, 'one task with comments and history', read=True)
    s.add_argument('id')
    s.add_argument('--full', action='store_true')
    s.add_argument('--last', type=int)

    s = sp('claim', cmd_claim, 'take a task (atomic) and start it')
    s.add_argument('id')
    s.add_argument('--no-start', action='store_true')
    s.add_argument('--force', metavar='REASON')

    s = sp('release', cmd_release, 'give a task back (no assignee, back to todo)')
    s.add_argument('id')
    s.add_argument('--note', '-m')

    s = sp('assign', cmd_assign, 'assign (lead or admin), or --propose someone')
    s.add_argument('id')
    s.add_argument('who', help="a name, or 'none'")
    s.add_argument('--propose', action='store_true')

    s = sp('move', cmd_move, 'change status')
    s.add_argument('id')
    s.add_argument('status', choices=db.STATUSES)
    s.add_argument('--proof')
    s.add_argument('--note', '-m')
    s.add_argument('--reviewer')
    s.add_argument('--force', metavar='REASON')

    s = sp('review', cmd_review, 'approve or reject, or --request a reviewer')
    s.add_argument('id')
    g = s.add_mutually_exclusive_group()
    g.add_argument('--approve', action='store_true')
    g.add_argument('--reject', action='store_true')
    g.add_argument('--request', metavar='NAME')
    s.add_argument('--proof')
    s.add_argument('--note', '-m')

    s = sp('comment', cmd_comment, 'comment (@name mentions notify)')
    s.add_argument('id')
    s.add_argument('text', nargs='+')

    s = sp('link', cmd_link, 'add or remove a link kind:value')
    s.add_argument('id')
    s.add_argument('link')
    s.add_argument('--remove', action='store_true')

    s = sp('block', cmd_block, 'mark ID as waiting on BY')
    s.add_argument('id')
    s.add_argument('by')
    s.add_argument('--remove', action='store_true')

    s = sp('touch', cmd_touch, 'set the file globs a task edits')
    s.add_argument('id')
    s.add_argument('globs', nargs='*')
    s.add_argument('--remove', action='append')
    s.add_argument('--force', metavar='REASON')

    s = sp('edit', cmd_edit, 'edit fields or labels')
    s.add_argument('id')
    for f in ('title', 'body', 'body-file', 'area', 'due', 'reviewer', 'parent'):
        s.add_argument(f'--{f}')
    s.add_argument('--priority', choices=db.PRIORITIES)
    s.add_argument('--label', action='append')
    s.add_argument('--unlabel', action='append')

    sp('conflicts', cmd_conflicts, 'in-progress tasks whose touches overlap', read=True)
    sp('labels', cmd_labels, 'the label vocabulary and how often each label is used', read=True)
    sp('stats', cmd_stats, 'counts + progress per area and label', read=True)
    s = sp('board', cmd_board, 'kanban columns (grouped list when narrow)', read=True)
    filters(s)
    s.add_argument('--columns')
    s.add_argument('--per', type=int, default=12)
    s = sp('log', cmd_log, 'the event log', read=True)
    s.add_argument('id', nargs='?')
    s.add_argument('--actor')
    s.add_argument('--since', type=int, help='events after this id')
    s.add_argument('--limit', type=int, default=40)
    s = sp('snapshot', cmd_snapshot, 'one JSON read for live views (stats, board, new events)', read=True)
    filters(s)
    s.add_argument('--since', type=int, help='events after this id (default: none)')
    s.add_argument('--per', type=int, default=40)
    sp('whoami', cmd_whoami, 'who the tracker thinks you are', read=True)
    s = sp('import', cmd_import, 'run the configured importers (idempotent)')
    s.add_argument('--only', help='comma list of importer entries')
    s.add_argument('--dry-run', action='store_true')
    s = sp('export', cmd_export, 'write a markdown board (default: TASKS_BOARD.md beside the db)', read=True)
    s.add_argument('--out')
    return p


def configure_from_args(a, env=None):
    flags = {'dbPath': a.db, 'rolesFile': a.roles, 'idPrefix': a.prefix, 'labels': a.labels,
             'importers': a.importers, 'notifyCommand': a.notify_cmd}
    return db.configure(config.load(flags, env=env))


def main(argv=None):
    _enc_safe()
    a = build_parser().parse_args(argv)
    try:
        configure_from_args(a)
        me = db.resolve_actor(a.as_name)
        conn = None if a.cmd in ('init', 'where') else db.connect()
        try:
            a.fn(conn, me, a)
        finally:
            if conn is not None:
                conn.close()
        return 0
    except Denied as e:
        print(c(str(e), '31'), file=sys.stderr)
        return 3
    except db.NoTracker as e:
        print(c(f'error: {e}', '31'), file=sys.stderr)
        return 4
    except TaskError as e:
        print(c(f'error: {e}', '31'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
