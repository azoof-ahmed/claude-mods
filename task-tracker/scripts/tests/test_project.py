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
        (root / '.claude' / 'claude-mods.json').write_text(json.dumps({name: keys}), encoding='utf-8')

    def cli(self, cwd, *args, **env):
        r = subprocess.run([sys.executable, str(SCRIPTS / 'task.py'), *args], cwd=cwd, env={**self.env, **env},
                           capture_output=True, text=True, encoding='utf-8', stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr

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

    def test_the_hand_over_names_a_project(self):
        lane = {'COORD_LANE': f'{self.alpha.as_posix()}#w'}
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(project.resolve(SECTION, STATE, self.beta, lane).root, self.alpha)
            self.assertEqual(project.resolve(SECTION, STATE, self.beta, lane, init=True).root, self.beta)
            for bad in ('', 'w', '#w', f'{self.home.as_posix()}/none#w', f'{self.home.as_posix()}#w'):
                self.assertEqual(project.resolve(SECTION, STATE, self.beta, {'COORD_LANE': bad}).root, self.beta, bad)
            self.section(self.beta, SECTION)
            self.assertEqual(project.resolve(SECTION, STATE, self.beta, lane).root, self.beta)


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

    def test_environment_paths_outside_the_project_are_refused(self):
        for key, var in (('dbPath', 'TASKS_DB'), ('rolesFile', 'TASKS_ROLES'), ('importers', 'TASKS_IMPORTERS')):
            for value in (str(self.alpha / 'x.py'), '../alpha/x.py'):
                for verb in (['init'], ['list'], ['where']):
                    code, _, err = self.unchanged(self.beta, *verb, **{var: value})
                    self.assertEqual(code, 2, (key, verb))
                    for part in (key, var, str(self.beta / '.claude' / 'claude-mods.json')):
                        self.assertIn(part, err)

    def test_environment_paths_inside_still_work_with_a_warning(self):
        code, out, err = self.cli(self.beta, 'where', TASKS_DB='x/t.db')
        self.assertEqual(code, 0)
        self.assertIn(str(self.beta / 'x' / 't.db'), out)
        self.assertIn('dbPath comes from a plugin option', err)
        self.assertEqual(self.where(self.beta, TASKS_DB='')['db'], str(self.beta / '.claude/tasks/tasks.db'))

    def old_option(self, name, **options):
        (self.home / '.claude' / 'settings.json').write_text(json.dumps({'pluginConfigs': {name: {'options': options}}}))

    def test_old_options_warn_inside_and_are_refused_outside(self):
        for name in ('task-tracker', 'task-tracker@claude-mods'):
            self.old_option(name, dbPath='x/t.db')
            code, out, err = self.cli(self.beta, 'where')
            self.assertEqual(code, 0)
            self.assertIn(str(self.beta / 'x' / 't.db'), out)
            self.assertIn('dbPath comes from a plugin option', err)
            self.old_option(name, dbPath=str(self.alpha / 't.db'))
            code, _, err = self.unchanged(self.beta, 'init')
            self.assertEqual(code, 2)
            self.assertIn('refusing dbPath', err)

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
        lane = f'{self.alpha.as_posix()}#w'
        self.assertEqual(self.where(self.beta, COORD_LANE=lane)['prefix'], 'AL')
        self.assertEqual(self.where(self.beta)['prefix'], 'TASK')
        before = tree(self.alpha)
        self.assertEqual(self.cli(self.beta, 'init', COORD_LANE=lane)[0], 0)
        self.assertEqual(tree(self.alpha), before)


if __name__ == '__main__':
    unittest.main()
