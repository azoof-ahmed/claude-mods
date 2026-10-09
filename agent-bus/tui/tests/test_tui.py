"""In-process render tests (Textual pilot). No screen capture: assertions read widgets and the exported SVG text."""
import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from orca_bus import chat
from orca_bus.store import Bus

from bus_tui.model import Model, fuzzy_score
from bus_tui.screens import Compose, Picker, WhichKey
from bus_tui.ui import BusTui


def make_bus():
    d = Path(tempfile.mkdtemp(prefix='bus-tui-test-'))
    bus = Bus(d)
    for n in ('alice', 'bob', 'carol'):
        bus.register(n, None, 'claude', mode='file')
    bus.register('user', None, 'shell', mode='inbox')
    bus.send('alice', 'bob', 'first message about the parser', route=True)
    bus.send('bob', ['#build'], 'build is green', route=True)
    root = bus.send('carol', 'user', 'hello user, please review', route=True)
    bus.send('alice', 'carol', 'a reply in the thread', reply_to=root['id'], route=True)
    return d, bus


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir, self.bus = make_bus()

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)  # IsolatedAsyncioTestCase enables slow-callback warnings

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def app(self, role='user', theme=None):
        return BusTui(str(self.dir), role, theme)

    async def test_list_renders_messages(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            ol = app.q('#list')
            self.assertEqual(ol.option_count, 4)
            texts = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
            self.assertTrue(any('first message about the parser' in t for t in texts))
            self.assertTrue(any('↳' in t and 'a reply in the thread' in t for t in texts))  # reply under its root
            svg = app.export_screenshot()
            self.assertIn('build', svg)
            self.assertIn('NORMAL', svg)

    async def test_jk_moves_selection(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            ol = app.q('#list')
            last = ol.option_count - 1
            self.assertEqual(ol.highlighted, last)  # starts at the newest, like a chat
            await pilot.press('k')
            self.assertEqual(ol.highlighted, last - 1)
            await pilot.press('k', 'k')
            self.assertEqual(ol.highlighted, last - 3)
            await pilot.press('j')
            self.assertEqual(ol.highlighted, last - 2)
            await pilot.press('g', 'g')
            self.assertEqual(ol.highlighted, 0)
            await pilot.press('G')
            self.assertEqual(ol.highlighted, last)

    async def test_command_react_adds_reaction(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            target = app.selected_id()
            await pilot.press(':')
            self.assertEqual(app.mode, 'command')
            app.q('#cmdline').value = 'react ✅'
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(app.mode, 'normal')
            rx = chat.reactions(chat.chat_events(self.bus))
            self.assertEqual(rx.get(target), {'✅': ['user']})
            self.assertEqual(app.model.msgs[target]['rx'], {'✅': ['user']})

    async def test_compose_modal_sends_to_inbox(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            self.assertIsInstance(app.screen, Compose)
            app.screen.query_one('#c-to').value = 'bob'
            app.screen.query_one('#c-text').text = 'composed from the tui'
            await pilot.press('ctrl+s')
            await pilot.pause()
            self.assertNotIsInstance(app.screen, Compose)
            inbox = [m['text'] for m in self.bus.inbox_file('bob')]
            self.assertIn('composed from the tui', inbox)
            self.assertTrue(any('composed from the tui' in m['text'] for m in app.model.msgs.values()))

    async def test_insert_mode_posts_to_channel(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press('i')
            self.assertEqual(app.mode, 'insert')
            await pilot.press(*'hi @alice')
            await pilot.press('enter')
            await pilot.pause()
            self.assertIn('hi @alice', [m['text'] for m in self.bus.inbox_file('alice')])
            await pilot.press('escape')
            self.assertEqual(app.mode, 'normal')

    async def test_space_shows_which_key(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press('space')
            await pilot.pause()
            self.assertIsInstance(app.screen, WhichKey)
            text = str(app.screen.query_one('#wk-list').render())
            self.assertIn('roles', text)
            self.assertIn('toggle tree', text)
            await pilot.press('escape')
            self.assertNotIsInstance(app.screen, WhichKey)

    async def test_picker_filters(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press('space', 'f')
            await pilot.pause()
            self.assertIsInstance(app.screen, Picker)
            pl = app.screen.query_one('#pick-list')
            before = pl.option_count
            await pilot.press(*'parser')
            await pilot.pause()
            after = pl.option_count
            self.assertGreater(before, after)
            self.assertGreaterEqual(after, 1)
            self.assertIn('parser', str(pl.get_option_at_index(0).prompt))
            await pilot.press('enter')
            await pilot.pause()
            self.assertNotIsInstance(app.screen, Picker)
            self.assertIn('parser', app.selected()['text'])

    async def test_roles_picker_sets_role_filter(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press('space', 'r')
            await pilot.pause()
            await pilot.press(*'bob', 'enter')
            await pilot.pause()
            self.assertEqual(app.filters.get('role'), 'bob')
            self.assertTrue(all(app.model.msgs[i]['from'] == 'bob' or
                                any(r['to'] == 'bob' for r in app.model.msgs[i]['rc'])
                                or app.model.root_of(app.model.msgs[i]) != i for i in app.row_ids))

    async def test_mention_toast(self):
        app = self.app()
        async with app.run_test(size=(140, 40), notifications=True) as pilot:
            await pilot.pause()
            self.assertEqual(app.toast_log, [])  # nothing toasted for the backlog
            self.bus.send('alice', ['#all'], 'ping @user can you look', route=True)
            await pilot.pause(1.5)  # the poll timer picks it up
            titles = [t for t, _ in app.toast_log]
            self.assertIn('@alice mentioned you', titles)
            self.assertTrue(any('ping @user' in m['text'] for m in app.model.msgs.values()))

    async def test_reply_toast_and_reply_command(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            mine = app.model.send('bob', 'question from the user')
            app.poll_bus()
            ans = self.bus.send('bob', 'user', 'answer', reply_to=mine['id'], route=True)
            app.poll_bus()
            self.assertIn('bob replied to you', [t for t, _ in app.toast_log])
            app.select_id(ans['id'])
            app.run_command('reply thanks')
            app.poll_bus()
            got = [m for m in self.bus.inbox_file('bob') if m['text'] == 'thanks']
            self.assertEqual(got[0]['reply_to'], ans['id'])
            self.assertEqual(got[0]['thread'], mine['id'])

    async def test_layout_narrow_and_wide(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertFalse(app.q('#tree').display)
            svg = app.export_screenshot()
            self.assertIn('NORMAL', svg)
            await pilot.press('space', 'e')
            await pilot.pause()
            self.assertTrue(app.q('#tree').display)
        app2 = self.app()
        async with app2.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            tree = app2.q('#tree')
            self.assertTrue(tree.display)
            labels = [str(n.label) for n in tree.root.children]
            self.assertEqual(labels[:3], ['Channels', 'Roles', 'Threads'])
            await pilot.press('space', 'e')
            await pilot.pause()
            self.assertFalse(tree.display)

    async def test_tree_keyboard_navigation(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press('tab')
            self.assertEqual(app.focused.id, 'tree')
            tree = app.q('#tree')
            tree.cursor_line = 0
            await pilot.press('j', 'j', 'j', 'enter')  # Channels > All > @me > #build
            await pilot.pause()
            self.assertEqual(app.view, '#build')
            await pilot.press('tab')
            self.assertEqual(app.focused.id, 'list')

    async def test_detail_shows_receipts(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press('g', 'g', 'enter')
            await pilot.pause()
            body = str(app.screen.query_one('#d-body').render())
            self.assertIn('receipts', body)
            self.assertIn('bob', body)
            self.assertIn('queued', body)

    async def test_tabs_thread_and_theme(self):
        app = self.app(theme='claude')
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.theme, 'bus-claude')
            await pilot.press('t')  # open the thread of the newest message (a reply)
            await pilot.pause()
            self.assertTrue(app.view.startswith('thread:'))
            self.assertEqual(app.q('#list').option_count, 2)
            await pilot.press('x')
            self.assertEqual(app.view, 'all')
            await pilot.press('L', 'L')
            self.assertEqual(app.view, '#build')
            app.run_command('theme catppuccin')
            self.assertEqual(app.theme, 'bus-catppuccin')
            app.run_command('filter unread')
            self.assertEqual(app.filters, {'unread': True})

    async def test_pin_and_search(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press('/')
            app.q('#cmdline').value = 'green'
            await pilot.press('enter')
            await pilot.pause()
            self.assertIn('green', app.selected()['text'])
            await pilot.press('p')
            await pilot.pause()
            self.assertTrue(app.model.msgs[app.selected_id()]['pin'])


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.dir, self.bus = make_bus()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fuzzy(self):
        self.assertIsNotNone(fuzzy_score('prs', 'parser'))
        self.assertIsNone(fuzzy_score('xyz', 'parser'))
        self.assertGreater(fuzzy_score('parser', 'the parser'), fuzzy_score('prs', 'the parser'))

    def test_health_reads_heartbeats(self):
        (self.dir / 'deliver.heartbeat').write_text(str(time.time()))
        (self.dir / 'inbox' / 'alice.watch.json').write_text(json.dumps({'pid': 1, 'beat': time.time()}))
        (self.dir / 'inbox' / 'bob.watch.json').write_text(json.dumps({'pid': 1, 'beat': time.time() - 500}))
        h = Model(self.dir, 'alice').health()
        self.assertLess(h['deliver'], 5)
        self.assertEqual(h['watches'], 1)
        self.assertTrue(h['my_watch'])
        self.assertIsNotNone(h['log'])

    def test_non_ascii_goes_to_body(self):
        md = Model(self.dir, 'user')
        md.send('bob', 'naïve ✅ text')
        m = self.bus.inbox_file('bob')[-1]
        self.assertTrue(m['text'].isascii())
        self.assertIn('body', m)


if __name__ == '__main__':
    unittest.main()
