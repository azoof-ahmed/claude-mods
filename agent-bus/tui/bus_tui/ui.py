"""The Textual application: modal vim keys, leader + which-key, picker, statusline, bufferline, tree + messages."""
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, Label, OptionList, Static, Tree
from textual.widgets.option_list import Option

from orca_bus import chat
from orca_bus.store import BusError

from . import themes
from .model import REACTIONS, Model, age_text
from .screens import Compose, Detail, Picker, React, WhichKey

NARROW = 100  # below this many columns the tree hides (Space e shows it anyway)
POLL_S = 1.0

LEADER = [
    ('f', 'find: threads, roles, channels, messages'),
    ('r', 'roles'),
    ('t', 'threads'),
    ('b', 'channels / tabs'),
    ('p', 'pinned'),
    ('e', 'toggle tree'),
    ('n', 'new message (compose)'),
    ('u', 'filter: unread'),
    ('@', 'filter: to me'),
    ('m', 'mark view read'),
    ('T', 'theme'),
    ('h', 'help (keymap)'),
    ('q', 'quit'),
]

HELP = """NORMAL
  j/k ↓/↑     move              gg / G       first / last
  ctrl-d/u    half page         enter / o    message detail
  i / a       insert (write)    c            compose (modal)
  r           reply (modal)     e / +        react
  p           pin / unpin       s            follow / unfollow thread
  t           open thread tab   x            close thread tab
  H / L, gT/gt  prev / next tab tab          focus tree / list
  /           search            n / N        next / previous match
  :           command line      esc          clear search, then filters
  y           copy message id   ?            this help
  space       leader (which-key popup)

INSERT        enter send to the current tab (channel, or reply in a thread) · esc normal

COMMAND LINE
  :reply <text>   :react <emoji> [off]   :send <to> <text>   :thread [id]
  :pin  :unpin  :sub  :unsub  :edit <text>  :delete  :seen
  :filter [@role|#chan|mentions|unread|words...|clear]   :theme <name>   :tree   :q

LEADER (space)
""" + '\n'.join(f'  {k}  {d}' for k, d in LEADER)

CSS = """
Screen { background: $background; }
#tabs { height: 1; background: $panel; }
#main { height: 1fr; }
#tree { width: 30; background: $panel; border-right: tall $surface; padding: 0 0; }
#tree:focus { border-right: tall $primary; }
#list { height: 1fr; background: $background; border: none; padding: 0 0; }
#list:focus { border: none; }
#list > .option-list--option-highlighted { background: $surface; }
#composer { height: 1; border: none; padding: 0 1; background: $panel; }
#composer:focus { border: none; background: $surface; }
#cmdbar { height: 1; display: none; background: $panel; }
#cmdbar.on { display: block; }
#cmdprefix { width: 2; padding: 0 0 0 1; color: $primary; text-style: bold; }
#cmdline { height: 1; border: none; padding: 0; background: $panel; width: 1fr; }
#cmdline:focus { border: none; }
#status { height: 1; background: $panel; }
"""


class MessageList(OptionList):
    pass


class Main(Screen):
    def compose(self) -> ComposeResult:
        yield Static('', id='tabs')
        with Horizontal(id='main'):
            tree = Tree('bus', id='tree')
            tree.show_root = False
            tree.guide_depth = 2
            yield tree
            with Vertical():
                yield MessageList(id='list')
        yield Input(placeholder='', id='composer')
        with Horizontal(id='cmdbar'):
            yield Label(':', id='cmdprefix')
            yield Input(id='cmdline')
        yield Static('', id='status')

    def on_key(self, event):
        if self.app.handle_key(event):
            event.stop()
            event.prevent_default()


