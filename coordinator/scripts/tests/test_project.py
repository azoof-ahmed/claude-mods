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
        f = root / '.claude' / 'claude-mods.json'
        data = json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
        f.write_text(json.dumps({**data, name: keys}), encoding='utf-8')

    def cli(self, cwd, *args, **env):
        r = subprocess.run([sys.executable, str(SCRIPTS / 'coord.py'), *args], cwd=cwd, env={**self.env, **env},
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

    def test_an_inherited_path_of_another_project_is_ignored(self):
        for key in (*coord.PATHS, 'repos'):
            var = coord.env_key(key)
            for value in (str(self.alpha / 'x'), '../alpha/x'):
                code, cfg, err = self.config(self.beta, **{var: value})
                self.assertEqual((code, cfg[key]), (0, coord.DEFAULTS[key]), key)
                for part in ('ignoring ' + key, var, str(self.beta / '.claude' / 'claude-mods.json')):
                    self.assertIn(part, err)
        code, cfg, _ = self.config(self.beta, COORD_REPOS=f'.,{self.alpha}')
        self.assertEqual((code, cfg['repos']), (0, '.'))

    def test_the_per_workspace_map_applies_only_inside_its_path(self):
        (self.home / '.claude' / 'claude-mods.json').write_text(json.dumps({'workspaces': {
            str(self.home / 'b*'): {'coordinator': {'maxLanes': 2}},
            str(self.beta): {'coordinator': {'maxLanes': 5, 'cardsDir': 'mapped'}}}}))
        cfg = self.config(self.beta, COORD_MAX_LANES='9')[1]
        self.assertEqual((cfg['maxLanes'], cfg['cardsDir']), (5, 'mapped'))
        self.assertEqual(self.config(self.alpha, COORD_MAX_LANES='9')[1]['maxLanes'], 9)
        self.section(self.beta, 'coordinator', maxLanes=1)
        self.assertEqual(self.config(self.beta)[1]['maxLanes'], 1)

    def test_the_context_hook_ignores_another_projects_path(self):
        code, out, err = self.unchanged(self.beta, 'context', COORD_CARDS_DIR=str(self.alpha / 'x'))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)['configured'])
        self.assertIn('ignoring cardsDir', err)

    def test_environment_paths_inside_still_work_with_a_warning(self):
        code, cfg, err = self.config(self.beta, COORD_BUS_DIR='tmp/bus')
        self.assertEqual((code, cfg['busDir']), (0, 'tmp/bus'))
        self.assertIn('busDir comes from a plugin option', err)
        self.assertEqual(self.config(self.beta, COORD_BUS_DIR='.claude/bus')[2], '')

    def old_option(self, name, **options):
        (self.home / '.claude' / 'settings.json').write_text(json.dumps({'pluginConfigs': {name: {'options': options}}}))

    def test_old_options_warn_inside_and_are_ignored_outside(self):
        for name in ('coordinator', 'coordinator@claude-mods'):
            self.old_option(name, busDir='tmp/bus')
            code, cfg, err = self.config(self.beta)
            self.assertEqual((code, cfg['busDir']), (0, 'tmp/bus'))
            self.assertIn('busDir comes from a plugin option', err)
            self.old_option(name, cardsDir=str(self.alpha / 'cards'), maxLanes=1)
            code, cfg, err = self.config(self.beta)
            self.assertEqual((code, cfg['cardsDir']), (0, coord.DEFAULTS['cardsDir']))
            self.assertIn('ignoring cardsDir', err)
        self.section(self.beta, 'coordinator', cardsDir='mine')
        self.assertEqual(self.config(self.beta)[1]['cardsDir'], 'mine')
        self.old_option('another', cardsDir=str(self.alpha / 'cards'))
        self.assertEqual(self.config(self.alpha)[2], '')

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
        self.lane_record(self.alpha, w=self.beta)
        lane = coord.lane_key(coord.Config(self.alpha), 'w')
        self.assertEqual(self.config(self.beta, COORD_LANE=lane)[1]['maxLanes'], 3)
        self.assertEqual(self.config(self.beta)[1]['maxLanes'], 6)

    def test_a_worker_elsewhere_gets_absolute_paths(self):
        self.section(self.alpha, 'coordinator', maxLanes=3)
        self.lane_record(self.alpha, w=self.beta, v=self.lane)
        for cwd, role in ((self.beta, 'w'), (self.lane, 'v')):
            env = {'COORD_LANE': f'{self.alpha.as_posix()}#{role}', 'AGENT_BUS_ROLE': role}
            ctx = json.loads(self.cli(cwd, 'context', **env)[1])
            self.assertEqual(ctx['card'], (self.alpha / f'.claude/coord/lanes/{role}.md').as_posix())
            self.assertEqual(ctx['rules'], (self.alpha / '.claude/coord/RULES_CORE.md').as_posix())
            self.assertEqual(self.cli(cwd, 'ask', f'from {role}', **env)[0], 0)
            self.assertFalse((cwd / '.claude').exists())
        out = self.cli(self.alpha, 'questions')[1]
        self.assertIn('from w', out)
        self.assertIn('from v', out)
        ctx = json.loads(self.cli(self.alpha, 'context', AGENT_BUS_ROLE='w')[1])
        self.assertEqual(ctx['card'], '.claude/coord/lanes/w.md')

    def test_a_committed_section_does_not_detach_a_worktree_worker(self):
        self.section(self.alpha, 'coordinator', maxLanes=3)
        self.section(self.lane, 'coordinator', maxLanes=3)
        self.cli(self.lane, 'ask', 'to the coordinator', COORD_LANE=f'{self.alpha.as_posix()}#w')
        self.assertIn('to the coordinator', self.cli(self.alpha, 'questions')[1])

    def test_spawn_elsewhere_names_the_brief_absolutely(self):
        self.cli(self.alpha, 'init', AGENT_BUS_CLI=os.devnull)
        brief = (self.alpha / '.claude/coord/briefs/RESUME.md').as_posix()
        for repo in (self.beta, self.lane):
            out = self.cli(self.alpha, 'spawn', 'w', '--repo', str(repo), '--dry-run')[1]
            self.assertIn(f'Read {brief} ', out)
        out = self.cli(self.alpha, 'spawn', 'w', '--dry-run')[1]
        self.assertIn('Read .claude/coord/briefs/RESUME.md ', out)

    def test_help_inside_free_text_is_text(self):
        self.cli(self.alpha, 'ask', 'which?')
        qid = self.cli(self.alpha, 'questions')[1].split()[0]
        code, _, err = self.cli(self.alpha, 'answer', qid, 'run', 'ls', '-h', '--force', '--repos', 'x')
        self.assertEqual(code, 0, err)
        self.assertIn('run ls -h --force --repos x', self.cli(self.alpha, 'questions', '--all')[1])
        self.assertEqual(self.cli(self.alpha, 'ask', 'is', '--help', 'fine?', '--from', 'w', '--options', 'a|b')[0], 0)
        out = self.cli(self.alpha, 'questions')[1]
        self.assertIn('(w,', out)
        self.assertIn('is --help fine? [a|b]', out)
        for args in (['answer', '-h'], ['answer', qid, '--help'], ['ask', '--from', 'w', '-h']):
            self.assertIn('coord.py', self.unchanged(self.alpha, *args)[1])

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
        self.lane_record(self.alpha, w=self.beta)
        before = tree(self.alpha)
        self.cli(self.beta, 'init', COORD_LANE=f'{self.alpha}#w')
        self.assertEqual(tree(self.alpha), before)
        self.assertTrue((self.beta / '.claude/claude-mods.json').exists())


if __name__ == '__main__':
    unittest.main()
