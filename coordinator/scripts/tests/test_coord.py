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


if __name__ == '__main__':
    unittest.main()
