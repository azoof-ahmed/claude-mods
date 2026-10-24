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

    def test_watch_for_ends_on_its_own_and_clears_the_heartbeat(self):
        run(['init', '--roles', 'worker-a'], self.root)
        env = {**os.environ, **CLEAN, 'AGENT_BUS_ROLE': 'worker-a'}
        started = time.time()
        p = subprocess.run([sys.executable, str(SCRIPTS / 'bus.py'), 'watch', '--for', '1'], cwd=self.root, env=env,
                           capture_output=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertLess(time.time() - started, 15)
        self.assertFalse(bus.watch_live(self.dir, 'worker-a'))

    def test_deliver_skips_nudges_for_watched_roles(self):
        from orca_bus import deliver
        b = Bus(self.dir)
        b.register('worker-a', handle='h1', kind='claude', mode='file')
        b.register('worker-b', handle='h2', kind='claude', mode='file')
        seen = []
        with mock.patch.object(deliver, 'Deliverer', deliver.Deliverer), \
                mock.patch.object(deliver.Deliverer, 'nudge_deaf', lambda self, reg=None: seen.append(sorted(reg))), \
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

    # ---- the nudge reaches an idle worker (orca backend)

    class FakeOrca:
        def terminals(self):
            return {}

    def test_tick_nudges_an_idle_worker_whose_mail_was_filed(self):
        from orca_bus import deliver
        b = Bus(self.dir)
        b.register('coordinator', handle='h0', kind='claude', mode='file')
        b.register('worker-a', handle='h1', kind='claude', mode='file')
        b.send('coordinator', 'worker-a', 'please rebase')
        real = time.time()
        with mock.patch.object(deliver, 'Deliverer', deliver.Deliverer):
            bus.patch_deliverer(deliver, self.dir)
            d = deliver.Deliverer(b, self.FakeOrca(), nudge_after=300)
            d.tick()  # files the normal message into the inbox at once; too young to nudge
            self.assertFalse([m for m in b.unread('worker-a', ('urgent',)) if m.get('topic') == 'nudge'])
            with mock.patch.object(deliver.time, 'time', lambda: real + 400):
                d.tick()  # nothing is queued any more: the nudge must still run
        nudges = [m for m in b.unread('worker-a', ('urgent',)) if m.get('topic') == 'nudge']
        self.assertEqual(len(nudges), 1)
        self.assertIn('please rebase', nudges[0]['text'])

    def test_reply_cmd_never_names_a_bare_python(self):
        with mock.patch.dict(os.environ, {'AGENT_BUS_PYTHON': ''}):
            self.assertTrue(bus.reply_cmd().startswith(sys.executable))
        # the hooks export the python option, default `python`, which may not exist
        with mock.patch.dict(os.environ, {'AGENT_BUS_PYTHON': 'python-not-installed-here'}):
            self.assertTrue(bus.reply_cmd().startswith(sys.executable))
        exe = Path(sys.executable).name
        with mock.patch.dict(os.environ, {'AGENT_BUS_PYTHON': exe}):
            import shutil
            self.assertTrue(bus.reply_cmd().startswith(shutil.which(exe) + ' "'))  # resolved to a full path

    def test_refresh_reply_cmd_survives_a_concurrent_writer(self):
        self.dir.mkdir(parents=True)
        (self.dir / 'config.json').write_text(json.dumps({'reply_cmd': 'python "/gone/bus.py"'}))
        with mock.patch.object(bus.os, 'replace', mock.Mock(side_effect=FileNotFoundError('raced'))):
            self.assertFalse(bus.refresh_reply_cmd(self.dir))
        self.assertEqual([p.name for p in self.dir.iterdir()], ['config.json'])  # no temp file left behind

    def test_stale_reply_cmd_is_refreshed(self):
        cache = self.root / 'cache' / 'agent-bus'
        for v in ('0.1.2', '0.1.3', '0.1.9'):
            (cache / v / 'scripts').mkdir(parents=True)
            (cache / v / 'scripts' / 'bus.py').write_text('')
        mine = f'{sys.executable} "{(cache / "0.1.3/scripts/bus.py").as_posix()}"'
        older = f'{sys.executable} "{(cache / "0.1.2/scripts/bus.py").as_posix()}"'
        newer = f'{sys.executable} "{(cache / "0.1.9/scripts/bus.py").as_posix()}"'
        no_python = f'python-missing-here "{(cache / "0.1.3/scripts/bus.py").as_posix()}"'
        self.assertTrue(bus.reply_cmd_stale(older, mine))
        self.assertTrue(bus.reply_cmd_stale(no_python, mine))
        self.assertTrue(bus.reply_cmd_stale(f'{sys.executable} "/gone/bus.py"', mine))
        self.assertTrue(bus.reply_cmd_stale(None, mine))
        self.assertFalse(bus.reply_cmd_stale(newer, mine))   # another session runs a newer version: no flapping
        self.assertFalse(bus.reply_cmd_stale('orca-bus', mine))  # hand-written: not ours
        self.dir.mkdir(parents=True)
        (self.dir / 'config.json').write_text(json.dumps({'notify': ['coordinator'], 'reply_cmd': 'python "/gone/bus.py"'}))
        self.assertTrue(bus.refresh_reply_cmd(self.dir))
        conf = json.loads((self.dir / 'config.json').read_text())
        self.assertEqual(conf, {'notify': ['coordinator'], 'reply_cmd': bus.reply_cmd()})
        self.assertFalse(bus.refresh_reply_cmd(self.dir))

    def test_ensure_deliver_starts_one_daemon_per_bus_dir(self):
        self.dir.mkdir(parents=True)
        started, live = [], set()

        def start(argv, log):
            started.append(argv)
            live.add(4242 + len(started))
            return 4242 + len(started)
        orca = {'backend': 'orca'}
        hb = self.dir / 'deliver.heartbeat'
        with mock.patch.object(bus, 'pid_is_deliver', lambda pid: pid in live):
            self.assertIsNone(bus.ensure_deliver(self.dir, {'backend': 'none'}, start=start))
            self.assertEqual(bus.ensure_deliver(self.dir, orca, start=start), 4243)
            self.assertEqual(started[0][-3:], ['--dir', str(self.dir), 'deliver'])
            self.assertEqual(json.loads((self.dir / 'deliver.pid').read_text())['pid'], 4243)
            self.assertIsNone(bus.ensure_deliver(self.dir, orca, start=start))  # started, no heartbeat yet
            # a slow tick: the heartbeat is 90 s old but the pid is a live daemon, so no second one
            hb.write_text('')
            self.assertIsNone(bus.ensure_deliver(self.dir, orca, now=time.time() + 90, start=start))
            # another session holds the start lock: no waiting, no start
            import fcntl
            with open(self.dir / 'deliver.lock', 'a+') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                live.clear()
                self.assertIsNone(bus.ensure_deliver(self.dir, orca, now=time.time() + 90, start=start))
            # the daemon died: the next hook starts one
            self.assertEqual(bus.ensure_deliver(self.dir, orca, now=time.time() + 90, start=start), 4244)
            # hung (pid alive, no tick for DELIVER_HUNG_S): stopped and replaced
            killed = []
            with mock.patch.object(bus.os, 'kill', lambda pid, sig: killed.append(pid)):
                self.assertEqual(bus.ensure_deliver(self.dir, orca, now=time.time() + bus.DELIVER_HUNG_S + 1,
                                                    start=start), 4245)
            self.assertEqual(killed, [4244])
            (self.dir / 'deliver.STOP').write_text('')
            live.clear()
            self.assertIsNone(bus.ensure_deliver(self.dir, orca, now=time.time() + 999, start=start))  # stopped on purpose
        self.assertEqual(len(started), 3)

    def test_deliver_refuses_beside_a_running_daemon(self):
        run(['init'], self.root)
        (self.dir / 'deliver.pid').write_text(json.dumps({'pid': 4242}))
        err = io.StringIO()
        from orca_bus import deliver
        with mock.patch.object(deliver, 'Deliverer', deliver.Deliverer), \
                mock.patch.object(bus, 'pid_is_deliver', lambda pid: pid == 4242), redirect_stderr(err), \
                mock.patch('orca_bus.cli.main', lambda argv, root=None, default_reply_cmd=None: 0):  # never a real loop
            code, _ = run(['deliver'], self.root, AGENT_BUS_BACKEND='orca')
        self.assertEqual(code, 1)
        self.assertIn('already runs', err.getvalue())

    def test_pid_is_deliver_rejects_dead_and_foreign_pids(self):
        self.assertFalse(bus.pid_is_deliver(os.getpid()))  # alive, but not a deliver daemon
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        self.assertFalse(bus.pid_is_deliver(p.pid))

    def test_hook_starts_the_daemon_with_the_orca_backend(self):
        run(['init'], self.root)
        calls = []
        with mock.patch.object(bus, 'ensure_deliver', lambda root, cfg, now=None, start=None: calls.append(root) or 7):
            _, out = run(['hook', '--event', 'PostToolUse'], self.root, AGENT_BUS_BACKEND='orca')
            self.assertTrue(json.loads(out)['deliver'])
            _, out = run(['hook', '--event', 'PostToolUse'], self.root)
            self.assertNotIn('deliver', json.loads(out))
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