class BusTui(App):
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    TITLE = 'bus'

    def __init__(self, root, role='user', theme=None):
        super().__init__()
        self.model = Model(root, role)
        self.me = role
        for t in themes.all_themes():
            self.register_theme(t)
        self.theme_key = themes.resolve(theme) or themes.DEFAULT
        self.mode = 'normal'
        self.view = 'all'
        self.open_threads = []
        self.filters = {}
        self.search = ''
        self.tree_override = None
        self.pending = ''
        self.cmd_kind = ':'
        self.row_ids = []
        self.toast_log = []
        self._tree_sig = None
        self._seen_toast = set(self.model.msgs)
        self.main = Main()

    # ---------------------------------------------------------------- setup
    def on_mount(self):
        self.theme = themes.theme_name(self.theme_key)
        self.push_screen(self.main)
        self.call_after_refresh(self._first_paint)
        self.set_interval(POLL_S, self.poll_bus)
        if not self.model.registered:
            self.toast(f'role {self.me!r} is not registered on this bus: mentions will not reach it '
                       f'(register it with the bus CLI)', title='bus', severity='warning')

    def _first_paint(self):
        self.apply_layout()
        self.refresh_all(keep_last=True)
        self.set_mode('normal')

    def q(self, sel):
        return self.main.query_one(sel)

    @property
    def pal(self):
        return themes.PALETTES[self.theme_key]

    def on_resize(self, event):
        self.apply_layout()
        self.render_tabs()
        self.render_status()

    def apply_layout(self):
        try:
            tree = self.q('#tree')
        except Exception:
            return
        w = self.size.width
        show = self.tree_override if self.tree_override is not None else w >= NARROW
        tree.display = bool(show)
        tree.styles.width = 24 if w < 120 else 30
        if not show and self.focused is tree:
            self.q('#list').focus()

    # ---------------------------------------------------------------- live updates
    def poll_bus(self):
        changed, new = self.model.poll()
        for mid in new:
            if mid in self._seen_toast:
                continue
            self._seen_toast.add(mid)
            m = self.model.msgs.get(mid)
            why = m and self.model.notice(m)
            if why == 'mention':
                self.toast(m['text'][:160], title=f'@{m["from"]} mentioned you')
            elif why == 'reply':
                self.toast(m['text'][:160], title=f'{m["from"]} replied to you')
        if changed:
            self.refresh_all()
        else:
            self.render_status()
        return changed

    def toast(self, text, title='', severity='information'):
        self.toast_log.append((title, text))
        self.notify(text, title=title, severity=severity, timeout=6, markup=False)

    def refresh_all(self, keep_last=False):
        self.render_list(keep_last=keep_last)
        self.render_tree()
        self.render_tabs()
        self.render_status()

    # ---------------------------------------------------------------- tabs (bufferline)
    def tabs(self):
        out = [('all', 'All'), ('mine', '@me')]
        out += [(f'#{c}', f'#{c}') for c in self.model.channels()]
        for t in self.open_threads:
            m = self.model.msgs.get(t)
            out.append((f'thread:{t}', '≡ ' + (m['text'][:18] if m else t[-6:])))
        if self.view not in [k for k, _ in out]:
            out.append((self.view, self.view))
        return out

    def render_tabs(self):
        try:
            w = self.q('#tabs')
        except Exception:
            return
        p, tabs = self.pal, self.tabs()
        width = max(20, self.size.width)
        unread_by = {}
        for m in self.model.msgs.values():
            if self.model.unread_me(m):
                unread_by['all'] = unread_by.get('all', 0) + 1
                unread_by['mine'] = unread_by.get('mine', 0) + 1
                c = self.model.chan(m)
                if c != 'all':
                    unread_by[f'#{c}'] = unread_by.get(f'#{c}', 0) + 1
        cells = []
        for k, label in tabs:
            n = unread_by.get(k)
            cells.append((k, f' {label}{f" {n}" if n else ""} '))
        act = next((i for i, (k, _) in enumerate(cells) if k == self.view), 0)
        lo = 0  # slide the window so the active tab is visible
        while sum(len(c[1]) + 1 for c in cells[lo:act + 1]) > width - 4 and lo < act:
            lo += 1
        t = Text(no_wrap=True, overflow='crop')
        if lo:
            t.append('‹ ', style=p['muted'])
        for i, (k, label) in enumerate(cells[lo:], lo):
            if i == act:
                t.append(label, style=f'bold {p["on_accent"]} on {p["accent"]}')
            else:
                t.append(label, style=p['muted'] if not k.startswith('thread:') else p['accent2'])
            t.append('│' if i < len(cells) - 1 else '', style=p['faint'])
        w.update(t)

    def set_view(self, view):
        self.view = view
        if view.startswith('thread:') and view[7:] not in self.open_threads:
            self.open_threads.append(view[7:])
        self.render_list(keep_last=True)
        self.render_tabs()
        self.render_status()

    def cycle_tab(self, d):
        keys = [k for k, _ in self.tabs()]
        i = keys.index(self.view) if self.view in keys else 0
        self.set_view(keys[(i + d) % len(keys)])

    def open_thread(self, mid=None):
        mid = mid or self.selected_id()
        if not mid or mid not in self.model.msgs:
            return
        root = self.model.root_of(self.model.msgs[mid])
        self.set_view(f'thread:{root}')

    def close_thread(self):
        if self.view.startswith('thread:'):
            t = self.view[7:]
            if t in self.open_threads:
                self.open_threads.remove(t)
            self.set_view('all')

    # ---------------------------------------------------------------- message list
    def row_text(self, m, depth):
        p, me = self.pal, self.me
        t = Text(no_wrap=False)
        unread = self.model.unread_me(m)
        t.append('●' if unread else ' ', style=p['accent'])
        t.append('  ↳ ' if depth else ' ', style=p['faint'])
        t.append(m['time'][11:16] + ' ', style=p['muted'])
        if m.get('prio') == 'urgent':
            t.append('!! ', style=f'bold {p["red"]}')
        frm_style = f'bold {p["accent"]}' if m['from'] == me else f'bold {p["accent2"]}'
        t.append(m['from'], style=frm_style)
        tos = [r['to'] for r in m['rc'] if r['why'] in ('to', 'cc')]
        if tos:
            t.append(' → ' + ','.join(tos[:3]) + ('…' if len(tos) > 3 else ''), style=p['cyan'])
        if m.get('topic') and not self.view.startswith('#'):
            t.append(f' #{m["topic"]}', style=p['yellow'])
        text = '(deleted)' if m.get('deleted') else m['text']
        t.append('  ')
        t.append(text, style=p['muted'] if m.get('prio') == 'fyi' or m.get('deleted') else p['fg'])
        if self.search:
            for w in self.search.split():
                t.highlight_words([w], style=f'{p["on_accent"]} on {p["yellow"]}', case_sensitive=False)
        if m.get('edited'):
            t.append(' (edited)', style=p['muted'])
        if m.get('body'):
            t.append(' ▸body', style=p['accent'])
        if m.get('pin'):
            t.append(' 📌')
        rx = m.get('rx') or {}
        for e, who in rx.items():
            if who:
                t.append(f' {e}{len(who)}', style=f'bold {p["green"]}' if me in who else '')
        rc = [r for r in m['rc'] if r['r'] != 'posted']
        if rc and m['from'] == me:
            counts = {}
            for r in rc:
                counts[r['r']] = counts.get(r['r'], 0) + 1
            col = {'seen': p['green'], 'delivered': p['cyan'], 'queued': p['muted'], 'failed': p['red'],
                   'expired': p['red']}
            for k, n in counts.items():
                t.append(f' {chat.TICKS.get(k, "?")}{n if len(rc) > 1 else ""}', style=col.get(k, ''))
        return t

    def render_list(self, keep_last=False):
        try:
            ol = self.q('#list')
        except Exception:
            return
        prev = self.selected_id()
        at_end = ol.highlighted is None or ol.highlighted >= ol.option_count - 1
        rows = self.model.rows(self.view, self.filters)
        self.row_ids = [m['id'] for m, _ in rows]
        ol.clear_options()
        ol.add_options([Option(self.row_text(m, d), id=m['id']) for m, d in rows])
        if not rows:
            return
        if (keep_last or at_end) or prev not in self.row_ids:
            idx = len(rows) - 1 if (keep_last or at_end or prev is None) else 0
        else:
            idx = self.row_ids.index(prev)
        ol.highlighted = idx

    def selected_id(self):
        try:
            ol = self.q('#list')
        except Exception:
            return None
        i = ol.highlighted
        return self.row_ids[i] if i is not None and 0 <= i < len(self.row_ids) else None

    def selected(self):
        mid = self.selected_id()
        return self.model.msgs.get(mid) if mid else None

    def select_id(self, mid):
        if mid not in self.row_ids:
            self.filters, self.view = {}, 'all'
            self.render_list()
            self.render_tabs()
        if mid in self.row_ids:
            self.q('#list').highlighted = self.row_ids.index(mid)

    def move(self, d):
        ol = self.q('#list')
        if not ol.option_count:
            return
        cur = ol.highlighted if ol.highlighted is not None else 0
        ol.highlighted = max(0, min(ol.option_count - 1, cur + d))

    def on_option_list_option_highlighted(self, event):
        if event.option_list.id != 'list':
            return
        mid = event.option.id
        m = self.model.msgs.get(mid)
        if m and self.model.unread_me(m):
            self.model.mark_seen([mid])
        self.render_status()

    def on_option_list_option_selected(self, event):
        if event.option_list.id == 'list':
            self.show_detail()

    # ---------------------------------------------------------------- tree (neo-tree-like)
    def render_tree(self):
        try:
            tree = self.q('#tree')
        except Exception:
            return
        md = self.model
        roots = md.roots()
        threads = [(r, len(md.replies(r['id']))) for r in roots]
        threads = [x for x in threads if x[1]][-30:]
        pins = [m for m in md.msgs.values() if m.get('pin')]
        health = md.health()
        watch_live = {n: md.watch_live(n) for n in md.roles()}
        unread = {n: md.unread_count(n) for n in md.roles()}
        sig = (tuple(md.channels()), tuple(sorted(unread.items())), tuple(sorted(watch_live.items())),
               tuple((r['id'], n) for r, n in threads), tuple(sorted(m['id'] for m in pins)), self.theme_key,
               health['watches'])
        if sig == self._tree_sig:
            return
        self._tree_sig = sig
        p = self.pal
        tree.clear()
        ch = tree.root.add(Text('Channels', style=f'bold {p["accent"]}'), expand=True)
        ch.add_leaf(Text('All'), data=('view', 'all'))
        ch.add_leaf(Text('@me'), data=('view', 'mine'))
        for c in md.channels():
            ch.add_leaf(Text(f'#{c}', style=p['yellow']), data=('view', f'#{c}'))
        rl = tree.root.add(Text('Roles', style=f'bold {p["accent"]}'), expand=True)
        for n in md.roles():
            label = Text()
            label.append('● ' if watch_live.get(n) else '○ ', style=p['green'] if watch_live.get(n) else p['faint'])
            label.append(n, style=f'bold {p["accent"]}' if n == self.me else '')
            if unread.get(n):
                label.append(f' {unread[n]}', style=p['orange'])
            rl.add_leaf(label, data=('role', n))
        th = tree.root.add(Text('Threads', style=f'bold {p["accent"]}'), expand=True)
        for r, n in reversed(threads):
            th.add_leaf(Text(f'{r["text"][:22]} ({n})'), data=('thread', r['id']))
        if pins:
            pn = tree.root.add(Text('Pinned', style=f'bold {p["accent"]}'), expand=True)
            for m in pins:
                pn.add_leaf(Text(f'📌 {m["from"]}: {m["text"][:18]}'), data=('msg', m['id']))

    def on_tree_node_selected(self, event):
        data = event.node.data
        if data:
            self.pick_result(data)

    # ---------------------------------------------------------------- statusline (lualine-like)
    def render_status(self):
        try:
            st = self.q('#status')
        except Exception:
            return
        p = self.pal
        mode_col = {'normal': p['accent'], 'insert': p['green'], 'command': p['orange'], 'search': p['accent2']}
        h = self.model.health()
        unread = self.model.unread_count()
        t = Text(no_wrap=True, overflow='crop')
        t.append(f' {self.mode.upper()} ', style=f'bold {p["on_accent"]} on {mode_col[self.mode]}')
        t.append(f' {self.me} ', style=f'bold {p["fg"]} on {p["surface"]}')
        view = dict(self.tabs()).get(self.view, self.view)
        t.append(f' {view} ', style=p['fg'])
        fl = self.filter_label()
        if fl:
            t.append(f'[{fl}] ', style=p['yellow'])
        if self.search:
            t.append(f'/{self.search} ', style=p['accent2'])
        right = Text(no_wrap=True)
        right.append(f' ✉ {unread} ', style=f'bold {p["on_accent"]} on {p["orange"]}' if unread else p['muted'])

        def hcol(age, warn, bad):
            if age is None:
                return p['muted']
            return p['green'] if age < warn else p['yellow'] if age < bad else p['red']
        right.append(' deliver ', style=p['muted'])
        right.append(age_text(h['deliver']), style=hcol(h['deliver'], 30, 120))
        right.append(' watch ', style=p['muted'])
        right.append(f'{h["watches"]}{"*" if h["my_watch"] else ""}',
                     style=p['green'] if h['watches'] else p['muted'])
        right.append(' log ', style=p['muted'])
        right.append(age_text(h['log']), style=p['fg'])
        ol_i = None
        try:
            ol_i = self.q('#list').highlighted
        except Exception:
            pass
        right.append(f' {(ol_i or 0) + 1 if self.row_ids else 0}/{len(self.row_ids)} ',
                     style=f'{p["on_accent"]} on {mode_col[self.mode]}')
        width = self.size.width
        gap = width - len(t) - len(right)
        if gap > 0:
            t.append(' ' * gap)
            t.append_text(right)
        else:  # narrow: keep the left side, then as much of the right as fits
            t.append(' ')
            t.append_text(right)
        st.update(t)

    def filter_label(self):
        f, out = self.filters, []
        if f.get('chan'):
            out.append('#' + f['chan'])
        if f.get('role'):
            out.append('@' + f['role'])
        if f.get('mentions'):
            out.append('to me')
        if f.get('unread'):
            out.append('unread')
        if f.get('q'):
            out.append(f'"{f["q"]}"')
        return ' '.join(out)

    def set_filter(self, **kw):
        for k, v in kw.items():
            if v in (None, False, ''):
                self.filters.pop(k, None)
            else:
                self.filters[k] = v
        self.render_list(keep_last=True)
        self.render_status()

    # ---------------------------------------------------------------- modes
    def set_mode(self, mode):
        self.mode = mode
        comp = self.q('#composer')
        bar = self.q('#cmdbar')
        if mode == 'insert':
            bar.remove_class('on')
            comp.display = True
            comp.placeholder = f'write to {self.context_label()} · enter send · esc normal'
            comp.focus()
        elif mode in ('command', 'search'):
            comp.display = False
            bar.add_class('on')
            self.q('#cmdprefix').update(':' if mode == 'command' else '/')
            cl = self.q('#cmdline')
            cl.value = ''
            cl.focus()
        else:
            bar.remove_class('on')
            comp.display = True
            comp.placeholder = f'i: write to {self.context_label()}   c: compose   space: leader   ?: help'
            self.q('#list').focus()
        self.render_status()

    def context_label(self):
        if self.view.startswith('thread:'):
            return 'this thread (reply)'
        if self.view.startswith('#'):
            return self.view
        return '#all'

    # ---------------------------------------------------------------- key dispatch
    def handle_key(self, event):
        """Main-screen keys; True when handled here."""
        k = event.key
        ch = event.character if event.character and event.character.isprintable() else None
        if self.mode == 'insert':
            if k == 'escape':
                self.set_mode('normal')
                return True
            return False
        if self.mode in ('command', 'search'):
            if k == 'escape':
                self.set_mode('normal')
                return True
            return False
        focused = self.focused
        in_tree = focused is not None and focused.id == 'tree'
        key = ch or k
        if self.pending == 'g':
            self.pending = ''
            if key == 'g':
                self.q('#list').highlighted = 0 if self.row_ids else None
            elif key == 't':
                self.cycle_tab(1)
            elif key == 'T':
                self.cycle_tab(-1)
            return True
        if in_tree and key in ('j', 'k'):
            tree = self.q('#tree')
            (tree.action_cursor_down if key == 'j' else tree.action_cursor_up)()
            return True
        if in_tree and k in ('enter', 'up', 'down', 'left', 'right'):
            return False
        half = max(1, self.q('#list').size.height // 2)
        acts = {
            'j': lambda: self.move(1), 'down': lambda: self.move(1),
            'k': lambda: self.move(-1), 'up': lambda: self.move(-1),
            'G': lambda: self.move(10 ** 9), 'end': lambda: self.move(10 ** 9), 'home': lambda: self.move(-10 ** 9),
            'ctrl+d': lambda: self.move(half), 'ctrl+u': lambda: self.move(-half),
            'ctrl+f': lambda: self.move(half * 2), 'ctrl+b': lambda: self.move(-half * 2),
            'enter': self.show_detail, 'o': self.show_detail,
            'i': lambda: self.set_mode('insert'), 'a': lambda: self.set_mode('insert'),
            'c': self.compose_new, 'r': self.compose_reply,
            'e': self.react_modal, '+': self.react_modal,
            'p': self.toggle_pin, 's': self.toggle_sub,
            't': self.open_thread, 'x': self.close_thread,
            'H': lambda: self.cycle_tab(-1), 'L': lambda: self.cycle_tab(1),
            'y': self.yank, '/': lambda: self.set_mode('search'), ':': lambda: self.set_mode('command'),
            'n': lambda: self.search_next(1), 'N': lambda: self.search_next(-1),
            'escape': self.clear_search_or_filters, '?': self.show_help,
            'space': self.leader, ' ': self.leader, 'tab': self.toggle_focus,
        }
        if key == 'g':
            self.pending = 'g'
            return True
        fn = acts.get(key) or acts.get(k)
        if fn is None:
            return False
        fn()
        return True

    def toggle_focus(self):
        tree, lst = self.q('#tree'), self.q('#list')
        if self.focused is lst and tree.display:
            tree.focus()
        else:
            lst.focus()

    # ---------------------------------------------------------------- leader
    def leader(self):
        self.push_screen(WhichKey(LEADER), self.on_leader)

    def on_leader(self, key):
        if not key:
            return
        {
            'f': lambda: self.open_picker('all'), 'r': lambda: self.open_picker('role'),
            't': lambda: self.open_picker('thread'), 'b': lambda: self.open_picker('chan'),
            'p': lambda: self.open_picker('pin'), 'e': self.toggle_tree, 'n': self.compose_new,
            'u': lambda: self.set_filter(unread=not self.filters.get('unread')),
            '@': lambda: self.set_filter(mentions=not self.filters.get('mentions')),
            'm': self.mark_view_read, 'T': lambda: self.open_picker('theme'), 'h': self.show_help,
            'q': self.exit,
        }.get(key, lambda: None)()

    def toggle_tree(self):
        tree = self.q('#tree')
        self.tree_override = not tree.display
        self.apply_layout()
        if tree.display:
            self._tree_sig = None
            self.render_tree()

    def mark_view_read(self):
        n = self.model.mark_seen(self.row_ids)
        self.poll_bus()
        self.toast(f'marked {n} read', title='bus')

    # ---------------------------------------------------------------- picker
    def picker_items(self, kind):
        md, items = self.model, []
        if kind in ('all', 'chan'):
            items += [('chan', 'all', 'All', 'all channel'), ('chan', 'mine', '@me', 'me mine mentions')]
            items += [('chan', f'#{c}', f'#{c}', f'#{c} channel') for c in md.channels()]
        if kind in ('all', 'role'):
            for n in md.roles():
                e = md.index.reg.get(n, {})
                u = md.unread_count(n)
                items.append(('role', n, f'{n}  {e.get("kind") or ""} {e.get("mode") or ""}'
                              + (f'  {u} unread' if u else ''), f'{n} {e.get("note") or ""}'))
        if kind in ('all', 'thread'):
            for r in reversed(md.roots()):
                n = len(md.replies(r['id']))
                if kind == 'thread' or n:
                    items.append(('thread', r['id'], f'{r["text"][:70]}  ({n} repl{"y" if n == 1 else "ies"}, '
                                  f'{r["from"]}, {r["last"][11:16]})', f'{r["text"]} {r["from"]} {r.get("topic") or ""}'))
        if kind in ('all',):
            for m in sorted(md.msgs.values(), key=lambda m: m['time'], reverse=True):
                items.append(('msg', m['id'], f'{m["time"][11:16]} {m["from"]}: {m["text"][:80]}',
                              f'{m["from"]} {m["text"]} {" ".join(r["to"] for r in m["rc"])} {m.get("topic") or ""}'))
        if kind == 'pin':
            for m in md.msgs.values():
                if m.get('pin'):
                    items.append(('msg', m['id'], f'{m["from"]}: {m["text"][:80]}', f'{m["from"]} {m["text"]}'))
        if kind == 'theme':
            items = [('theme', n, n + ('  (current)' if n == self.theme_key else ''), n) for n in themes.PALETTES]
        return items

    def open_picker(self, kind):
        titles = {'all': 'find', 'role': 'roles', 'thread': 'threads', 'chan': 'channels', 'pin': 'pinned',
                  'theme': 'theme'}
        self.push_screen(Picker(titles[kind], self.picker_items(kind)), self.pick_result)

    def pick_result(self, res):
        if not res:
            return
        kind, v = res
        if kind in ('chan', 'view'):
            self.set_view(v)
        elif kind == 'role':
            self.set_filter(role=None if self.filters.get('role') == v else v)
        elif kind == 'thread':
            self.open_thread(v)
        elif kind == 'msg':
            self.select_id(v)
        elif kind == 'theme':
            self.set_theme(v)

    # ---------------------------------------------------------------- actions on the selection
    def guard(self, fn, *a, **kw):
        try:
            res = fn(*a, **kw)
        except BusError as ex:
            self.toast(str(ex), title='bus error', severity='error')
            return None
        self.poll_bus()
        return res

    def show_detail(self):
        m = self.selected()
        if not m:
            return
        p = self.pal
        t = Text()
        t.append(f'{m["id"]}  {m["time"]}\n', style=p['muted'])
        t.append(m['from'], style=f'bold {p["accent2"]}')
        tos = [r['to'] for r in m['rc'] if r['why'] in ('to', 'cc')]
        if tos:
            t.append(' → ' + ', '.join(tos), style=p['cyan'])
        meta = [f'prio {m.get("prio")}']
        if m.get('topic'):
            meta.append(f'#{m["topic"]}')
        if m.get('re'):
            meta.append(f're {m["re"]}')
        if m.get('thread'):
            meta.append(f'thread {m["thread"]}')
        if m.get('pin'):
            meta.append('📌 pinned')
        t.append('   ' + ' · '.join(meta) + '\n\n', style=p['muted'])
        t.append(('(deleted)' if m.get('deleted') else m['text']) + ('  (edited)' if m.get('edited') else '') + '\n')
        body = self.model.body(m['id'])
        if body:
            t.append('\n── body ──\n', style=p['muted'])
            t.append(body.rstrip() + '\n')
        t.append('\n── receipts ──\n', style=p['muted'])
        col = {'seen': p['green'], 'delivered': p['cyan'], 'queued': p['muted'], 'failed': p['red'],
               'expired': p['red'], 'posted': p['muted']}
        for r in self.model.receipts(m['id']):
            t.append(f'  {r["tick"]:<2} {r["receipt"]:<9} ', style=col.get(r['receipt'], ''))
            t.append(f'{r["to"]:<16} {r["rcpt"]:<8} {r["since"][11:19]}')
            if r['receipt'] in ('failed', 'expired', 'queued') and r['detail']:
                t.append(f'  {r["detail"][:70]}', style=p['muted'])
            t.append('\n')
        rx = {e: w for e, w in (m.get('rx') or {}).items() if w}
        if rx:
            t.append('\n── reactions ──\n', style=p['muted'])
            for e, who in rx.items():
                t.append(f'  {e} {", ".join(who)}\n')
        root = self.model.msgs.get(self.model.root_of(m), {})
        if root.get('subs'):
            t.append(f'\nfollowing the thread: {", ".join(root["subs"])}\n', style=p['muted'])
        self.push_screen(Detail(t, f'message from {m["from"]}'), self.on_detail)

    def on_detail(self, action):
        if action == 'reply':
            self.compose_reply()
        elif action == 'react':
            self.react_modal()
        elif action == 'pin':
            self.toggle_pin()
        elif action == 'thread':
            self.open_thread()
        elif action == 'sub':
            self.toggle_sub()
        elif action == 'yank':
            self.yank()

    def compose_submit(self, v):
        to = v['to'].strip()
        if not to and not v.get('re'):
            to = self.view if self.view.startswith('#') else '#all'
        try:
            self.model.send(to, v['text'], re=v.get('re'), prio=v.get('prio') or 'normal',
                            topic=(v.get('topic') or '').strip() or None)
        except BusError as ex:
            return str(ex)
        self.poll_bus()
        return None

    def compose_new(self):
        topic = self.view[1:] if self.view.startswith('#') else ''
        self.push_screen(Compose(self.compose_submit, title='compose', topic=topic))

    def compose_reply(self):
        m = self.selected()
        if not m:
            return
        self.push_screen(Compose(self.compose_submit, title=f'reply to {m["from"]}', re=m['id'],
                                 topic=m.get('topic') or '', quote=f'{m["from"]}: {m["text"][:90]}'))

    def react_modal(self):
        m = self.selected()
        if not m:
            return
        mine = [e for e, w in (m.get('rx') or {}).items() if self.me in w]
        mid = m['id']
        self.push_screen(React(mine), lambda e: e and self.guard(self.model.react, mid, e))

    def toggle_pin(self):
        m = self.selected()
        if m:
            self.guard(self.model.pin, m['id'])

    def toggle_sub(self):
        m = self.selected()
        if m:
            ev = self.guard(self.model.subscribe, m['id'])
            if ev:
                self.toast(('following' if ev.get('on') else 'unfollowed') + ' the thread', title='bus')

    def yank(self):
        mid = self.selected_id()
        if mid:
            self.copy_to_clipboard(mid)
            self.toast(f'copied {mid}', title='bus')

    def show_help(self):
        self.push_screen(Detail(Text(HELP), 'keymap'), self.on_detail)

    # ---------------------------------------------------------------- search
    def search_next(self, d, start=None):
        if not self.search or not self.row_ids:
            return False
        ws = self.search.lower().split()
        ol = self.q('#list')
        cur = ol.highlighted if start is None else start
        cur = 0 if cur is None else cur
        n = len(self.row_ids)
        for step in range(1 if start is None else 0, n + 1):
            i = (cur + d * step) % n
            m = self.model.msgs[self.row_ids[i]]
            hay = f'{m["from"]} {m["text"]} {m.get("topic") or ""}'.lower()
            if all(w in hay for w in ws):
                ol.highlighted = i
                return True
        self.toast(f'pattern not found: {self.search}', title='search', severity='warning')
        return False

    def clear_search_or_filters(self):
        if self.search:
            self.search = ''
            self.render_list()
        elif self.filters:
            self.filters = {}
            self.render_list(keep_last=True)
        self.render_status()

    # ---------------------------------------------------------------- inputs
    def on_input_submitted(self, event):
        iid = event.input.id
        if iid == 'composer':
            text = event.value.strip()
            if not text:
                return
            m = self.selected()
            if self.view.startswith('thread:'):
                re_id = m['id'] if m else self.view[7:]
                err = self.compose_submit({'to': '', 'text': text, 're': re_id})
            else:
                err = self.compose_submit({'to': self.view if self.view.startswith('#') else '#all', 'text': text})
            if err:
                self.toast(err, title='not sent', severity='error')
            else:
                event.input.value = ''
                self.render_list(keep_last=True)
        elif iid == 'cmdline':
            kind, val = self.mode, event.value
            self.set_mode('normal')
            if kind == 'search':
                self.search = val.strip()
                self.render_list()
                if self.search:
                    self.search_next(1, start=self.q('#list').highlighted or 0)
            else:
                self.run_command(val)

    def run_command(self, line):
        line = line.strip()
        if not line:
            return
        cmd, _, arg = line.partition(' ')
        arg = arg.strip()
        m = self.selected()
        need = ('reply', 'react', 'pin', 'unpin', 'sub', 'unsub', 'edit', 'delete', 'detail', 'open')
        if cmd in need and not m:
            self.toast('no message selected', title=f':{cmd}', severity='warning')
            return
        if cmd in ('q', 'q!', 'quit', 'qa', 'qa!', 'wq'):
            self.exit()
        elif cmd in ('reply', 're'):
            if arg:
                err = self.compose_submit({'to': '', 'text': arg, 're': m['id']})
                if err:
                    self.toast(err, title=':reply', severity='error')
            else:
                self.compose_reply()
        elif cmd == 'react':
            if not arg:
                return self.react_modal()
            emoji, _, flag = arg.partition(' ')
            on = None if not flag else flag.strip() not in ('off', '-', 'remove')
            self.guard(self.model.react, m['id'], emoji, on=on)
        elif cmd in ('send', 'msg'):
            to, _, text = arg.partition(' ')
            if not to or not text.strip():
                self.toast('usage: :send <to[,to2]|#chan> <text>', title=':send', severity='warning')
                return
            err = self.compose_submit({'to': to, 'text': text})
            if err:
                self.toast(err, title=':send', severity='error')
        elif cmd in ('thread', 'th'):
            self.open_thread(arg or None)
        elif cmd in ('pin', 'unpin'):
            self.guard(self.model.pin, m['id'], on=cmd == 'pin')
        elif cmd in ('sub', 'unsub', 'follow', 'unfollow'):
            self.guard(self.model.subscribe, m['id'], on=cmd in ('sub', 'follow'))
        elif cmd == 'edit':
            self.guard(self.model.edit, m['id'], arg)
        elif cmd in ('delete', 'del'):
            self.guard(self.model.delete, m['id'])
        elif cmd in ('detail', 'open'):
            self.show_detail()
        elif cmd == 'filter':
            self.command_filter(arg)
        elif cmd == 'theme':
            if not arg:
                return self.open_picker('theme')
            self.set_theme(arg)
        elif cmd == 'tree':
            self.toggle_tree()
        elif cmd == 'seen':
            self.mark_view_read()
        elif cmd in ('tabclose', 'bd', 'close'):
            self.close_thread()
        elif cmd in ('compose', 'new'):
            self.compose_new()
        elif cmd in ('help', 'h'):
            self.show_help()
        else:
            self.toast(f'not a command: {cmd}', title='command', severity='error')

    def command_filter(self, arg):
        if not arg or arg in ('clear', 'none', 'off'):
            self.filters = {}
            self.render_list(keep_last=True)
            self.render_status()
            return
        f, words = {}, []
        for w in arg.split():
            if w in ('mentions', '@me', 'me'):
                f['mentions'] = True
            elif w == 'unread':
                f['unread'] = True
            elif w.startswith('#') and len(w) > 1:
                f['chan'] = w[1:]
            elif w.startswith('@') and len(w) > 1:
                f['role'] = w[1:]
            elif w.startswith('role:'):
                f['role'] = w[5:]
            else:
                words.append(w)
        if words:
            f['q'] = ' '.join(words)
        self.filters = f
        self.render_list(keep_last=True)
        self.render_status()

    def set_theme(self, name):
        key = themes.resolve(name)
        if not key:
            self.toast(f'no theme {name!r}: {", ".join(themes.PALETTES)}', title='theme', severity='error')
            return
        self.theme_key = key
        self.theme = themes.theme_name(key)
        self._tree_sig = None
        self.refresh_all()
