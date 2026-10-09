"""Tests for the task tracker CLI core.  Run: python -m unittest discover -s scripts/tests"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import config  # noqa: E402
import importers  # noqa: E402
import minitoml  # noqa: E402
import ops  # noqa: E402
import task  # noqa: E402
import taskdb as db  # noqa: E402
from taskdb import Denied, TaskError  # noqa: E402

ROLES = """
default_level = "member"
[levels]
admin = ["coordinator"]
lead = ["reviewer"]
member = ["worker-a", "worker-b"]
viewer = ["watcher"]
[identity]
no_assign = ["bot"]
"""


class Base(unittest.TestCase):
    roles = ROLES

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='task-tracker-test-'))
        (self.tmp / '.git').mkdir()
        self.env = {}
        self.reconfigure()
        db.init(write_roles=False)
        if self.roles is not None:
            self.cfg.roles_path.write_text(self.roles, encoding='utf-8')
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reconfigure(self, **flags):
        self.cfg = db.configure(config.load(flags, env=self.env, cwd=self.tmp))
        return self.cfg

    def who(self, name):
        return db.resolve_actor(name, env={})

    def new(self, name, title, **kw):
        res, notes = ops.create(self.conn, self.who(name), title, **kw)
        ops.send_notes(self.conn, self.who(name), notes)
        return res['key']

    def status(self, key):
        return db.get_issue(self.conn, key)['status']


class TestLifecycle(Base):
    def test_create_claim_move_done(self):
        k = self.new('worker-a', 'Write the parser')
        self.assertEqual(k, 'TASK-1')
        self.assertEqual(self.status(k), 'backlog')
        ops.claim(self.conn, self.who('worker-a'), k)
        row = db.get_issue(self.conn, k)
        self.assertEqual((row['assignee'], row['status']), ('worker-a', 'in_progress'))
        ops.move(self.conn, self.who('worker-a'), k, 'blocked', note='waits on the API')
        self.assertEqual(self.status(k), 'blocked')
        ops.move(self.conn, self.who('worker-a'), k, 'done')
        row = db.get_issue(self.conn, k)
        self.assertEqual(row['status'], 'done')
        self.assertIsNotNone(row['closed_at'])
        actions = [e['action'] for e in db.issue_dict(self.conn, row, full=True)['history']]
        self.assertEqual(actions[:3], ['create', 'claim', 'move'])

    def test_claim_is_exclusive(self):
        k = self.new('worker-a', 'Only one owner')
        ops.claim(self.conn, self.who('worker-a'), k)
        with self.assertRaises(Denied) as cm:
            ops.claim(self.conn, self.who('worker-b'), k)
        self.assertEqual(cm.exception.rule, 'claim.one-assignee')

    def test_release_returns_to_todo(self):
        k = self.new('worker-a', 'Give it back')
        ops.claim(self.conn, self.who('worker-a'), k)
        ops.release(self.conn, self.who('worker-a'), k)
        row = db.get_issue(self.conn, k)
        self.assertEqual((row['assignee'], row['status']), (None, 'todo'))

    def test_review_flow(self):
        k = self.new('worker-a', 'Needs review', reviewer='reviewer')
        ops.claim(self.conn, self.who('worker-a'), k)
        with self.assertRaises(Denied) as cm:
            ops.move(self.conn, self.who('worker-a'), k, 'done')
        self.assertEqual(cm.exception.rule, 'done.needs-review')
        ops.move(self.conn, self.who('worker-a'), k, 'in_review')
        with self.assertRaises(Denied):
            ops.review(self.conn, self.who('worker-b'), k, 'approve')
        ops.review(self.conn, self.who('reviewer'), k, 'reject', note='missing tests')
        self.assertEqual(self.status(k), 'in_progress')
        ops.move(self.conn, self.who('worker-a'), k, 'in_review')
        ops.review(self.conn, self.who('reviewer'), k, 'approve')
        self.assertEqual(self.status(k), 'done')

    def test_blocking_and_unblock_note(self):
        a = self.new('worker-a', 'Build the schema')
        b = self.new('worker-b', 'Use the schema')
        ops.claim(self.conn, self.who('worker-b'), b)
        ops.block(self.conn, self.who('worker-b'), b, a)
        with self.assertRaises(TaskError):
            ops.block(self.conn, self.who('coordinator'), a, b)  # a cycle
        _, notes = ops.move(self.conn, self.who('coordinator'), a, 'done')
        self.assertTrue(any(to == 'worker-b' and 'UNBLOCKED' in text for to, text, _, _ in notes))

    def test_duplicate_detection(self):
        self.new('worker-a', 'Fix the login redirect')
        with self.assertRaises(Denied) as cm:
            self.new('worker-b', 'Fix the login redirect!')
        self.assertEqual(cm.exception.rule, 'dup.detect')
        self.new('worker-b', 'Fix the login redirect!', force_dup=True)

    def test_labels_vocabulary(self):
        self.new('worker-a', 'Labelled', labels=['bug'])
        with self.assertRaises(TaskError):
            self.new('worker-a', 'Bad label', labels=['nonsense'])
        self.reconfigure(labels='')
        self.new('worker-a', 'Any label goes', labels=['nonsense'])

    def test_inbox_notes(self):
        k = self.new('coordinator', 'Assigned work', assignee='worker-a')
        n = self.conn.execute("SELECT COUNT(*) FROM notes WHERE recipient='worker-a'").fetchone()[0]
        self.assertEqual(n, 1)
        ops.send_notes(self.conn, self.who('worker-b'), ops.comment(self.conn, self.who('worker-b'), k,
                                                                    'ping @reviewer')[1])
        n = self.conn.execute("SELECT COUNT(*) FROM notes WHERE recipient='reviewer'").fetchone()[0]
        self.assertEqual(n, 1)


class TestPermissions(Base):
    def test_viewer_is_read_only(self):
        with self.assertRaises(Denied) as cm:
            self.new('watcher', 'Not allowed')
        self.assertEqual(cm.exception.rule, 'viewer.read-only')

    def test_unlisted_name_gets_default_level(self):
        self.assertEqual(self.who('someone-new').level, 'member')

    def test_member_updates_only_own_tasks(self):
        k = self.new('worker-a', 'Mine')
        ops.claim(self.conn, self.who('worker-a'), k)
        with self.assertRaises(Denied) as cm:
            ops.move(self.conn, self.who('worker-b'), k, 'blocked')
        self.assertEqual(cm.exception.rule, 'member.own-tasks-only')
        ops.move(self.conn, self.who('reviewer'), k, 'blocked')  # a lead may

    def test_member_assigning_someone_else_becomes_a_proposal(self):
        res, _ = ops.create(self.conn, self.who('worker-a'), 'For b', assignee='worker-b')
        row = db.get_issue(self.conn, res['key'])
        self.assertEqual((row['assignee'], row['proposed']), (None, 'worker-b'))
        with self.assertRaises(Denied):
            ops.assign(self.conn, self.who('worker-a'), res['key'], 'worker-b')
        ops.assign(self.conn, self.who('reviewer'), res['key'], 'worker-b')
        self.assertEqual(db.get_issue(self.conn, res['key'])['assignee'], 'worker-b')

    def test_no_assign_names(self):
        with self.assertRaises(Denied):
            self.new('coordinator', 'For the bot', assignee='bot')

    def test_import_needs_lead(self):
        with self.assertRaises(Denied):
            db.can(self.who('worker-a'), 'import')
        db.can(self.who('reviewer'), 'import')


class TestNoRolesFile(Base):
    roles = None

    def test_everyone_may_do_everything(self):
        self.assertFalse(self.cfg.roles_path.exists())
        me = self.who('anyone')
        self.assertEqual(me.level, 'admin')
        k = self.new('anyone', 'Free for all')
        ops.claim(self.conn, self.who('other'), k)
        ops.move(self.conn, me, k, 'done')


class TestConflicts(Base):
    def test_globs_overlap(self):
        o = db.globs_overlap
        self.assertTrue(o('src/**', 'src/api/x.py'))
        self.assertTrue(o('src/api/**', 'src/**'))
        self.assertTrue(o('src/*.py', 'src/main.py'))
        self.assertTrue(o('./src/', 'src/a.txt'))
        self.assertTrue(o('SRC\\A.txt', 'src/a.txt'))
        self.assertFalse(o('src/api/**', 'docs/**'))
        self.assertFalse(o('src/a.py', 'src/b.py'))

    def test_claim_refuses_overlap_and_force_is_logged(self):
        a = self.new('worker-a', 'Refactor api', touches=['src/api/**'])
        b = self.new('worker-b', 'Touch handler', touches=['src/api/handler.py'])
        ops.claim(self.conn, self.who('worker-a'), a)
        with self.assertRaises(Denied) as cm:
            ops.claim(self.conn, self.who('worker-b'), b)
        self.assertEqual(cm.exception.rule, 'conflict.touches')
        self.assertEqual(self.status(b), 'backlog')  # the whole claim rolled back
        self.assertIsNone(db.get_issue(self.conn, b)['assignee'])
        _, notes = ops.claim(self.conn, self.who('worker-b'), b, force_reason='hotfix')
        self.assertEqual(self.status(b), 'in_progress')
        self.assertTrue(any(to == 'worker-a' for to, *_ in notes))
        self.assertEqual(len(db.all_conflicts(self.conn)), 1)
        ev = self.conn.execute("SELECT * FROM events WHERE action='conflict_override'").fetchone()
        self.assertIn('hotfix', ev['detail'])

    def test_touch_on_in_progress_task_checks(self):
        a = self.new('worker-a', 'Docs pass', touches=['docs/**'])
        b = self.new('worker-b', 'Other work')
        ops.claim(self.conn, self.who('worker-a'), a)
        ops.claim(self.conn, self.who('worker-b'), b)
        with self.assertRaises(Denied):
            ops.touch(self.conn, self.who('worker-b'), b, add=['docs/readme.md'])
        ops.touch(self.conn, self.who('worker-b'), b, add=['src/x.py'])
        self.assertEqual(db.all_conflicts(self.conn), [])


class TestConfigAndPrefix(Base):
    def test_default_prefix(self):
        self.assertEqual(db.fmt_key(7), 'TASK-7')
        self.assertEqual(db.parse_key('task-7'), 7)
        self.assertEqual(db.parse_key('7'), 7)
        with self.assertRaises(TaskError):
            db.parse_key('BUG-7')

    def test_custom_prefix_from_flag(self):
        self.reconfigure(idPrefix='WEB-')
        k = self.new('worker-a', 'Prefixed')
        self.assertEqual(k, 'WEB-1')
        self.assertEqual(db.parse_key('web-1'), 1)

    def test_precedence_flag_env_project_default(self):
        (self.tmp / '.claude').mkdir(exist_ok=True)
        (self.tmp / '.claude' / 'claude-mods.json').write_text(json.dumps(
            {'task-tracker': {'idPrefix': 'PROJ', 'labels': ['a', 'b'], 'dbPath': 'x/t.db'}}), encoding='utf-8')
        c = config.load({}, env={}, cwd=self.tmp)
        self.assertEqual((c.prefix, c.labels, c.db_path), ('PROJ', ['a', 'b'], self.tmp / 'x' / 't.db'))
        env = {'TASKS_PREFIX': 'ENV', 'TASKS_ROLES': 'env/roles.toml'}
        c = config.load({}, env=env, cwd=self.tmp)
        self.assertEqual(c.prefix, 'PROJ')  # the project file beats the environment
        self.assertEqual((c.roles_path, c.sources['rolesFile']), (self.tmp / 'env' / 'roles.toml', 'env'))
        c = config.load({'idPrefix': 'FLAG'}, env=env, cwd=self.tmp)
        self.assertEqual((c.prefix, c.sources['idPrefix']), ('FLAG', 'flag'))
        self.assertEqual(config.load({}, env={}, cwd=self.tmp).sources['rolesFile'], 'default')
        (self.tmp / '.claude' / 'claude-mods.json').unlink()
        self.assertEqual(config.load({}, env=env, cwd=self.tmp).prefix, 'ENV')

    def test_project_root_from_subdirectory(self):
        sub = self.tmp / 'a' / 'b'
        sub.mkdir(parents=True)
        self.assertEqual(config.find_root(sub), self.tmp.resolve())


class TestImporters(Base):
    def test_python_importer_is_idempotent_and_keeps_tracker_changes(self):
        src = self.tmp / 'my_source.py'
        src.write_text(
            "def records(ctx):\n"
            "    return [{'ext_key': 'src:1', 'title': 'Imported one', 'status': 'todo', 'labels': ['bug'],\n"
            "             'links': ['url:https://example.invalid/1'], 'comments': ['from the source']},\n"
            "            {'ext_key': 'src:2', 'title': 'Child', 'parent': 'src:1'}]\n", encoding='utf-8')
        self.reconfigure(importers='my_source.py')
        boss = self.who('coordinator')
        stats = importers.run_all(self.conn, boss)
        self.assertEqual(stats['my_source.py']['new'], 2)
        row = self.conn.execute("SELECT * FROM issues WHERE ext_key='src:1'").fetchone()
        child = self.conn.execute("SELECT * FROM issues WHERE ext_key='src:2'").fetchone()
        self.assertEqual(child['parent'], row['id'])
        ops.claim(self.conn, self.who('worker-a'), row['id'])  # a change made in the tracker
        stats = importers.run_all(self.conn, boss)
        self.assertEqual(stats['my_source.py'].get('new', 0), 0)
        self.assertEqual(self.status(row['id']), 'in_progress')
        n = self.conn.execute('SELECT COUNT(*) FROM comments WHERE issue=?', (row['id'],)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_command_importer(self):
        script = self.tmp / 'emit.py'
        script.write_text("import json\nprint(json.dumps({'ext_key': 'cmd:1', 'title': 'From a command'}))\n",
                          encoding='utf-8')
        self.reconfigure(importers=f'"{sys.executable}" emit.py')
        stats = importers.run_all(self.conn, self.who('coordinator'))
        self.assertEqual(list(stats.values())[0]['new'], 1)

    def test_no_importers_configured(self):
        with self.assertRaises(TaskError):
            importers.run_all(self.conn, self.who('coordinator'))


class TestToml(unittest.TestCase):
    def test_fallback_parser_reads_the_template(self):
        d = minitoml.parse_simple(db.ROLES_TEMPLATE)
        self.assertEqual(d['default_level'], 'member')
        self.assertEqual(d['levels']['admin'], ['user', 'coordinator'])
        self.assertEqual(d['identity']['strict_names'], False)
        self.assertEqual(d['review']['default_reviewer'], '')


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='task-tracker-cli-'))
        (self.tmp / '.git').mkdir()
        self.old = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = task.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_init_then_use(self):
        rc, _, err = self.run_cli('list')
        self.assertEqual(rc, 4)
        self.assertIn('init', err)
        rc, out, _ = self.run_cli('init')
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / '.claude' / 'tasks' / 'tasks.db').exists())
        self.assertTrue((self.tmp / '.claude' / 'tasks' / 'roles.toml').exists())
        rc, out, _ = self.run_cli('--prefix', 'OPS', '--as', 'coordinator', 'new', 'First', '--json')
        self.assertEqual(json.loads(out)['key'], 'OPS-1')
        rc, out, _ = self.run_cli('--prefix', 'OPS', '--as', 'coordinator', 'snapshot')
        snap = json.loads(out)
        self.assertEqual(snap['prefix'], 'OPS')
        self.assertEqual(snap['stats']['by_status']['backlog'], 1)
        rc, out, _ = self.run_cli('where', '--json')
        self.assertTrue(json.loads(out)['db_exists'])


if __name__ == '__main__':
    unittest.main()
