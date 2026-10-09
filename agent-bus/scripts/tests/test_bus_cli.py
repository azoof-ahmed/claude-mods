"""Tests for scripts/bus.py: config, the hook, the watch heartbeat and the nudge suppression."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / 'vendor'))
import bus  # noqa: E402
from orca_bus.store import Bus  # noqa: E402

CLEAN = {k: '' for k in list(bus.ENV.values()) + ['AGENT_BUS_ROLE', 'ORCA_BUS_NAME', 'ORCA_TERMINAL_HANDLE']}


def run(argv, cwd, **env):
    out = io.StringIO()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        with mock.patch.dict(os.environ, {**CLEAN, **env}), redirect_stdout(out):
            code = bus.main(argv)
    finally:
        os.chdir(old)
    return code, out.getvalue()


class BusCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / '.git').mkdir()
        self.dir = self.root / '.claude' / 'bus'

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_layers(self):
        (self.root / '.claude').mkdir()
        (self.root / '.claude' / 'claude-mods.json').write_text(json.dumps({'agent-bus': {'dir': 'x/bus', 'chatPort': 9000}}))
        with mock.patch.dict(os.environ, {**CLEAN, 'AGENT_BUS_CHAT_PORT': '9100'}):
            cfg = bus.load_config({}, root=self.root)
            self.assertEqual(Path(cfg['dir']), self.root / 'x' / 'bus')
            self.assertEqual(cfg['chatPort'], 9000)  # the project file beats the env
            cfg = bus.load_config({'dir': 'flag/bus'}, root=self.root)
            self.assertEqual(Path(cfg['dir']), self.root / 'flag' / 'bus')

    def test_project_file_reads_busDir_and_dir_and_warns_on_unknown_keys(self):
        (self.root / '.claude').mkdir()
        f = self.root / '.claude' / 'claude-mods.json'
        err = io.StringIO()
        f.write_text(json.dumps({'agent-bus': {'busDir': 'tmp/bus', 'colour': 'red', 'theme': 'claude'}}))
        with mock.patch.dict(os.environ, {**CLEAN, 'AGENT_BUS_DIR': '.claude/bus'}), redirect_stderr(err):
            cfg = bus.load_config({}, root=self.root)
        self.assertEqual(Path(cfg['dir']), self.root / 'tmp' / 'bus')  # busDir beats the env
        self.assertIn('unknown key "colour"', err.getvalue())
        self.assertIn('"theme" is a plugin option', err.getvalue())
        self.assertNotIn('key "busDir"', err.getvalue())
        f.write_text(json.dumps({'agent-bus': {'dir': 'old/bus'}}))  # the old key still works
        with mock.patch.dict(os.environ, CLEAN):
            self.assertEqual(Path(bus.load_config({}, root=self.root)['dir']), self.root / 'old' / 'bus')

    def test_verbs_never_create_the_bus_implicitly(self):
        for verb in (['whoami', '--as', 'x'], ['digest'], ['who'], ['watch-live', '--as', 'x']):
            err = io.StringIO()
            with redirect_stderr(err):
                code, _ = run(verb, self.root)
            self.assertEqual(code, 2, verb)
            self.assertIn('busDir', err.getvalue())
        self.assertFalse(self.dir.exists())
        code, _ = run(['init'], self.root)
        self.assertEqual(code, 0)
        self.assertTrue(self.dir.is_dir())

    def test_hook_without_a_bus_stays_quiet(self):
        code, out = run(['hook', '--event', 'PostToolUse'], self.root)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['bus'], False)

    def test_init_send_and_hook_serves_mail_once(self):
        run(['init', '--roles', 'coordinator,worker-a'], self.root)
        self.assertEqual(sorted(Bus(self.dir).registry()), ['coordinator', 'worker-a'])
        run(['send', 'worker-a', 'hello'], self.root, AGENT_BUS_ROLE='coordinator')
        _, out = run(['hook', '--event', 'PostToolUse'], self.root, AGENT_BUS_ROLE='worker-a')
        got = json.loads(out)
        self.assertEqual(got['role'], 'worker-a')
        self.assertTrue(got['registered'])
        self.assertIn('hello', got['text'])
        _, out = run(['hook', '--event', 'PostToolUse'], self.root, AGENT_BUS_ROLE='worker-a')
        self.assertIsNone(json.loads(out)['text'])

    def test_fyi_is_never_served(self):
        run(['init', '--roles', 'coordinator,worker-a'], self.root)
        run(['send', 'worker-a', 'just so you know', '--prio', 'fyi'], self.root, AGENT_BUS_ROLE='coordinator')
        _, out = run(['hook', '--event', 'Stop'], self.root, AGENT_BUS_ROLE='worker-a')
        self.assertIsNone(json.loads(out)['text'])

    def test_heartbeat_live_and_stale(self):
        Bus(self.dir)
        self.assertFalse(bus.watch_live(self.dir, 'worker-a'))
        clear = bus.start_heartbeat(self.dir, 'worker-a')
        try:
            time.sleep(0.2)
            self.assertTrue(bus.watch_live(self.dir, 'worker-a'))
            self.assertFalse(bus.watch_live(self.dir, 'worker-a', now=time.time() + bus.WATCH_STALE_S + 1))
        finally:
            clear()
        self.assertFalse(bus.heartbeat_path(self.dir, 'worker-a').exists())

    def test_watch_process_writes_a_heartbeat(self):
        run(['init', '--roles', 'worker-a'], self.root)
        env = {**os.environ, **CLEAN, 'AGENT_BUS_ROLE': 'worker-a'}
        p = subprocess.Popen([sys.executable, str(SCRIPTS / 'bus.py'), 'watch'], cwd=self.root, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(40):
                if bus.watch_live(self.dir, 'worker-a'):
                    break
                time.sleep(0.25)
            self.assertTrue(bus.watch_live(self.dir, 'worker-a'))
            _, out = run(['hook', '--event', 'Stop'], self.root, AGENT_BUS_ROLE='worker-a')
            self.assertTrue(json.loads(out)['watch'])
        finally:
            p.kill()
            p.wait()
            p.stdout.close()
            p.stderr.close()

    def test_deliver_skips_nudges_for_watched_roles(self):
        from orca_bus import deliver
        b = Bus(self.dir)
        b.register('worker-a', handle='h1', kind='claude', mode='file')
        b.register('worker-b', handle='h2', kind='claude', mode='file')
        seen = []
        with mock.patch.object(deliver.Deliverer, 'nudge_deaf', lambda self, reg=None: seen.append(sorted(reg))), \
                mock.patch.object(bus, 'import_orca_bus', lambda cfg: None), \
                mock.patch('orca_bus.cli.main', lambda argv, root=None, default_reply_cmd=None: 0):
            clear = bus.start_heartbeat(self.dir, 'worker-a')
            try:
                time.sleep(0.1)
                code, _ = run(['deliver', '--once'], self.root, AGENT_BUS_BACKEND='orca')
                self.assertEqual(code, 0)
                deliver.Deliverer.nudge_deaf(mock.Mock(bus=b))
            finally:
                clear()
        self.assertEqual(seen, [['worker-b']])

    def test_snapshot_without_a_bus(self):
        code, out = run(['snapshot'], self.root)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)['bus'])

    def test_snapshot_messages_receipts_unread_and_cursor(self):
        run(['init', '--roles', 'coordinator,worker-a'], self.root)
        snap = json.loads(run(['snapshot'], self.root)[1])
        self.assertIn('user', Bus(self.dir).registry())  # registered as the web chat's ensure_user does
        self.assertEqual(snap['messages'], [])
        run(['send', 'worker-a', 'from the human', '--from', 'user'], self.root)
        run(['send', 'user', 'hello @user', '--from', 'coordinator', '--prio', 'urgent'], self.root)
        snap = json.loads(run(['snapshot', '--as', 'user'], self.root)[1])
        self.assertEqual([m['text'] for m in snap['messages']], ['from the human', 'hello @user'])
        mine, theirs = snap['messages']
        self.assertEqual(mine['from'], 'user')
        self.assertEqual(mine['to'], ['worker-a'])
        self.assertIn('worker-a', [r['to'] for r in mine['receipts']])
        self.assertTrue(all(r['tick'] in ('.', 'v', 'vv', '!', 'x', '#') for r in mine['receipts']))
        self.assertTrue(theirs['unread'])
        self.assertTrue(theirs['mention'])
        self.assertIsNotNone(theirs['rid'])
        self.assertEqual(snap['unread'], 1)
        self.assertEqual(snap['mentions'], 1)
        self.assertIn('all', snap['channels'])
        self.assertEqual({r['name'] for r in snap['roles']}, {'coordinator', 'worker-a', 'user'})
        self.assertIn('deliver', snap['health'])
        # nothing changed: the cursor answers without rebuilding the index
        self.assertTrue(json.loads(run(['snapshot', '--since', snap['cursor']], self.root)[1])['unchanged'])
        # a reaction and a read change it
        run(['react', theirs['id'], '✅', '--as', 'user'], self.root)
        run(['read', theirs['rid'], '--as', 'user'], self.root)
        again = json.loads(run(['snapshot', '--since', snap['cursor']], self.root)[1])
        self.assertNotIn('unchanged', again)
        self.assertEqual(again['unread'], 0)
        self.assertEqual(again['messages'][1]['reactions'], {'✅': ['user']})

    def test_snapshot_reply_to_user_and_body(self):
        run(['init', '--roles', 'coordinator'], self.root)
        run(['snapshot'], self.root)
        run(['send', 'coordinator', 'question', '--from', 'user'], self.root)
        q = json.loads(run(['snapshot'], self.root)[1])['messages'][0]
        f = self.root / 'long.md'
        f.write_text('line one\nline two', encoding='utf-8')
        run(['send', 'user', 'answer', '--from', 'coordinator', '--re', q['id'], '--body-file', str(f)], self.root)
        a = json.loads(run(['snapshot'], self.root)[1])['messages'][-1]
        self.assertEqual(a['reply_to'], q['id'])
        self.assertTrue(a['reply_to_me'])
        self.assertTrue(a['body'])
        got = json.loads(run(['snapshot', '--body', a['id']], self.root)[1])
        self.assertIn('line two', got['body'])

    def test_deliver_refuses_without_the_orca_backend(self):
        run(['init'], self.root)
        err = io.StringIO()
        with redirect_stderr(err):
            code, _ = run(['deliver', '--once'], self.root)
        self.assertEqual(code, 2)
        self.assertIn('backend is "none"', err.getvalue())


if __name__ == '__main__':
    unittest.main()
