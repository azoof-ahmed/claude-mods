"""The task board: a keyboard-driven terminal app over the tracker (Textual, an optional dependency).

  python scripts/board.py                  # board / list / progress / conflicts, live
  python scripts/board.py --area api       # global options as for task.py: --db --roles --prefix

Keys: 1-4 switch views, enter opens a task, / searches, a / p / l cycle area / priority / label filters, c clears
them, r refreshes, q quits. The view refreshes by itself when anyone changes the tracker.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (DataTable, Footer, Input, Label, ListItem, ListView, Markdown, Static, TabbedContent,
                                 TabPane)
except ImportError:  # the CLI never needs it; only this viewer does
    print('board.py needs Textual: python -m pip install textual', file=sys.stderr)
    raise SystemExit(2)

import config  # noqa: E402
import taskdb as db  # noqa: E402

COLS = ['todo', 'in_progress', 'in_review', 'blocked', 'done']
LABEL = {'backlog': 'Backlog', 'todo': 'Todo', 'in_progress': 'In progress', 'in_review': 'In review',
         'blocked': 'Blocked', 'done': 'Done today', 'cancelled': 'Cancelled'}
ICON = {'backlog': '◌', 'todo': '○', 'in_progress': '◐', 'in_review': '◑', 'blocked': '⊘',
        'done': '●', 'cancelled': '✕'}
COLOR = {'backlog': '#6b7280', 'todo': '#d1d5db', 'in_progress': '#f5b041', 'in_review': '#b388ff',
         'blocked': '#ff5c5c', 'done': '#4ade80', 'cancelled': '#6b7280'}
PICON = {'urgent': '‼', 'high': '▲', 'med': '■', 'low': '▽'}
PCOLOR = {'urgent': '#ff5c5c', 'high': '#f5b041', 'med': '#9ca3af', 'low': '#6b7280'}
PRIOS = ['', 'urgent', 'high', 'med', 'low']


def esc(s) -> str:
    return str(s or '').replace('[', r'\[')


def short(s, n) -> str:
    s = (s or '').replace('\n', ' ')
    return s if len(s) <= n else s[:max(0, n - 1)] + '…'


class Card(ListItem):
    def __init__(self, d: dict, width: int = 30):
        self.key_ = d['key']
        who = d['assignee'] or (f'?{d["proposed"]}' if d.get('proposed') else '—')
        txt = (f'[{PCOLOR[d["priority"]]}]{PICON[d["priority"]]}[/] [b]{d["key"]}[/] [#7dd3fc]{esc(short(who, 14))}[/]\n'
               f'{esc(short(d["title"], width * 2 - 2))}')
        super().__init__(Label(txt, classes='card-text'))


class DetailScreen(ModalScreen):
    BINDINGS = [Binding('escape,q,backspace', 'app.pop_screen', 'Back')]

    def __init__(self, key: str):
        super().__init__()
        self.key_ = key

    def compose(self) -> ComposeResult:
        conn = db.connect()
        d = db.issue_dict(conn, db.get_issue(conn, self.key_), full=True)
        st = d['status']
        head = (f'[b]{d["key"]}[/]  [b]{esc(d["title"])}[/]\n'
                f'[{COLOR[st]}]{ICON[st]} {LABEL[st]}[/]   [{PCOLOR[d["priority"]]}]{PICON[d["priority"]]} {d["priority"]}[/]'
                f'   assignee [#7dd3fc]{esc(d["assignee"] or "—")}[/]'
                + (f'   proposed [#b388ff]{esc(d["proposed"])}[/]' if d['proposed'] else '')
                + f'   reviewer {esc(d["reviewer"] or "—")}   area {esc(d["area"] or "—")}   by {esc(d["creator"])}')
        meta = []
        if d['labels']:
            meta.append(' '.join(f'[#9ca3af]#{esc(x)}[/]' for x in d['labels']))
        if d['links']:
            meta.append('links  ' + '  '.join(f'{x["kind"]}:{esc(x["value"])}' for x in d['links']))
        if d['touches']:
            meta.append('[#f5b041]touches[/]  ' + ', '.join(esc(x) for x in d['touches']))
        if d['blocked_by'] or d['blocks']:
            meta.append(f'[#ff5c5c]waits on[/] {", ".join(d["blocked_by"]) or "—"}   blocks {", ".join(d["blocks"]) or "—"}')
        if d['parent']:
            meta.append(f'parent {d["parent"]}')
        with VerticalScroll(id='detail'):
            yield Static(head, id='detail-head')
            if meta:
                yield Static('\n'.join(meta), id='detail-meta')
            if d['body'].strip():
                yield Markdown(d['body'])
            if d['children']:
                yield Static('[b]Sub-issues[/]\n' + '\n'.join(
                    f'  [{COLOR[c["status"]]}]{ICON[c["status"]]}[/] [b]{c["key"]}[/] [#7dd3fc]{esc(c["assignee"] or "—")}[/] '
                    f'{esc(short(c["title"], 90))}' for c in d['children']))
            if d['comments']:
                yield Static('[b]Comments[/]\n' + '\n'.join(
                    f'  [#6b7280]{c["ts"][5:16].replace("T", " ")}[/] [#7dd3fc]{esc(c["author"])}[/] {esc(c["body"])}'
                    for c in d['comments'][-15:]))
            yield Static('[b]History[/]\n' + '\n'.join(
                f'  [#6b7280]{h["ts"][5:16].replace("T", " ")}[/] [#7dd3fc]{esc(h["actor"])}[/] {h["action"]} '
                f'[#6b7280]{esc(short(" ".join(f"{k}={v}" for k, v in h["detail"].items() if v not in (None, "", [], {})), 100))}[/]'
                for h in d['history'][-20:]))
        yield Footer()


class Board(App):
    TITLE = 'Tasks'
    CSS = """
    Screen { background: $surface; }
    #filters { height: 1; padding: 0 1; color: $text-muted; }
    #search { display: none; height: 3; }
    #search.on { display: block; }
    .col { width: 1fr; border: round $panel-lighten-2; margin: 0 0 0 0; }
    .col-title { height: 1; padding: 0 1; text-style: bold; }
    .col ListView { background: transparent; }
    Card { padding: 0 1; height: auto; }
    Card.-highlight { background: $boost; }
    .card-head, .card-title { width: 100%; text-wrap: nowrap; text-overflow: ellipsis; }
    .card-title { color: $text; margin-bottom: 1; }
    #stats { height: 1; padding: 0 1; }
    #progress, #conflicts { padding: 1 2; }
    DetailScreen { align: center middle; }
    #detail { width: 90%; height: 90%; border: round $accent; background: $panel; padding: 1 2; }
    #detail-head { margin-bottom: 1; }
    #detail-meta { margin-bottom: 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding('1', "show('board')", 'Board'), Binding('2', "show('list')", 'List'),
        Binding('3', "show('progress')", 'Progress'), Binding('4', "show('conflicts')", 'Conflicts'),
        Binding('slash', 'search', 'Search'), Binding('a', 'cycle_area', 'Area'), Binding('p', 'cycle_prio', 'Prio'),
        Binding('l', 'cycle_label', 'Label'), Binding('c', 'clear', 'Clear'), Binding('r', 'refresh', 'Refresh'),
        Binding('q', 'quit', 'Quit'),
    ]

    def __init__(self, area='', priority='', label='', poll=2.0):
        super().__init__()
        self.f = {'area': area, 'priority': priority, 'label': label, 'text': ''}
        self.poll = poll
        self._stamp = None
        self._areas = ['']
        self._labels = ['']

    # ---------------------------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Static(id='stats')
        yield Static(id='filters')
        yield Input(placeholder='search title / body, enter to apply, esc to clear', id='search')
        with TabbedContent(initial='board', id='tabs'):
            with TabPane('Board', id='board'):
                with Horizontal():
                    for st in COLS:
                        with Vertical(classes='col', id=f'col-{st}'):
                            yield Static(classes='col-title', id=f'title-{st}')
                            yield ListView(id=f'lv-{st}')
            with TabPane('List', id='list'):
                yield DataTable(id='table', cursor_type='row', zebra_stripes=True)
            with TabPane('Progress', id='progress-tab'):
                yield VerticalScroll(Static(id='progress'))
            with TabPane('Conflicts', id='conflicts-tab'):
                yield VerticalScroll(Static(id='conflicts'))
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one('#table', DataTable)
        t.add_columns('id', 'status', 'prio', 'assignee', 'area', 'labels', 'title')
        self.reload()
        self.query_one('#lv-todo', ListView).focus()
        if self.size.width < 100:
            self.call_after_refresh(self.action_show, 'list')
        if self.poll:
            self.set_interval(self.poll, self.check)

    # ---------------------------------------------------------------------------------- data
    def check(self) -> None:
        stamp = tuple(p.stat().st_mtime_ns if p.exists() else 0
                      for p in (db.cfg().db_path, db.cfg().db_path.with_name(db.cfg().db_path.name + '-wal')))
        if stamp != self._stamp:
            self._stamp = stamp
            self.reload()

    def reload(self) -> None:
        conn = db.connect()
        f = self.f
        rows = db.query(conn, area=f['area'] or None, priority=f['priority'] or None, label=f['label'] or None,
                        text=f['text'] or None, include_closed=True)
        today = db.today()
        ds = [db.issue_dict(conn, r) for r in rows
              if r['status'] != 'cancelled' and (r['status'] != 'done' or (r['closed_at'] or '')[:10] == today)]
        by = dict(conn.execute('SELECT status, COUNT(*) FROM issues GROUP BY status').fetchall())
        done_today = conn.execute("SELECT COUNT(*) FROM issues WHERE status='done' AND substr(closed_at,1,10)=?",
                                  (today,)).fetchone()[0]
        conflicts = db.all_conflicts(conn)
        self._areas = [''] + [r[0] for r in conn.execute("SELECT DISTINCT area FROM issues WHERE area IS NOT NULL "
                                                         "ORDER BY area")]
        self._labels = [''] + (db.cfg().labels or [r[0] for r in conn.execute('SELECT DISTINCT label FROM labels '
                                                                                'ORDER BY label')])
        self.query_one('#stats', Static).update(
            f'[b]▤ Tasks[/]   [{COLOR["in_progress"]}]◐ {by.get("in_progress", 0)} active[/]   '
            f'[{COLOR["in_review"]}]◑ {by.get("in_review", 0)} review[/]   [{COLOR["blocked"]}]⊘ {by.get("blocked", 0)} blocked[/]'
            f'   [{COLOR["done"]}]● {done_today} today[/]   [#9ca3af]{by.get("todo", 0)} todo · {by.get("backlog", 0)} backlog[/]'
            + (f'   [b #ff5c5c]⚠ {len(conflicts)} conflicts[/]' if conflicts else ''))
        parts = [f'area [b]{f["area"] or "all"}[/]', f'priority [b]{f["priority"] or "any"}[/]',
                 f'label [b]{f["label"] or "any"}[/]'] + ([f'search [b]{esc(f["text"])}[/]'] if f['text'] else [])
        self.query_one('#filters', Static).update('  ·  '.join(parts) + '   [#6b7280](a p l cycle, / search, c clear)[/]')
        width = max(16, (self.size.width - 10) // len(COLS))
        for st in COLS:
            grp = [d for d in ds if d['status'] == st]
            self.query_one(f'#title-{st}', Static).update(f'[{COLOR[st]}]{ICON[st]} {LABEL[st]}[/] [#6b7280]{len(grp)}[/]')
            lv = self.query_one(f'#lv-{st}', ListView)
            idx = lv.index
            lv.clear()
            lv.extend(Card(d, width) for d in grp[:200])
            if idx is not None and grp:
                lv.index = min(idx, len(grp) - 1)
        t = self.query_one('#table', DataTable)
        t.clear()
        order = {s: i for i, s in enumerate(['in_progress', 'in_review', 'blocked', 'todo', 'backlog', 'done'])}
        for d in sorted(ds, key=lambda d: (order.get(d['status'], 9), db.PRIORITIES.index(d['priority']), d['id'])):
            st = d['status']
            t.add_row(f'[b]{d["key"]}[/]', f'[{COLOR[st]}]{ICON[st]} {LABEL[st]}[/]',
                      f'[{PCOLOR[d["priority"]]}]{PICON[d["priority"]]} {d["priority"]}[/]',
                      f'[#7dd3fc]{esc(d["assignee"] or "—")}[/]', esc(d['area'] or ''), esc(','.join(d['labels'])),
                      esc(short(d['title'], 90)), key=d['key'])
        self.query_one('#progress', Static).update(self.progress_text(conn))
        self.query_one('#conflicts', Static).update(self.conflicts_text(conflicts))

    @staticmethod
    def bar(d, n=28):
        tot = sum(v for k, v in d.items() if k != 'cancelled')
        pct = (100 * d.get('done', 0) // tot) if tot else 0
        full = pct * n // 100
        return (f'[{COLOR["done"]}]{"█" * full}[/][#374151]{"░" * (n - full)}[/] [b]{pct:>3}%[/] '
                f'[#9ca3af]{d.get("done", 0)}/{tot}[/]  [{COLOR["in_progress"]}]◐{d.get("in_progress", 0)}[/] '
                f'[{COLOR["in_review"]}]◑{d.get("in_review", 0)}[/] [{COLOR["blocked"]}]⊘{d.get("blocked", 0)}[/]')

    def progress_text(self, conn) -> str:
        by_label, areas = {}, {}
        for r in conn.execute('SELECT l.label, i.status, COUNT(*) FROM labels l JOIN issues i ON i.id=l.issue '
                              'GROUP BY l.label, i.status'):
            by_label.setdefault(r[0], {})[r[1]] = r[2]
        for r in conn.execute("SELECT COALESCE(area,'-'), status, COUNT(*) FROM issues GROUP BY 1, 2"):
            areas.setdefault(r[0], {})[r[1]] = r[2]
        out = ['[b]Labels[/]']
        out += [f'  {esc(k):<24} {self.bar(d)}' for k, d in sorted(by_label.items())]
        out += ['', '[b]Areas[/]']
        out += [f'  {esc(k):<24} {self.bar(d)}' for k, d in sorted(areas.items()) if k != '-']
        return '\n'.join(out)

    @staticmethod
    def conflicts_text(cs) -> str:
        if not cs:
            return f'[{COLOR["done"]}]✓ No file conflicts between in-progress tasks.[/]'
        return '\n\n'.join(f'[#ff5c5c]⚠[/] [b]{x["a"]}[/] [#7dd3fc]{esc(x["a_assignee"])}[/] {esc(short(x["a_title"], 50))}'
                           f'  ×  [b]{x["b"]}[/] [#7dd3fc]{esc(x["b_assignee"])}[/] {esc(short(x["b_title"], 50))}\n'
                           f'   [#6b7280]{esc(x["a_glob"])}  overlaps  {esc(x["b_glob"])}[/]' for x in cs)

    def on_resize(self, ev) -> None:
        # five columns need ~100 cells; narrower, the grouped list is the board
        if ev.size.width < 100 and self.query_one('#tabs', TabbedContent).active == 'board':
            self.action_show('list')

    # ---------------------------------------------------------------------------------- actions
    def action_show(self, tab: str) -> None:
        self.query_one('#tabs', TabbedContent).active = {'progress': 'progress-tab',
                                                          'conflicts': 'conflicts-tab'}.get(tab, tab)
        self._refocus()

    def _cycle(self, field, values):
        cur = self.f[field]
        self.f[field] = values[(values.index(cur) + 1) % len(values)] if cur in values else ''
        self.reload()

    def action_cycle_area(self):
        self._cycle('area', self._areas)

    def action_cycle_prio(self):
        self._cycle('priority', PRIOS)

    def action_cycle_label(self):
        self._cycle('label', self._labels)

    def action_clear(self):
        self.f = {'area': '', 'priority': '', 'label': '', 'text': ''}
        self.reload()

    def action_refresh(self):
        self.reload()

    def action_search(self):
        box = self.query_one('#search', Input)
        box.add_class('on')
        box.focus()

    @on(Input.Submitted, '#search')
    def _search(self, ev: Input.Submitted):
        self.f['text'] = ev.value.strip()
        ev.input.remove_class('on')
        self.reload()
        self._refocus()

    def _refocus(self):
        tab = self.query_one('#tabs', TabbedContent).active
        target = {'list': '#table'}.get(tab, '#lv-todo' if tab == 'board' else None)
        if target:
            self.query_one(target).focus()

    def on_key(self, ev) -> None:
        box = self.query_one('#search', Input)
        if ev.key == 'escape' and box.has_class('on'):
            box.value = ''
            box.remove_class('on')
            self.f['text'] = ''
            self.reload()
            self._refocus()

    @on(ListView.Selected)
    def _open_card(self, ev: ListView.Selected):
        if isinstance(ev.item, Card):
            self.push_screen(DetailScreen(ev.item.key_))

    @on(DataTable.RowSelected, '#table')
    def _open_row(self, ev: DataTable.RowSelected):
        self.push_screen(DetailScreen(ev.row_key.value))


def main(argv=None):
    p = argparse.ArgumentParser(description='Task board (Textual)')
    for flag in ('--db', '--roles', '--prefix'):
        p.add_argument(flag)
    p.add_argument('--area', default='')
    p.add_argument('--label', default='')
    p.add_argument('--priority', default='')
    a = p.parse_args(argv)
    db.configure(config.load({'dbPath': a.db, 'rolesFile': a.roles, 'idPrefix': a.prefix}))
    db.connect().close()  # fails with the init hint when there is no tracker yet
    Board(a.area, a.priority, a.label).run()


if __name__ == '__main__':
    main()
