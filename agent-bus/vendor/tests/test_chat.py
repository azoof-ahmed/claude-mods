import datetime
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orca_bus import chat  # noqa: E402
from orca_bus.cli import main  # noqa: E402
from orca_bus.store import Bus, BusError  # noqa: E402


def names(ms):
    ms = ms if isinstance(ms, list) else [ms]
    return {m['to']: m.get('rcpt', 'to') for m in ms}


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus(self.tmp.name)
        for n in ('a', 'b', 'c', 'd', 'coordinator', 'scheduler'):
            self.bus.register(n, f'h-{n}', 'claude', mode='file')
        self.bus.register('user', None, 'shell', mode='inbox')

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(list(argv), root=self.tmp.name)
        return rc, buf.getvalue()

    def test_plain_send_is_unchanged_without_route(self):
        m = self.bus.send('a', 'b', 'hi @c')
        self.assertEqual(m['to'], 'b')
        self.assertNotIn('rcpt', m)
        self.assertEqual(self.bus.inbox_file('c'), [])

    def test_mentions_notify_only_the_named(self):
        ms = self.bus.send('a', 'b', 'hi @c and @nobody, mail me@d.com', route=True)
        self.assertEqual(names(ms), {'b': 'to', 'c': 'mention'})
        self.assertEqual(self.bus.inbox_file('d'), [])

    def test_reply_reaches_the_author_and_inherits_the_thread(self):
        root = self.bus.send('a', 'b', 'question', route=True)
        r1 = self.bus.send('b', 'c', 'asking c', reply_to=root['id'], route=True)
        self.assertEqual(names(r1), {'c': 'to', 'a': 'reply'})
        self.assertTrue(all(m['thread'] == root['id'] for m in r1))
        r2 = self.bus.send('c', 'b', 'answer', reply_to=r1[0]['id'], route=True)
        self.assertEqual(r2['thread'], root['id'])  # a reply to a reply stays in the root's thread
        self.assertEqual([m['id'] for m in chat.thread_messages(self.bus, r2['id'])],
                         [root['id'], chat.base_id(r1[0]['id']), r2['id']])

    def test_subscribers_get_thread_replies_and_unsub_stops_them(self):
        root = self.bus.send('a', 'b', 'topic', route=True)
        chat.subscribe(self.bus, 'd', root['id'])
        self.assertIn('d', names(self.bus.send('b', 'a', 'reply', reply_to=root['id'], route=True)))
        chat.subscribe(self.bus, 'd', root['id'], on=False)
        self.assertNotIn('d', names(self.bus.send('b', 'a', 'again', reply_to=root['id'], route=True)))

    def test_code_quoted_and_body_at_all_do_not_ping(self):
        got = names(self.bus.send('a', 'b', 'use `@c` to ping c', body='note: @all is admin only; ask @d', route=True))
        self.assertEqual(got, {'b': 'to', 'd': 'mention'})

    def test_at_all_is_admin_only(self):
        with self.assertRaises(BusError):
            self.bus.send('a', 'b', 'hey @all', route=True)
        got = names(self.bus.send('coordinator', 'b', 'stop @all', route=True))
        self.assertEqual(set(got), {'a', 'b', 'c', 'd', 'scheduler', 'user'})

    def test_user_posts_always_reach_the_coordinator(self):
        self.assertEqual(names(self.bus.send('user', 'a', 'do x', route=True)), {'a': 'to', 'coordinator': 'watch'})

    def test_channel_post_notifies_nobody(self):
        m = self.bus.send('a', '#train', 'train 3 green', route=True)
        self.assertEqual((m['to'], m['state'], m['topic']), ('#train', 'posted', 'train'))
        for n in ('b', 'c', 'coordinator'):
            self.assertEqual(self.bus.inbox_file(n), [])
        m2 = self.bus.send('a', '#train', 'cc @b', route=True)
        self.assertEqual(names(m2), {'b': 'mention'})

    def test_receipts_queued_delivered_seen_failed(self):
        ms = self.bus.send('a', 'b,c', 'x', cc='zz', route=True)
        recs = chat.records(self.bus, ms[0]['id'])
        self.assertEqual([chat.receipt(self.bus, r)[0] for r in recs], ['queued', 'queued', 'failed'])
        self.bus.set_state(ms[0]['id'], 'inbox', 'file mode')
        self.bus.mark_seen('c', [ms[1]['id']])
        recs = chat.records(self.bus, ms[0]['id'])
        self.assertEqual([chat.receipt(self.bus, r)[0] for r in recs], ['delivered', 'seen', 'failed'])
        rc, out = self.cli('status', ms[0]['id'])
        self.assertIn('vv seen', out)
        self.assertIn('! ', out)

    def test_reactions_toggle_never_notify_and_show_in_digest(self):
        m = self.bus.send('a', 'b', 'done?', route=True)
        before = len(self.bus.inbox_file('a'))
        chat.react(self.bus, 'b', m['id'], '✅')
        chat.react(self.bus, 'c', m['id'], '\U0001F440')
        chat.react(self.bus, 'c', m['id'], '\U0001F440', on=False)
        self.assertEqual(len(self.bus.inbox_file('a')), before)
        self.assertEqual(chat.reactions(chat.chat_events(self.bus)), {m['id']: {'✅': ['b']}})
        rc, out = self.cli('digest', '--as', 'a', '--mark')
        self.assertIn('b ✅', out)
        rc, out = self.cli('digest', '--as', 'a')
        self.assertNotIn('reactions', out)

    def test_edit_and_delete_own_within_five_minutes(self):
        m = self.bus.send('a', 'b', 'typo', route=True)
        with self.assertRaises(BusError):
            chat.edit(self.bus, 'b', m['id'], 'not mine')
        chat.edit(self.bus, 'a', m['id'], 'fixed')
        late = lambda: datetime.datetime.now().astimezone() + datetime.timedelta(minutes=6)
        with self.assertRaises(BusError):
            chat.delete(self.bus, 'a', m['id'], clock=late)

    def test_cli_send_routes_and_old_form_still_works(self):
        rc, out = self.cli('send', 'b', 'ping @c', '--from', 'a')
        self.assertEqual(rc, 0)
        self.assertIn('-> c (mention)', out)
        rc, out = self.cli('send', 'b', 'plain', '--from', 'a', '--prio', 'fyi', '--topic', 't')
        self.assertIn('-> b [fyi]', out)


