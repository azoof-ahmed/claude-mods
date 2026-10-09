"""Floating modals: which-key, fuzzy picker, compose/reply, reactions, message detail."""
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from .model import PRIOS, REACTIONS, fuzzy_score

MODAL_CSS = """
ModalScreen { align: center middle; background: $background 40%; }
.box { background: $surface; border: round $primary; padding: 0 1; width: 80%; max-width: 100; height: auto;
       max-height: 90%; }
.box > .title { color: $primary; text-style: bold; width: 1fr; }
.hint { color: $text-muted; }
.err { color: $error; }
"""


class WhichKey(ModalScreen):
    """The leader popup: lists the leader bindings, the next key picks one (Esc cancels)."""
    DEFAULT_CSS = MODAL_CSS + """
    WhichKey { align: right bottom; background: $background 0%; }
    WhichKey .box { width: auto; min-width: 34; max-width: 60; margin: 0 1 2 0; }
    """

    def __init__(self, bindings, title='leader'):
        super().__init__()
        self.items = bindings

    def compose(self) -> ComposeResult:
        t = Text()
        for i, (key, desc) in enumerate(self.items):
            if i:
                t.append('\n')
            t.append(f' {key:>3} ', style='bold')
            t.append(' → ', style='dim')
            t.append(desc)
        with Vertical(classes='box', id='whichkey'):
            yield Label('␣ leader', classes='title')
            yield Static(t, id='wk-list')

    def on_key(self, event):
        event.stop()
        event.prevent_default()
        if event.key == 'escape':
            self.dismiss(None)
            return
        keys = {k for k, _ in self.items}
        ch = event.character if event.character and event.character.isprintable() else event.key
        self.dismiss(ch if ch in keys else None)


class Picker(ModalScreen):
    """Telescope-style fuzzy picker. items: [(kind, value, label, haystack)]; returns (kind, value) or None."""
    DEFAULT_CSS = MODAL_CSS + """
    Picker .box { height: 80%; }
    Picker #pick-list { height: 1fr; background: $surface; border: none; }
    Picker #pick-q { border: tall $primary; }
    """
    BINDINGS = [
        Binding('escape', 'cancel', show=False),
        Binding('down,ctrl+n,ctrl+j', 'move(1)', show=False, priority=True),
        Binding('up,ctrl+p,ctrl+k', 'move(-1)', show=False, priority=True),
    ]

    def __init__(self, title, items):
        super().__init__()
        self.title_text, self.items, self.shown = title, items, []

    def compose(self) -> ComposeResult:
        with Vertical(classes='box', id='picker'):
            yield Label(f' {self.title_text}', classes='title', id='pick-title')
            yield Input(placeholder='fuzzy search', id='pick-q')
            yield OptionList(id='pick-list')
            yield Label('enter pick · ↑↓ / ctrl-n ctrl-p move · esc close', classes='hint')

    def on_mount(self):
        self.refilter('')
        self.query_one('#pick-q', Input).focus()

    def refilter(self, q):
        scored = []
        for n, it in enumerate(self.items):
            s = fuzzy_score(q, it[3])
            if s is not None:
                scored.append((-s, n, it))
        scored.sort(key=lambda x: (x[0], x[1]) if q.strip() else (0, x[1]))
        self.shown = [it for _, _, it in scored[:500]]
        ol = self.query_one('#pick-list', OptionList)
        ol.clear_options()
        icons = {'thread': '≡', 'role': '◆', 'msg': '·', 'chan': '#', 'pin': '📌', 'theme': '◐'}
        ol.add_options([Option(Text.assemble((f'{icons.get(k, " ")} ', 'bold'), (f'{k:<7}', 'dim'), label))
                        for k, _, label, _ in self.shown])
        if self.shown:
            ol.highlighted = 0
        self.query_one('#pick-title', Label).update(f' {self.title_text}  {len(self.shown)}/{len(self.items)}')

    def on_input_changed(self, event):
        self.refilter(event.value)

    def on_input_submitted(self, event):
        self.pick()

    def on_option_list_option_selected(self, event):
        event.stop()
        self.pick(event.option_index)

    def action_move(self, d):
        ol = self.query_one('#pick-list', OptionList)
        if ol.option_count:
            ol.highlighted = max(0, min(ol.option_count - 1, (ol.highlighted or 0) + d))

    def pick(self, idx=None):
        ol = self.query_one('#pick-list', OptionList)
        idx = ol.highlighted if idx is None else idx
        if idx is None or idx >= len(self.shown):
            return
        k, v, _, _ = self.shown[idx]
        self.dismiss((k, v))

    def action_cancel(self):
        self.dismiss(None)


