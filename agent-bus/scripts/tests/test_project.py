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
import bus  # noqa: E402
import project  # noqa: E402

SECTION, STATE = 'agent-bus', Path('.claude') / 'bus'


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
        r = subprocess.run([sys.executable, str(SCRIPTS / 'bus.py'), *args], cwd=cwd, env={**self.env, **env},
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


class BusPathsTest(Places):
    def cli(self, cwd, *args, **env):
        return super().cli(cwd, *args, **{'AGENT_BUS_PIFORGE_SYNC': 'off', 'AGENT_BUS_PAUSE_PIFORGE': 'off', **env})

    def hook(self, cwd, **env):
        code, out, err = self.cli(cwd, 'hook', '--event', 'SessionStart', AGENT_BUS_GLOBAL='off', **env)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_help_after_every_verb_changes_nothing(self):
        for verb in ('init', 'send', 'digest', 'read', 'watch', 'watch-live', 'whoami', 'hook', 'deliver', 'chat', 'tui',
                     'snapshot', 'broadcast', 'global', 'register', 'who', 'status', 'ack', 'edit', 'delete'):
            for flag in ('-h', '--help'):
                code, out, _ = self.unchanged(self.alpha, verb, flag, AGENT_BUS_DIR=str(self.beta / 'bus'))
                self.assertEqual(code, 0, verb)
                self.assertIn('bus.py', out)
        self.assertEqual(self.unchanged(self.alpha, 'broadcast', '--pause', '--help')[0], 0)

    def test_unknown_flags_are_errors(self):
        self.assertEqual(self.cli(self.alpha, 'init')[0], 0)
        for args in (['init', '--rolls', 'a'], ['broadcast', '--urgent', 'x'], ['watch', '--as', 'a', '--x'],
                     ['hook', '--event'], ['send', 'coordinator', 'x', '--from', 'a', '--nope'], ['--nope', 'who'],
                     ['deliver', '--nope'], ['digest', '--as', 'a', '-x']):
            code, _, err = self.unchanged(self.alpha, *args)
            self.assertEqual(code, 2, args)
            self.assertTrue(err)

    def test_an_inherited_bus_dir_of_another_project_is_ignored(self):
        # a terminal or daemon started from alpha's session hands its env to a session in beta
        self.cli(self.alpha, 'init')
        self.cli(self.beta, 'init', '--roles', 'x')
        alpha_bus = tree(self.alpha)
        for value in (str(self.alpha / '.claude' / 'bus'), '../alpha/.claude/bus'):
            code, _, err = self.cli(self.beta, 'send', 'x', 'hi', '--from', 'x', AGENT_BUS_DIR=value)
            self.assertEqual(code, 0, err)
            for part in ('ignoring busDir', 'AGENT_BUS_DIR', str(self.beta / '.claude' / 'claude-mods.json')):
                self.assertIn(part, err)
        self.assertEqual(tree(self.alpha), alpha_bus)
        self.assertIn('hi', self.cli(self.beta, 'digest', '--as', 'x')[1])

    def test_the_workspace_file_beats_the_environment(self):
        self.section(self.beta, 'agent-bus', busDir='tmp/bus')
        code, out, _ = self.cli(self.beta, 'init', AGENT_BUS_DIR=str(self.beta / 'other'))
        self.assertEqual(code, 0)
        self.assertIn(f'init writes: {self.beta / "tmp" / "bus"}', out)

    def test_the_per_workspace_map_applies_only_inside_its_path(self):
        (self.home / '.claude' / 'claude-mods.json').write_text(json.dumps({'workspaces': {
            str(self.beta): {'agent-bus': {'busDir': 'tmp/mapped'}},
            str(self.home / 'al*'): {'agent-bus': {'busDir': 'tmp/globbed'}},
        }}))
        self.assertIn(f'init writes: {self.beta / "tmp" / "mapped"}', self.cli(self.beta, 'init')[1])
        self.assertIn(f'init writes: {self.alpha / "tmp" / "globbed"}', self.cli(self.alpha, 'init')[1])
        (self.home / 'gamma').mkdir()
        self.git(self.home / 'gamma', 'init', '-q')
        self.assertIn(f'init writes: {self.home / "gamma" / ".claude" / "bus"}', self.cli(self.home / 'gamma', 'init')[1])
        # the workspace's own file still wins over the map, and the map over this session's options
        self.section(self.beta, 'agent-bus', busDir='tmp/own')
        self.assertIn(f'init writes: {self.beta / "tmp" / "own"}', self.cli(self.beta, 'init', AGENT_BUS_DIR='x')[1])
        self.assertIn(f'init writes: {self.alpha / "tmp" / "globbed"}', self.cli(self.alpha, 'init', AGENT_BUS_DIR='x')[1])

    def test_an_inherited_lane_role_of_another_project_is_ignored(self):
        lane = {'COORD_LANE': f'{self.alpha}#worker1', 'AGENT_BUS_ROLE': 'worker1'}
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(project.env_role(self.beta, lane), None)
            self.assertEqual(project.env_role(self.lane, lane), 'worker1')
            self.assertEqual(project.env_role(self.beta, {'AGENT_BUS_ROLE': 'me'}), 'me')

    def test_the_hook_ignores_another_projects_bus(self):
        self.cli(self.alpha, 'init')
        before = tree(self.alpha)
        out = self.hook(self.beta, AGENT_BUS_DIR=str(self.alpha / '.claude' / 'bus'), AGENT_BUS_ROLE='coordinator')
        self.assertEqual((out['bus'], tree(self.alpha)), (False, before))

    def test_an_ignored_bus_leaves_the_global_channel_on(self):
        env = {'AGENT_BUS_DIR': str(self.alpha / 'bus'), 'AGENT_BUS_GLOBAL_DIR': str(self.top / 'global')}
        self.assertEqual(self.cli(self.beta, 'broadcast', '--pause', 'too', 'hot', '--no-piforge', '--from', 'a', **env)[0], 0)
        self.assertIn('too hot', self.cli(self.beta, 'global', 'status', **env)[1])
        out = json.loads(self.cli(self.beta, 'hook', '--event', 'SessionStart', '--session', 's', **env)[1])
        self.assertIn('too hot', out['text'])
        self.assertEqual(out['bus'], False)
        self.assertFalse((self.alpha / 'bus').exists())

    def test_the_note_says_what_a_running_lane_can_do(self):
        err = self.cli(self.beta, 'who', AGENT_BUS_DIR=str(self.alpha / 'bus'))[2]
        self.assertIn(f'--dir {self.alpha / "bus"}', err)

    def test_help_inside_free_text_is_text(self):
        env = {'AGENT_BUS_GLOBAL_DIR': str(self.top / 'global')}
        self.assertEqual(self.cli(self.alpha, 'broadcast', 'run', 'ls', '-h', '--from', 'a', **env)[0], 0)
        self.assertIn('run ls -h', self.cli(self.alpha, 'global', 'log', **env)[1])
        for words in (['stop', '--too', 'x'], ['--from', 'a', 'stop', '--puase'], ['stop', '--to']):
            code, _, err = self.unchanged(self.alpha, 'broadcast', *words, **env)
            self.assertEqual(code, 2, words)
            self.assertIn('quote the text', err)
        self.assertEqual(self.cli(self.alpha, 'broadcast', '--from', 'a', '--', 'use', '--force', '--to', **env)[0], 0)
        self.assertEqual(self.cli(self.alpha, 'broadcast', 'use --quoted words', '--from', 'a', **env)[0], 0)
        log = self.cli(self.alpha, 'global', 'log', **env)[1]
        self.assertIn('use --force --to', log)
        self.assertIn('use --quoted words', log)
        self.cli(self.alpha, 'init', '--roles', 'a,b')
        code, _, err = self.cli(self.alpha, 'send', 'a', 'run', 'ls', '-h', '--from', 'b')
        self.assertEqual(code, 2)
        self.assertIn('quote the text', err)
        self.assertEqual(self.cli(self.alpha, 'send', '--from', 'b', '--', 'a', '--help')[0], 0)
        self.assertIn('--help', self.cli(self.alpha, 'digest', '--as', 'a')[1])

    def test_the_terminal_clients_launcher_starts(self):
        r = subprocess.run([sys.executable, str(SCRIPTS.parent / 'tui' / 'bus-tui'), '--help'], env=self.env,
                           capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--dir', r.stdout)

    def test_a_tasks_db_outside_is_ignored_with_a_message(self):
        self.cli(self.beta, 'init')
        code, _, err = self.cli(self.beta, 'whoami', '--as', 'x', AGENT_BUS_TASKS_DB=str(self.alpha / 't.db'))
        self.assertEqual(code, 0)
        self.assertIn('ignoring tasksDb', err)

    def test_environment_paths_inside_still_work_with_a_warning(self):
        code, out, err = self.cli(self.beta, 'init', AGENT_BUS_DIR='tmp/bus')
        self.assertEqual(code, 0)
        self.assertIn(f'init writes: {self.beta / "tmp" / "bus"}', out)
        self.assertIn('busDir comes from a plugin option', err)

    def old_option(self, name, **options):
        (self.home / '.claude' / 'settings.json').write_text(json.dumps({'pluginConfigs': {name: {'options': options}}}))

    def test_old_options_warn_inside_and_are_ignored_outside(self):
        for name in ('agent-bus', 'agent-bus@claude-mods'):
            self.old_option(name, busDir='tmp/bus')
            code, _, err = self.cli(self.beta, 'who')
            self.assertIn('busDir comes from a plugin option', err)
            self.assertIn(str(self.beta / 'tmp' / 'bus'), err)
            self.old_option(name, busDir=str(self.alpha / 'bus'))
            code, out, err = self.cli(self.beta, 'init')
            self.assertEqual(code, 0)
            self.assertIn('ignoring busDir', err)
            self.assertIn(f'init writes: {self.beta / ".claude" / "bus"}', out)

    def test_the_terminal_client_finds_the_same_bus(self):
        def find(cwd, **env):
            code = 'import sys; sys.path.insert(0, sys.argv[1]); from bus_tui.app import find_bus_dir; print(find_bus_dir())'
            return subprocess.run([sys.executable, '-c', code, str(SCRIPTS.parent / 'tui')], cwd=cwd,
                                  env={**self.env, **env}, capture_output=True, text=True, encoding='utf-8')
        self.section(self.alpha, 'agent-bus', busDir='tmp/bus')
        for cwd in (self.alpha, self.lane):
            self.assertEqual(find(cwd).stdout.strip(), str(self.alpha / 'tmp' / 'bus'))
        self.assertEqual(find(self.beta).stdout.strip(), str(self.beta / '.claude' / 'bus'))
        r = find(self.beta, AGENT_BUS_DIR=str(self.alpha / 'tmp' / 'bus'))
        self.assertEqual(r.stdout.strip(), str(self.beta / '.claude' / 'bus'))
        self.assertIn('ignoring busDir', r.stderr)

    def test_init_refuses_a_folder_outside(self):
        for args in (['--dir', str(self.alpha / 'bus'), 'init'], ['init', '--dir', '../alpha/bus']):
            self.assertEqual(self.unchanged(self.beta, *args)[0], 2)
        self.section(self.beta, 'agent-bus', busDir=str(self.alpha / 'bus'))
        self.assertEqual(self.unchanged(self.beta, 'init')[0], 2)

    def test_a_worktree_uses_the_main_checkouts_bus(self):
        self.section(self.alpha, 'agent-bus', busDir='tmp/bus')
        self.cli(self.alpha, 'init', '--roles', 'coordinator,w')
        self.assertEqual(self.cli(self.lane, 'send', 'coordinator', 'hello', '--from', 'w')[0], 0)
        self.assertIn('hello', self.cli(self.alpha, 'digest', '--as', 'coordinator')[1])
        self.assertTrue(self.hook(self.lane, AGENT_BUS_ROLE='w')['bus'])
        self.assertFalse((self.lane / 'tmp').exists())

    def test_a_worktree_finds_a_bus_made_without_a_section(self):
        self.cli(self.alpha, 'init')
        self.assertTrue(self.hook(self.lane, AGENT_BUS_ROLE='w')['bus'])

    def test_a_worker_in_another_repository_uses_its_coordinators_bus(self):
        self.cli(self.alpha, 'init', '--roles', 'coordinator,w')
        self.lane_record(self.alpha, w=self.beta)
        lane = f'{self.alpha.as_posix()}#w'
        self.assertFalse(self.hook(self.beta, AGENT_BUS_ROLE='w')['bus'])
        self.assertTrue(self.hook(self.beta, AGENT_BUS_ROLE='w', COORD_LANE=lane)['bus'])
        before = tree(self.alpha)
        self.assertEqual(self.cli(self.beta, 'init', COORD_LANE=lane)[0], 0)  # its own, not the coordinator's
        self.assertEqual(tree(self.alpha), before)
        self.assertTrue((self.beta / '.claude' / 'bus').is_dir())

    def test_lanes_are_read_only_from_this_project(self):
        (self.alpha / 'cards').mkdir()
        (self.alpha / 'cards' / 'lanes.json').write_text('{"w": {"state": "live"}}')
        with mock.patch.dict(os.environ, {**self.env, 'COORD_CARDS_DIR': str(self.alpha / 'cards')}):
            self.assertIsNone(bus.coord_lanes(self.beta))
            self.assertIn('w', bus.coord_lanes(self.alpha))


if __name__ == '__main__':
    unittest.main()
