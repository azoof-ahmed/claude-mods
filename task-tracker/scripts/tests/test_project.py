"""The project a command works on: worktrees, two projects, paths from the environment, help and unknown flags."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import project  # noqa: E402
import task  # noqa: E402

SECTION, STATE = 'task-tracker', Path('.claude') / 'tasks' / 'tasks.db'


def tree(root):
    """Every file outside .git with its content, to show that nothing changed."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*'))
            if p.is_file() and '.git' not in p.relative_to(root).parts}


class Places(unittest.TestCase):
    """Two made-up projects in one folder, a home folder beside them, and a worktree of the first project."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name).resolve()
        self.home = self.top / 'home'
        self.alpha, self.beta = self.home / 'alpha', self.home / 'beta'
        (self.home / '.claude').mkdir(parents=True)
        self.env = {'HOME': str(self.home), 'USERPROFILE': str(self.home), 'PATH': os.environ.get('PATH', ''),
                    'GIT_CONFIG_NOSYSTEM': '1'}
        for p in (self.alpha, self.beta):
            p.mkdir()
            self.git(p, 'init', '-q')
            self.git(p, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty',
                     '-m', 'first')
        self.lane = self.home / 'alpha-lane'
        self.git(self.alpha, 'worktree', 'add', '-q', str(self.lane))

    def git(self, cwd, *args):
        subprocess.run(['git', *args], cwd=cwd, env=self.env, check=True, capture_output=True)

    def section(self, root, name, **keys):
        (root / '.claude').mkdir(exist_ok=True)
        f = root / '.claude' / 'claude-mods.json'
        data = json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
        f.write_text(json.dumps({**data, name: keys}), encoding='utf-8')

    def cli(self, cwd, *args, **env):
        r = subprocess.run([sys.executable, str(SCRIPTS / 'task.py'), *args], cwd=cwd, env={**self.env, **env},
                           capture_output=True, text=True, encoding='utf-8', stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr

    def lane_record(self, root, **lanes):
        d = root / '.claude' / 'coord' / 'lanes'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'lanes.json').write_text(json.dumps({r: {'repo': str(p)} for r, p in lanes.items()}), encoding='utf-8')

    def unchanged(self, cwd, *args, **env):
        before = tree(self.top)
        got = self.cli(cwd, *args, **env)
        self.assertEqual(tree(self.top), before, args)
        return got

class ProjectTest(Places):
    """project.py itself."""

    def test_home_is_not_a_project(self):
        (self.home / 'loose').mkdir()
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(project.find_root(self.home / 'loose'), self.home / 'loose')
            self.assertFalse(project.Project(self.home).holds(str(self.beta / 'x')))
            self.assertTrue(project.Project(self.home).holds('x'))

    def test_the_main_checkout_is_what_git_says(self):
        r = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=self.lane, env=self.env, check=True,
                           capture_output=True, text=True)
        self.assertEqual(project.main_checkout(self.lane), (self.lane / r.stdout.strip()).resolve().parent)
        self.assertIsNone(project.main_checkout(self.alpha))

    def test_a_bare_repository_has_no_main_checkout(self):
        self.git(self.home, 'clone', '-q', '--bare', str(self.alpha), 'bare.git')
        self.git(self.home / 'bare.git', 'worktree', 'add', '-q', str(self.home / 'w'), 'HEAD', '--detach')
        self.assertIsNone(project.main_checkout(self.home / 'w'))

    def test_paths_outside_do_not_belong(self):
        (self.alpha / 'out').symlink_to(self.beta)
        p = project.Project(self.alpha)
        for v in ('../beta/x', str(self.beta), str(self.home / 'alpha2'), 'out/x', '~/beta'):
            self.assertFalse(p.holds(v), v)
        for v in ('x', './a/../b', str(self.alpha / 'x')):
            self.assertTrue(p.holds(v), v)

    def test_a_worktree_keeps_its_own_section_or_state(self):
        with mock.patch.dict(os.environ, self.env):
            def root():
                return project.resolve(SECTION, STATE, self.lane).root
            (self.lane / STATE).mkdir(parents=True)
            self.assertEqual(root(), self.lane)
            self.section(self.alpha, SECTION)
            self.assertEqual(root(), self.alpha)
            self.section(self.lane, SECTION)
            self.assertEqual(root(), self.lane)

    def found(self, cwd, lane, **kw):
        with mock.patch.dict(os.environ, self.env):
            return project.resolve(SECTION, STATE, cwd, {'COORD_LANE': lane}, **kw).root

    def test_the_hand_over_wins_in_a_worktree_of_its_project(self):
        lane = f'{self.alpha.as_posix()}#w'
        self.section(self.alpha, SECTION)
        self.section(self.lane, SECTION)
        (self.lane / STATE).mkdir(parents=True)
        self.assertEqual(self.found(self.lane, lane), self.alpha)
        self.assertEqual(self.found(self.lane, ''), self.lane)
        self.assertEqual(self.found(self.lane, lane, init=True), self.lane)

    def test_the_hand_over_wins_in_the_lanes_repository_whatever_is_there(self):
        lane = f'{self.alpha.as_posix()}#w'
        self.section(self.alpha, SECTION)
        self.lane_record(self.alpha, w=self.beta)
        (self.beta / STATE).mkdir(parents=True)
        self.section(self.beta, SECTION)
        self.assertEqual(self.found(self.beta, lane), self.alpha)
        self.git(self.beta, 'worktree', 'add', '-q', str(self.home / 'beta-lane'))
        self.assertEqual(self.found(self.home / 'beta-lane', lane), self.alpha)
        self.assertEqual(self.found(self.beta, lane, init=True), self.beta)

    def test_the_hand_over_is_not_followed_anywhere_else(self):
        self.section(self.alpha, SECTION)
        self.section(self.alpha, 'coordinator', repos='.')
        self.assertEqual(self.found(self.beta, f'{self.alpha.as_posix()}#w'), self.beta)  # no record
        self.lane_record(self.alpha, w=self.alpha)
        self.assertEqual(self.found(self.beta, f'{self.alpha.as_posix()}#w'), self.beta)  # another repository
        self.lane_record(self.alpha, w=self.beta)
        self.assertEqual(self.found(self.beta, f'{self.alpha.as_posix()}#other'), self.beta)  # another lane
        (self.home / 'loose').mkdir()
        self.assertEqual(self.found(self.home / 'loose', f'{self.alpha.as_posix()}#w'), self.home / 'loose')
        for bad in ('', 'w', '#w', f'{self.home.as_posix()}/none#w', f'{self.home.as_posix()}#w'):
            self.assertEqual(self.found(self.beta, bad), self.beta, bad)

    def test_the_projects_repos_are_followed_with_and_without_a_record(self):
        self.section(self.alpha, SECTION)
        self.section(self.alpha, 'coordinator', repos='.,../beta')
        self.assertEqual(self.found(self.beta, f'{self.alpha.as_posix()}#usage-watch'), self.alpha)
        self.lane_record(self.alpha, w=self.alpha)
        self.assertEqual(self.found(self.beta, f'{self.alpha.as_posix()}#w'), self.alpha)

    def test_a_mod_the_coordinators_project_does_not_use_is_the_repositorys_own(self):
        with mock.patch.dict(os.environ, self.env):
            self.lane_record(self.alpha, w=self.beta)
            lane = {'COORD_LANE': f'{self.alpha.as_posix()}#w'}
            other = Path('.claude') / 'other'
            self.assertEqual(project.resolve('other', other, self.beta, lane).root, self.beta)
            (self.alpha / other).mkdir(parents=True)
            self.assertEqual(project.resolve('other', other, self.beta, lane).root, self.alpha)

    def test_a_worker_finds_what_its_coordinator_in_a_worktree_finds(self):
        with mock.patch.dict(os.environ, self.env):
            self.section(self.alpha, 'other')
            self.section(self.lane, 'coordinator')
            self.lane_record(self.lane, w=self.beta)
            other = Path('.claude') / 'other'
            lane = {'COORD_LANE': f'{self.lane.as_posix()}#w'}
            for section, state, root in (('other', other, self.alpha), ('coordinator', Path('.claude') / 'coord', self.lane)):
                self.assertEqual(project.resolve(section, state, self.lane, {}).root, root)
                for cwd in (self.beta, self.lane, self.alpha):
                    self.assertEqual(project.resolve(section, state, cwd, lane).root, root, (section, cwd))

    def test_a_bare_git_folder_beside_its_worktrees_has_no_main_checkout(self):
        (self.home / 'proj').mkdir()
        self.git(self.home, 'clone', '-q', '--bare', str(self.alpha), 'proj/.git')
        self.git(self.home / 'proj', '--git-dir', '.git', 'worktree', 'add', '-q', '--detach', 'w')
        self.assertIsNone(project.main_checkout(self.home / 'proj' / 'w'))

    def test_a_linked_claude_folder_is_the_projects_own(self):
        kept = self.top / 'dotfiles' / 'beta'
        kept.mkdir(parents=True)
        (self.beta / '.claude').symlink_to(kept)
        project.Project(self.beta).refuse_outside('x', [self.beta / '.claude' / 'a', self.beta / 'b'])
        with self.assertRaises(project.Refused):
            project.Project(self.beta).refuse_outside('x', [self.top / 'dotfiles' / 'a'])

    def test_a_folder_spelled_in_another_case_is_the_same(self):
        other = Path(str(self.beta).replace('beta', 'BETA'))
        if not other.exists():
            self.skipTest('a case-sensitive file system')
        self.assertTrue(project.under(other / 'x', self.beta))


class TaskPathsTest(Places):
    def where(self, cwd, **env):
        code, out, err = self.cli(cwd, 'where', '--json', **env)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def verbs(self):
        sub = next(a for a in task.build_parser()._actions if hasattr(a, 'choices') and isinstance(a.choices, dict))
        return list(sub.choices)

    def test_help_after_every_verb_changes_nothing(self):
        for verb in self.verbs():
            for flag in ('-h', '--help'):
                code, out, _ = self.unchanged(self.alpha, verb, flag, TASKS_DB=str(self.beta / 't.db'))
                self.assertEqual(code, 0, verb)
                self.assertIn('usage: task.py', out)

    def test_unknown_flags_are_errors(self):
        for args in (['init', '--no-role'], ['init', '--force'], ['list', '-x'], ['--nope', 'list']):
            self.assertEqual(self.unchanged(self.alpha, *args)[0], 2, args)

    def test_an_inherited_path_of_another_project_is_ignored(self):
        # e.g. TASKS_DB handed down from a session in alpha (a terminal or daemon started there)
        for key, var, default in (('dbPath', 'TASKS_DB', '.claude/tasks/tasks.db'), ('rolesFile', 'TASKS_ROLES', '.claude/tasks/roles.toml')):
            for value in (str(self.alpha / 'x.db'), '../alpha/x.db'):
                code, out, err = self.cli(self.beta, 'where', **{var: value})
                self.assertEqual(code, 0, (key, err))
                self.assertIn(str(self.beta / default), out)
                for part in ('ignoring ' + key, var, str(self.beta / '.claude' / 'claude-mods.json')):
                    self.assertIn(part, err)
        self.assertFalse(any(self.alpha.glob('x.db')))

    def test_the_workspace_file_beats_the_environment_and_the_map_applies_only_inside_its_path(self):
        (self.home / '.claude' / 'claude-mods.json').write_text(json.dumps({'workspaces': {
            str(self.beta): {'task-tracker': {'dbPath': 'mapped/t.db', 'idPrefix': 'BETA'}}}}))
        w = self.where(self.beta, TASKS_DB='env/t.db')
        self.assertEqual((w['db'], w['prefix']), (str(self.beta / 'mapped' / 't.db'), 'BETA'))
        self.assertEqual(self.where(self.alpha)['db'], str(self.alpha / '.claude/tasks/tasks.db'))
        self.section(self.beta, 'task-tracker', dbPath='own/t.db')
        w = self.where(self.beta, TASKS_DB='env/t.db')
        self.assertEqual((w['db'], w['prefix']), (str(self.beta / 'own' / 't.db'), 'BETA'))

    def test_environment_paths_inside_still_work_with_a_warning(self):
        code, out, err = self.cli(self.beta, 'where', TASKS_DB='x/t.db')
        self.assertEqual(code, 0)
        self.assertIn(str(self.beta / 'x' / 't.db'), out)
        self.assertIn('dbPath comes from a plugin option', err)
        self.assertEqual(self.where(self.beta, TASKS_DB='')['db'], str(self.beta / '.claude/tasks/tasks.db'))

    def old_option(self, name, **options):
        (self.home / '.claude' / 'settings.json').write_text(json.dumps({'pluginConfigs': {name: {'options': options}}}))

    def test_old_options_warn_inside_and_are_ignored_outside(self):
        for name in ('task-tracker', 'task-tracker@claude-mods'):
            self.old_option(name, dbPath='x/t.db')
            code, out, err = self.cli(self.beta, 'where')
            self.assertEqual(code, 0)
            self.assertIn(str(self.beta / 'x' / 't.db'), out)
            self.assertIn('dbPath comes from a plugin option', err)
            self.old_option(name, dbPath=str(self.alpha / 't.db'))
            code, out, err = self.cli(self.beta, 'where')
            self.assertEqual(code, 0)
            self.assertIn('ignoring dbPath', err)
            self.assertIn(str(self.beta / '.claude/tasks/tasks.db'), out)

    def test_an_importer_outside_stops_only_import(self):
        env = {'TASKS_IMPORTERS': str(self.alpha / 'x.py')}
        (self.alpha / 'x.py').write_text('def records(ctx): raise SystemError("ran")')
        for args in (['init'], ['new', 'a task'], ['list'], ['where']):
            self.assertEqual(self.cli(self.beta, *args, **env)[0], 0, args)
        code, _, err = self.unchanged(self.beta, 'import', **env)
        self.assertEqual(code, 2)
        self.assertIn('refusing the importer', err)

    def test_help_inside_a_comment_is_text(self):
        self.cli(self.beta, 'init')
        self.cli(self.beta, 'new', 'a task')
        self.assertEqual(self.cli(self.beta, 'comment', '1', 'run', 'ls', '-h')[0], 0)
        self.assertIn('run ls -h', self.cli(self.beta, 'show', '1')[1])
        self.assertIn('usage', self.unchanged(self.beta, 'comment', '1', '-h')[1])

    def test_init_lists_its_files_and_refuses_any_outside(self):
        code, out, _ = self.cli(self.beta, 'init')
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(f'init writes: {self.beta}'), out)
        self.section(self.alpha, 'task-tracker', rolesFile=str(self.beta / 'roles.toml'))
        self.assertEqual(self.unchanged(self.alpha, 'init')[0], 2)
        self.assertEqual(self.unchanged(self.alpha, '--db', str(self.beta / 't.db'), 'init', '--no-roles')[0], 2)

    def test_a_worktree_uses_the_main_checkouts_tracker(self):
        self.section(self.alpha, 'task-tracker', dbPath='tmp/t.db', idPrefix='AL')
        self.cli(self.alpha, 'init')
        self.assertEqual(self.where(self.lane)['db'], str(self.alpha / 'tmp' / 't.db'))
        self.assertIn('AL-1', self.cli(self.lane, 'new', 'from the lane')[1])
        self.assertIn('from the lane', self.cli(self.alpha, 'list')[1])

    def test_a_worker_uses_its_coordinators_project_but_never_inits_it(self):
        self.section(self.alpha, 'task-tracker', idPrefix='AL')
        self.lane_record(self.alpha, w=self.beta)
        lane = f'{self.alpha.as_posix()}#w'
        self.assertEqual(self.where(self.beta, COORD_LANE=lane)['prefix'], 'AL')
        self.assertEqual(self.where(self.beta)['prefix'], 'TASK')
        before = tree(self.alpha)
        self.assertEqual(self.cli(self.beta, 'init', COORD_LANE=lane)[0], 0)
        self.assertEqual(tree(self.alpha), before)


if __name__ == '__main__':
    unittest.main()