class Compose(ModalScreen):
    """Compose or reply. submit(dict) -> error text or None; the modal closes on success."""
    DEFAULT_CSS = MODAL_CSS + """
    Compose #c-text { height: 8; }
    Compose Input { margin: 0; }
    Compose .row { height: auto; }
    Compose #c-to { width: 2fr; } Compose #c-topic { width: 1fr; } Compose #c-prio { width: 16; }
    """
    BINDINGS = [Binding('ctrl+s', 'send', 'send', priority=True), Binding('escape', 'cancel', show=False)]

    def __init__(self, submit, title='compose', to='', topic='', re=None, quote=''):
        super().__init__()
        self.submit, self.title_text, self.to, self.topic, self.re, self.quote = submit, title, to, topic, re, quote

    def compose(self) -> ComposeResult:
        with Vertical(classes='box', id='compose'):
            yield Label(f' {self.title_text}', classes='title')
            if self.quote:
                yield Label(Text('↩ ' + self.quote, style='italic'), classes='hint', id='c-quote')
            with Horizontal(classes='row'):
                yield Input(self.to, placeholder='to: role[,role] or #channel (empty: #all)', id='c-to')
                yield Input(self.topic, placeholder='topic', id='c-topic')
                yield Select([(p, p) for p in PRIOS], value='normal', allow_blank=False, id='c-prio')
            yield TextArea(id='c-text', soft_wrap=True)
            yield Label('', classes='err', id='c-err')
            yield Label('ctrl-s send · tab next field · esc cancel · @role notifies', classes='hint')

    def on_mount(self):
        self.query_one('#c-text', TextArea).focus()

    def values(self):
        return {'to': self.query_one('#c-to', Input).value, 'topic': self.query_one('#c-topic', Input).value,
                'prio': self.query_one('#c-prio', Select).value, 'text': self.query_one('#c-text', TextArea).text,
                're': self.re}

    def action_send(self):
        err = self.submit(self.values())
        if err:
            self.query_one('#c-err', Label).update(err)
        else:
            self.dismiss(True)

    def action_cancel(self):
        self.dismiss(None)


class React(ModalScreen):
    """The reaction palette: 1-6 or j/k + enter toggles a reaction; returns the emoji or None."""
    DEFAULT_CSS = MODAL_CSS + """
    React .box { width: auto; min-width: 30; }
    """

    def __init__(self, mine=()):
        super().__init__()
        self.mine, self.pos = set(mine), 0

    def compose(self) -> ComposeResult:
        with Vertical(classes='box', id='react'):
            yield Label(' react', classes='title')
            yield Static(self.render_list(), id='rx-list')
            yield Label('1-6 · j/k enter · esc', classes='hint')

    def render_list(self):
        t = Text()
        for i, e in enumerate(REACTIONS):
            sel = i == self.pos
            t.append(('▸ ' if sel else '  ') + f'{i + 1} {e}' + ('  (yours: remove)' if e in self.mine else ''),
                     style='reverse' if sel else '')
            if i < len(REACTIONS) - 1:
                t.append('\n')
        return t

    def on_key(self, event):
        event.stop()
        event.prevent_default()
        k = event.key
        if k == 'escape':
            self.dismiss(None)
        elif k in ('j', 'down', 'k', 'up'):
            self.pos = (self.pos + (1 if k in ('j', 'down') else -1)) % len(REACTIONS)
            self.query_one('#rx-list', Static).update(self.render_list())
        elif k == 'enter':
            self.dismiss(REACTIONS[self.pos])
        elif k.isdigit() and 1 <= int(k) <= len(REACTIONS):
            self.dismiss(REACTIONS[int(k) - 1])


class Detail(ModalScreen):
    """Message detail: full text, body, per-recipient receipts, reactions. Keys return an action name."""
    DEFAULT_CSS = MODAL_CSS + """
    Detail .box { height: 85%; }
    Detail #d-scroll { height: 1fr; }
    """
    KEYS = {'r': 'reply', 'e': 'react', 'p': 'pin', 't': 'thread', 's': 'sub', 'y': 'yank'}

    def __init__(self, content, title):
        super().__init__()
        self.content, self.title_text = content, title

    def compose(self) -> ComposeResult:
        with Vertical(classes='box', id='detail'):
            yield Label(f' {self.title_text}', classes='title')
            with VerticalScroll(id='d-scroll'):
                yield Static(self.content, id='d-body')
            yield Label('r reply · e react · p pin · s follow · t thread · y copy id · esc',
                        classes='hint')

    def on_key(self, event):
        k = event.key
        sc = self.query_one('#d-scroll', VerticalScroll)
        if k in ('escape', 'q', 'enter'):
            self.dismiss(None)
        elif k in self.KEYS:
            self.dismiss(self.KEYS[k])
        elif k == 'j':
            sc.scroll_down(animate=False)
        elif k == 'k':
            sc.scroll_up(animate=False)
        else:
            return
        event.stop()
        event.prevent_default()
