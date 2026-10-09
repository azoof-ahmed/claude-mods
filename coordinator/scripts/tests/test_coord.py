"""Tests for scripts/coord.py: init, spawn (dry and manual), lanes, questions, close-idle, config layers."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import coord  # noqa: E402

CLEAN = {coord.env_key(k): '' for k in coord.DEFAULTS}
CLEAN.update({'AGENT_BUS_ROLE': '', 'AGENT_BUS_CLI': os.devnull + '-missing'})


class CoordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / '.git').mkdir()
        self.old = os.getcwd()
        os.chdir(self.root)
        self.env = mock.patch.dict(os.environ, CLEAN)
        self.env.start()
        # no sibling agent-bus: the tests run the coordinator alone
        self.nobus = mock.patch.object(coord, 'bus_cli', lambda: None)
        self.nobus.start()

    def tearDown(self):
        self.nobus.stop()
        self.env.stop()
        os.chdir(self.old)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = coord.main(list(argv))
            except SystemExit as ex:
                code = ex.code if isinstance(ex.code, int) else 2
                out.write(str(ex.code))
        return code, out.getvalue()

    def test_init_scaffolds_everything_and_saves_config(self):
        code, out = self.run_cli('init')
        self.assertEqual(code, 0)
        for p in ('.claude/coord/RULES_CORE.md', '.claude/coord/lanes/README.md', '.claude/coord/briefs/TEMPLATE.md',
                  '.claude/coord/briefs/RESUME.md', '.claude/coord/briefs/scheduler.md',
                  '.claude/coord/briefs/HANDOFF_PROTOCOL.md', '.claude/coord/reports/questions.jsonl'):
            self.assertTrue((self.root / p).exists(), p)
        rules = (self.root / '.claude/coord/RULES_CORE.md').read_text(encoding='utf-8')
        self.assertNotIn('{{', rules)
        self.assertIn('250k', rules)
        cfg = json.loads((self.root / '.claude/claude-mods.json').read_text())['coordinator']
        self.assertEqual(cfg['backend'], 'manual')

    def test_project_file_beats_environment(self):
        self.run_cli('init')
        with mock.patch.dict(os.environ, {'COORD_MAX_LANES': '9'}):
            cfg = coord.Config(self.root)
            self.assertEqual(cfg['maxLanes'], 6)  # init saved 6 into the project file
        (self.root / '.claude/claude-mods.json').unlink()
        with mock.patch.dict(os.environ, {'COORD_MAX_LANES': '9'}):
            self.assertEqual(coord.Config(self.root)['maxLanes'], 9)

    def test_spawn_manual_records_the_lane_and_prints_the_command(self):
        self.run_cli('init')
        code, out = self.run_cli('spawn', 'worker-a', '--model', 'sonnet')
        self.assertEqual(code, 0)
        self.assertIn("AGENT_BUS_ROLE", out)
        self.assertIn('claude --model sonnet "Read .claude/coord/briefs/RESUME.md and execute it. Your role is worker-a."', out)
        lanes = json.loads((self.root / '.claude/coord/lanes/lanes.json').read_text())
        self.assertEqual(lanes['worker-a']['state'], 'live')

    def test_spawn_respects_max_lanes_and_rejects_bad_roles(self):
        self.run_cli('init')
        code, _ = self.run_cli('spawn', 'a', '--max-lanes', '1')
        self.assertEqual(code, 0)
        code, out = self.run_cli('spawn', 'b', '--max-lanes', '1')
        self.assertNotEqual(code, 0)
        self.assertIn('max 1', out)
        code, out = self.run_cli('spawn', 'bad role;rm')
        self.assertNotEqual(code, 0)

    def test_spawn_dry_run_builds_backend_commands(self):
        self.run_cli('init')
        _, out = self.run_cli('spawn', 'w', '--backend', 'tmux', '--dry-run')
        self.assertIn('tmux new-window', out)
        self.assertIn('AGENT_BUS_ROLE=w', out)
        _, out = self.run_cli('spawn', 'w', '--backend', 'orca', '--dry-run')
        self.assertIn('orca terminal create', out)
        self.assertFalse(json.loads((self.root / '.claude/coord/lanes/lanes.json').read_text()))

    def test_questions_queue_and_answer(self):
        self.run_cli('init')
        self.run_cli('ask', 'Ship it?', '--from', 'worker-a', '--options', 'yes|no')
        _, out = self.run_cli('status')
        self.assertIn('q1 from worker-a: Ship it? [yes|no]', out)
        _, out = self.run_cli('answer', 'q1', 'yes')
        self.assertIn('tell worker-a: yes', out)
        _, out = self.run_cli('questions')
        self.assertIn('no open questions', out)

    def test_close_idle_only_closes_safe_lanes(self):
        self.run_cli('init')
        self.run_cli('spawn', 'done-lane')
        self.run_cli('spawn', 'busy-lane')
        cards = self.root / '.claude/coord/lanes'
        (cards / 'done-lane.md').write_text('## State\n- finished\n\nSAFE TO CLOSE\n')
        (cards / 'busy-lane.md').write_text('## State\n- running\n\nKEEP OPEN: build running\n')
        _, out = self.run_cli('close-idle')
        self.assertIn('closed: done-lane', out)
        lanes = json.loads((cards / 'lanes.json').read_text())
        self.assertEqual(lanes['done-lane']['state'], 'closed')
        self.assertEqual(lanes['busy-lane']['state'], 'live')

    def test_lanes_show_tracker_tasks(self):
        import sqlite3
        self.run_cli('init')
        self.run_cli('spawn', 'worker-a')
        db = self.root / '.claude/tasks/tasks.db'
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE issues (id INTEGER PRIMARY KEY, title TEXT, status TEXT, assignee TEXT)')
        con.execute("INSERT INTO issues VALUES (7, 'parser', 'in_progress', 'worker-a')")
        con.commit()
        con.close()
        code, out = self.run_cli('lanes')
        self.assertEqual(code, 0)
        self.assertIn('worker-a', out)
        self.assertIn('#7 in_progress', out)

    def test_context_for_worker_and_unconfigured(self):
        _, out = self.run_cli('context')
        self.assertFalse(json.loads(out)['configured'])
        self.run_cli('init')
        with mock.patch.dict(os.environ, {'AGENT_BUS_ROLE': 'worker-a'}):
            _, out = self.run_cli('context')
        c = json.loads(out)
        self.assertTrue(c['configured'])
        self.assertFalse(c['isCoordinator'])
        self.assertEqual(c['card'], '.claude/coord/lanes/worker-a.md')

    def test_preflight_waits_below_thresholds(self):
        with mock.patch.object(coord, 'free_ram_gb', lambda: 1.0), mock.patch.object(coord, 'battery', lambda: (20, False)):
            code, out = self.run_cli('preflight', '--min-ram-gb', '4', '--min-battery', '40')
        self.assertEqual(code, 1)
        self.assertIn('free RAM 1.0 GB < 4.0 GB', out)
        self.assertIn('battery 20%', out)
        with mock.patch.object(coord, 'free_ram_gb', lambda: 16.0), mock.patch.object(coord, 'battery', lambda: (20, True)):
            code, out = self.run_cli('preflight')
        self.assertEqual(code, 0)

    # ---- usage and the graceful pause

    def test_parse_usage_screen(self):
        screen = ("   Current session\n   \u2588\u2588\u2588\u258c          7% used\n   Resets 7:30am\n"
                  "   Current week (all models)\n   \u2588\u2588\u2588       67% used\n   Resets Oct 13\n"
                  "   Current week (Other)\n                0% used\n")
        self.assertEqual(coord.parse_usage(screen), {'session': 7, 'week': 67})
        self.assertEqual(coord.parse_usage('nothing here'), {'session': None, 'week': None})

    # the live /usage screen of `claude --model haiku` on Windows, verbatim
    LIVE_USAGE = ('Current session\n'
                  '███▌ 7% used\n'
                  'Resets 7:29am (Indian/Maldives)\n'
                  'Current week (all models)\n'
                  '█████████ 67% used\n'
                  'Resets Oct 13, 8:59am (Indian/Maldives)\n')

    def test_parse_usage_live_screen(self):
        self.assertEqual(coord.parse_usage(self.LIVE_USAGE), {'session': 7, 'week': 67})
        self.assertEqual(coord.parse_usage(self.LIVE_USAGE.replace('\n', '\r\n')), {'session': 7, 'week': 67})
        # the screen before /usage has drawn: nothing yet, never a stale number
        self.assertEqual(coord.parse_usage('Current session\n'), {'session': None, 'week': None})

    def test_watch_never_reuses_a_reading_without_numbers(self):
        # the 0.2.2 live failure: a failed 03:46 probe (None) was reused as fresh at 04:02, so nothing was probed
        self.run_cli('init')
        cfg = coord.Config(self.root)
        coord.write_usage(cfg, {'session': None, 'week': None}, 'probe:orca')
        probes = []

        def probe(cfg_, dry=False):
            probes.append(1)
            return coord.parse_usage(self.LIVE_USAGE)
        with mock.patch.object(coord, 'probe_usage', probe):
            _, out = self.run_cli('usage-watch', '--once')
        self.assertEqual(len(probes), 1)
        self.assertIn('week=67% session=7%', out)
        # a probe that reads nothing keeps the good reading
        with mock.patch.object(coord, 'probe_usage', lambda c, dry=False: {'session': None, 'week': None}):
            _, out = self.run_cli('usage-watch', '--once', '--probe')
        self.assertIn('no numbers', out)
        self.assertEqual(coord.read_usage(coord.Config(self.root))['week'], 67)
        # a fresh good reading is reused, and says so
        with mock.patch.object(coord, 'probe_usage', probe):
            _, out = self.run_cli('usage-watch', '--once')
        self.assertEqual(len(probes), 1)
        self.assertIn('(reading of', out)

    def test_debug_saves_every_screen_read(self):
        self.run_cli('init')
        cfg = coord.Config(self.root)
        coord.debug_flag(cfg, ['--debug'])
        try:
            with mock.patch.object(coord, 'run', lambda argv, dry=False, **kw: coord.subprocess.CompletedProcess(
                    argv, 0, json.dumps({'result': {'terminal': {'tail': self.LIVE_USAGE.split('\n')}}}), '')):
                with redirect_stdout(io.StringIO()):
                    self.assertIn('67% used', coord.orca_screen('term_1'))
        finally:
            coord.PROBE_DEBUG['dir'] = None
        saved = sorted(p.name for p in (cfg.path('reportsDir') / 'usage-debug').iterdir())
        self.assertEqual([n.split('.', 1)[1] for n in saved], ['raw.txt', 'screen.txt'])

    def test_wait_for_usage_polls_until_the_week_line(self):
        screens = iter(['> /usage', 'Current session\n█ 7% used\n', self.LIVE_USAGE])
        with mock.patch.object(coord.time, 'sleep', lambda s: None):
            screen = coord.wait_for_usage(lambda: next(screens), timeout=60)
        self.assertEqual(coord.parse_usage(screen)['week'], 67)

    def test_subprocess_output_is_decoded_as_utf8(self):
        # 0x9d is undefined in cp1252: the crash the live run hit
        r = coord.run([sys.executable, '-c',
                       'import sys; sys.stdout.buffer.write("█ 67% used ”".encode("utf-8"))'])
        self.assertIn('67% used', r.stdout)
        r = coord.run([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"\\x9d\\xff ok")'])
        self.assertIn('ok', r.stdout)

    def test_probe_schedule(self):
        cfg = coord.Config(self.root)
        self.assertEqual(coord.probe_interval_min(cfg, None), 60)
        self.assertEqual(coord.probe_interval_min(cfg, 50), 60)
        self.assertEqual(coord.probe_interval_min(cfg, 70), 20)
        self.assertEqual(coord.probe_interval_min(cfg, 79), 10)

    def test_record_usage_alerts_once_per_crossing(self):
        self.run_cli('init')
        _, out = self.run_cli('record-usage', '--week', '81', '--session', '10', '--alert')
        self.assertIn('WEEKLY usage 81% >= 80%', out)
        _, out = self.run_cli('record-usage', '--week', '82', '--alert')
        self.assertNotIn('WEEKLY', out)
        _, out = self.run_cli('usage')
        self.assertIn('week 82%', out)
        alerts = (self.root / '.claude/coord/reports/alerts.jsonl').read_text().splitlines()
        self.assertEqual(len(alerts), 1)

    def test_probe_refuses_backends_that_cannot_read_a_screen(self):
        self.run_cli('init')
        code, out = self.run_cli('usage', '--probe')
        self.assertNotEqual(code, 0)
        self.assertIn('cannot probe', out)

    def test_probe_tmux_dry_run(self):
        self.run_cli('init')
        _, out = self.run_cli('usage', '--probe', '--dry-run', '--backend', 'tmux')
        self.assertIn('claude --model haiku', out)

    # the real `orca terminal create --json` output: the handle is at result.terminal.handle
    ORCA_CREATE = json.dumps({'id': 'c0ffee', 'ok': True, 'result': {'terminal': {
        'handle': 'term_123', 'title': 'usage-probe', 'worktreeId': 'w::E:/repo', 'tabId': 't1'}}})

    def fake_orca(self, create_out, calls):
        def run(argv, dry=False, **kw):
            calls.append(argv)
            out = ''
            if argv[:3] == ['orca', 'terminal', 'create']:
                out = create_out
            elif argv[:3] == ['orca', 'terminal', 'list']:
                # an older tab with the probe's title is never touched; the orphan exists once create has run
                terms = [{'handle': 'term_old', 'title': 'usage-probe'}, {'handle': 'term_other', 'title': 'x'}]
                if any(c[:3] == ['orca', 'terminal', 'create'] for c in calls):
                    terms.append({'handle': 'term_orphan', 'title': 'usage-probe'})
                out = json.dumps({'ok': True, 'result': {'terminals': terms}})
            elif argv[:3] == ['orca', 'terminal', 'read']:
                out = 'Current session  12% used\nCurrent week (all models)  34% used\n'
            return coord.subprocess.CompletedProcess(argv, 0, out, '')
        return run

    def test_orca_handle_accepts_all_create_shapes(self):
        self.assertEqual(coord.orca_handle(self.ORCA_CREATE), 'term_123')
        self.assertEqual(coord.orca_handle('{"ok":true,"result":{"handle":"term_a"}}'), 'term_a')
        self.assertEqual(coord.orca_handle('{"handle":"term_b"}'), 'term_b')
        self.assertIsNone(coord.orca_handle('not json'))
        self.assertIsNone(coord.orca_handle('{"ok":true,"result":{"terminal":{}}}'))

    def test_orca_spawn_and_probe_read_the_real_create_output(self):
        self.run_cli('init')
        cfg = coord.Config(self.root)
        cfg['backend'] = 'orca'
        calls = []
        with mock.patch.object(coord, 'run', self.fake_orca(self.ORCA_CREATE, calls)), \
                mock.patch.object(coord.time, 'sleep', lambda s: None):
            self.assertEqual(coord.open_terminal(cfg, 'w1', str(self.root), 'claude'), ('term_123', 'orca'))
            coord.probe_usage(cfg)
        closes = [c for c in calls if c[:3] == ['orca', 'terminal', 'close']]
        self.assertEqual(closes, [['orca', 'terminal', 'close', '--terminal', 'term_123', '--tab']])

    def test_orca_unreadable_create_closes_the_tab_by_title(self):
        self.run_cli('init')
        cfg = coord.Config(self.root)
        cfg['backend'] = 'orca'
        calls = []
        with mock.patch.object(coord, 'run', self.fake_orca('{"ok":true,"result":{}}', calls)):
            with self.assertRaises(SystemExit):
                coord.probe_usage(cfg)
        closes = [c for c in calls if c[:3] == ['orca', 'terminal', 'close']]
        self.assertEqual(closes, [['orca', 'terminal', 'close', '--terminal', 'term_orphan', '--tab']])

    def test_orca_probe_reads_the_json_screen_and_always_closes_its_tab(self):
        self.run_cli('init')
        cfg = coord.Config(self.root)
        cfg['backend'] = 'orca'
        screen = json.dumps({'ok': True, 'result': {'terminal': {
            'handle': 'term_123', 'source': 'screen', 'tail': self.LIVE_USAGE.split('\n')}}})
        base = self.fake_orca(self.ORCA_CREATE, [])

        def run_ok(argv, dry=False, **kw):
            calls.append(argv)
            if argv[:3] == ['orca', 'terminal', 'read']:
                return coord.subprocess.CompletedProcess(argv, 0, screen, '')
            return base(argv)
        calls = []
        with mock.patch.object(coord, 'run', run_ok), mock.patch.object(coord.time, 'sleep', lambda s: None):
            self.assertEqual(coord.probe_usage(cfg), {'session': 7, 'week': 67})
        self.assertIn(['orca', 'terminal', 'close', '--terminal', 'term_123', '--tab'], calls)

        # a failure in the middle (the cp1252 crash) still closes the tab
        def run_crash(argv, dry=False, **kw):
            calls.append(argv)
            if argv[:3] == ['orca', 'terminal', 'read']:
                raise UnicodeDecodeError('charmap', b'\x9d', 0, 1, 'undefined')
            return base(argv)
        calls = []
        with mock.patch.object(coord, 'run', run_crash), mock.patch.object(coord.time, 'sleep', lambda s: None):
            with self.assertRaises(UnicodeDecodeError):
                coord.probe_usage(cfg)
        self.assertIn(['orca', 'terminal', 'close', '--terminal', 'term_123', '--tab'], calls)

        # a crash inside create itself: the tab is found by its title and closed
        base = self.fake_orca(self.ORCA_CREATE, [])

        def run_create_crash(argv, dry=False, **kw):
            calls.append(argv)
            if argv[:3] == ['orca', 'terminal', 'create']:
                base(argv)  # orca made the tab; reading its output crashed
                raise UnicodeDecodeError('charmap', b'\x9d', 0, 1, 'undefined')
            return base(argv)
        calls = []
        with mock.patch.object(coord, 'run', run_create_crash):
            with self.assertRaises(UnicodeDecodeError):
                coord.probe_usage(cfg)
        self.assertIn(['orca', 'terminal', 'close', '--terminal', 'term_orphan', '--tab'], calls)

    def test_graceful_pause_checkpoint_remind_interrupt_and_resume(self):
        self.run_cli('init')
        for r in ('fast', 'slow', 'silent'):
            self.run_cli('spawn', r)
        t0 = 1_000_000.0
        with mock.patch.object(coord, 'clock', lambda: t0):
            _, out = self.run_cli('pause')
        self.assertIn('asked 3 lane(s)', out)
        self.assertIn('(no bus) tell fast: PAUSE', out)
        with mock.patch.dict(os.environ, {'AGENT_BUS_ROLE': 'fast'}):
            code, _ = self.run_cli('paused')
        self.assertEqual(code, 0)
        (self.root / '.claude/coord/lanes/slow.md').write_text('## State\n- parked\n\nSAFE TO CLOSE\n')
        with mock.patch.object(coord, 'clock', lambda: t0 + 11 * 60):
            _, out = self.run_cli('pause', '--tick')
        self.assertIn('REMINDER', out)
        self.assertIn('2 paused', out)
        self.assertIn('1 asked', out)
        with mock.patch.object(coord, 'clock', lambda: t0 + 21 * 60):
            _, out = self.run_cli('pause', '--tick')
        self.assertIn('complete', out)
        st = json.loads((self.root / '.claude/coord/lanes/pause.json').read_text())
        self.assertEqual(st['lanes']['silent']['state'], 'interrupted')
        self.assertTrue((self.root / '.claude/coord/reports/RESUME_LIST.md').exists())
        lanes = json.loads((self.root / '.claude/coord/lanes/lanes.json').read_text())
        self.assertEqual({lanes[r]['state'] for r in ('fast', 'slow', 'silent')}, {'paused'})
        _, out = self.run_cli('context')
        self.assertEqual(json.loads(out)['pause'], 'complete')
        _, out = self.run_cli('resume')
        self.assertIn('resume: fast, silent, slow', out)
        lanes = json.loads((self.root / '.claude/coord/lanes/lanes.json').read_text())
        self.assertEqual({lanes[r]['state'] for r in ('fast', 'slow', 'silent')}, {'live'})
        self.assertEqual(lanes['fast']['generation'], 2)
        self.assertEqual(lanes['fast']['brief'], '.claude/coord/briefs/RESUME.md')


if __name__ == '__main__':
    unittest.main()