class WebIndexTests(unittest.TestCase):
    def setUp(self):
        from orca_bus import web
        self.web = web
        self.tmp = tempfile.TemporaryDirectory()
        self.app = web.App(self.tmp.name)
        for n in ('a', 'b', 'coordinator'):
            self.app.bus.register(n, f'h-{n}', 'claude', mode='file')
        self.app.ensure_user()

    def tearDown(self):
        self.tmp.cleanup()

    def test_tails_incrementally_and_waits_for_a_torn_line(self):
        idx = self.app.index
        m = self.app.bus.send('a', 'b', 'one', route=True)
        self.assertEqual(idx.poll(), {m['id']})
        self.assertEqual(idx.poll(), set())  # nothing new: nothing reread
        pos = idx.log.pos
        with open(self.app.bus.log_path, 'a', encoding='utf-8') as f:
            f.write('{"ev": "msg", "id": "torn"')
        self.assertEqual(idx.poll(), set())
        self.assertEqual(idx.log.pos, pos)
        with open(self.app.bus.log_path, 'a', encoding='utf-8') as f:
            f.write(', "from": "a", "to": "b", "time": "2026-10-10T00:00:00+05:00", "text": "x"}\n')
        self.assertEqual(idx.poll(), {'torn'})

    def test_user_send_react_seen_round_trip(self):
        r = self.app.post('/api/send', {'text': 'hi @a', 'to': [], 'chan': 'all'})
        self.assertEqual(sorted(r['to']), ['a', 'coordinator'])
        self.app.index.poll()
        self.app.bus.mark_seen('a', [r['id'] + '.1'])
        self.app.post('/api/react', {'id': r['id'], 'emoji': '\U0001F44D'})
        self.app.index.poll()
        v = self.app.index.views([r['id']])[0]
        self.assertEqual({x['to']: x['r'] for x in v['rc']}, {'a': 'seen', 'coordinator': 'queued'})
        self.assertEqual(v['rx'], {'\U0001F44D': ['user']})
        back = self.app.bus.send('a', 'user', 'answer', reply_to=r['id'] + '.1', route=True)
        self.app.index.poll()
        self.assertEqual(self.app.index.views([back['id']])[0]['thread'], r['id'])
        self.app.post('/api/seen', {'ids': [back['id']]})
        self.app.index.poll()
        self.assertEqual(self.app.index.unread('user'), 0)

    def test_non_ascii_and_multiline_go_to_the_body(self):
        r = self.app.post('/api/send', {'text': 'café ✓\nline two', 'to': ['a']})
        self.app.index.poll()
        self.assertIn('line two', self.app.index.body(r['id']))


if __name__ == '__main__':
    unittest.main()
