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
import coord  # noqa: E402
import project  # noqa: E402

SECTION, STATE = 'coordinator', Path('.claude') / 'coord'


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
        r = subprocess.run([sys.executable, str(SCRIPTS / 'coord.py'), *args], cwd=cwd, env={**self.env, **env},
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


class CoordPathsTest(Places):
    def config(self, cwd, **env):
        code, out, err = self.cli(cwd, 'config', **env)
        return code, (json.loads(out) if code == 0 else {}), err

    def test_help_after_every_verb_changes_nothing(self):
        for verb in [*coord.VERBS, 'config']:
            for flag in ('-h', '--help'):
                code, out, _ = self.unchanged(self.alpha, verb, flag, COORD_BUS_DIR=str(self.beta / 'bus'))
                self.assertEqual(code, 0, verb)
                self.assertIn('coord.py', out)
        self.assertEqual(self.unchanged(self.alpha, 'spawn', 'w', '--dry-run', '--help')[0], 0)

    def test_unknown_flags_are_errors(self):
        for args in (['init', '--rolls', 'a'], ['init', '--force=1'], ['status', '-x'], ['--nope', 'lanes'],
                     ['spawn', 'w', '--repo'], ['lanes', '--force']):
            code, _, err = self.unchanged(self.alpha, *args)
            self.assertEqual(code, 2, args)
            self.assertIn('coordinator: ', err)

    def test_every_flag_in_the_usage_text_is_known(self):
        for verb, flags in coord.FLAGS.items():
            for f in flags:
                self.assertIsNone(coord.flag_error(verb, [f.rstrip('=')] + (['v'] if f.endswith('=') else [])))

    def test_environment_paths_outside_the_project_are_refused(self):
        for key in (*coord.PATHS, 'repos'):
            var = coord.env_key(key)
            for value in (str(self.alpha / 'x'), '../alpha/x'):
                code, _, err = self.unchanged(self.beta, 'init', **{var: value})
                self.assertEqual(code, 2, key)
                for part in (key, var, str(self.beta / '.claude' / 'claude-mods.json')):
                    self.assertIn(part, err)
        self.assertEqual(self.config(self.beta, COORD_REPOS=f'.,{self.alpha}')[0], 2)

    def test_the_context_hook_says_why(self):
        code, out, _ = self.unchanged(self.beta, 'context', COORD_CARDS_DIR=str(self.alpha / 'x'))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)['configured'])
        self.assertIn('cardsDir', json.loads(out)['refused'])

    def test_environment_paths_inside_still_work_with_a_warning(self):
        code, cfg, err = self.config(self.beta, COORD_BUS_DIR='tmp/bus')
        self.assertEqual((code, cfg['busDir']), (0, 'tmp/bus'))
        self.assertIn('busDir comes from a plugin option', err)
        self.assertEqual(self.config(self.beta, COORD_BUS_DIR='.claude/bus')[2], '')

    def test_the_project_file_may_point_anywhere(self):
        self.section(self.beta, 'coordinator', busDir=str(self.alpha / 'bus'))
        code, cfg, err = self.config(self.beta, COORD_BUS_DIR=str(self.home / 'elsewhere'))
        self.assertEqual((code, cfg['busDir'], err), (0, str(self.alpha / 'bus'), ''))

    def test_a_worktree_uses_the_main_checkouts_config(self):
        self.section(self.alpha, 'coordinator', cardsDir='tmp/cards', maxLanes=3)
        self.assertEqual(self.config(self.lane)[1]['maxLanes'], 3)
        self.assertEqual(self.config(self.lane, COORD_BUS_DIR=str(self.alpha / 'bus'))[0], 0)
        self.cli(self.lane, 'ask', 'ok?')
        self.assertTrue((self.alpha / '.claude/coord/reports/questions.jsonl').exists())
        self.assertFalse((self.lane / '.claude').exists())

    def test_a_worker_uses_its_coordinators_project(self):
        self.section(self.alpha, 'coordinator', maxLanes=3)
        lane = coord.lane_key(coord.Config(self.alpha), 'w')
        self.assertEqual(self.config(self.beta, COORD_LANE=lane)[1]['maxLanes'], 3)
        self.assertEqual(self.config(self.beta)[1]['maxLanes'], 6)

    def test_init_lists_its_files_first(self):
        code, out, _ = self.cli(self.beta, 'init', AGENT_BUS_CLI=os.devnull)
        self.assertEqual(code, 0)
        self.assertIn(f'init writes:\n  {self.beta}', out)
        self.assertFalse((self.alpha / '.claude').exists())

    def test_init_refuses_files_outside_even_from_the_project_file(self):
        self.section(self.beta, 'coordinator', cardsDir=str(self.alpha / 'cards'))
        code, _, err = self.unchanged(self.beta, 'init')
        self.assertNotEqual(code, 0)
        self.assertIn(str(self.alpha / 'cards'), err)

    def test_a_worker_never_inits_its_coordinators_project(self):
        self.section(self.alpha, 'coordinator', maxLanes=3)
        before = tree(self.alpha)
        self.cli(self.beta, 'init', COORD_LANE=f'{self.alpha}#w')
        self.assertEqual(tree(self.alpha), before)
        self.assertTrue((self.beta / '.claude/claude-mods.json').exists())


if __name__ == '__main__':
    unittest.main()
