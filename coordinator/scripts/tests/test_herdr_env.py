import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import coord  # noqa: E402
import herdr_env as h  # noqa: E402

PANE = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'wE:p1', 'HERDR_SESSION': 'default', 'HERDR_SOCKET_PATH': '/s/herdr.sock'}


def done(out, code=0):
    return subprocess.CompletedProcess([], code, out, '')


class RecoverTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(h.shutil, 'which', lambda n: None)  # no hs, tmux or herdr on PATH unless a test says
        p.start()
        self.addCleanup(p.stop)

    def test_hs_locate_comes_first(self):
        env = {'ORCA_TERMINAL_HANDLE': 'term_old'}
        hs = lambda e, c: ({'HERDR_ENV': '1', 'HERDR_PANE_ID': 'w1:p8', 'HERDR_SOCKET_PATH': '/b.sock'}, 'agent')  # noqa
        got = h.recover(env, orca_up=lambda: False, hs=hs, tmux=lambda e: self.fail('asked tmux'))
        self.assertEqual(got, 'hs:agent')
        self.assertEqual((env['HERDR_PANE_ID'], env['HERDR_SOCKET_PATH']), ('w1:p8', '/b.sock'))
        self.assertNotIn('ORCA_TERMINAL_HANDLE', env)

    def test_hs_guess_does_not_override_a_running_orca(self):
        hs = lambda e, c: ({'HERDR_ENV': '1', 'HERDR_SOCKET_PATH': '/a.sock'}, 'cwd')  # noqa: E731
        env = {'ORCA_TERMINAL_HANDLE': 'term_a'}
        self.assertIsNone(h.recover(env, orca_up=lambda: True, hs=hs))
        self.assertEqual(env, {'ORCA_TERMINAL_HANDLE': 'term_a'})
        tm = lambda e, c: ({'HERDR_ENV': '1', 'HERDR_PANE_ID': 'w1:p1'}, 'tmux')  # noqa: E731
        self.assertEqual(h.recover(env, orca_up=lambda: True, hs=tm), 'hs:tmux')  # the pane is certain

    def test_from_hs_reads_its_json(self):
        out = '{"pane_id": "wE:p1", "session": "default", "socket_path": "/s.sock", "source": "tmux"}'
        with mock.patch.object(h.shutil, 'which', lambda n: '/u/hs' if n == 'hs' else None), \
                mock.patch.object(h, 'run', lambda argv, env=None, timeout=5: done(out)):
            ctx, src = h.from_hs({}, '/repo')
        self.assertEqual(src, 'tmux')
        self.assertEqual(ctx, {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'wE:p1', 'HERDR_SESSION': 'default',
                               'HERDR_SOCKET_PATH': '/s.sock'})
        with mock.patch.object(h.shutil, 'which', lambda n: '/u/hs'), \
                mock.patch.object(h, 'run', lambda argv, env=None, timeout=5: done('{}', 1)):
            self.assertEqual(h.from_hs({}, '/repo'), ({}, None))

    def test_a_pane_env_is_left_alone(self):
        env = {**PANE, 'ORCA_TERMINAL_HANDLE': 'term_a'}
        self.assertEqual(h.recover(env, tmux=lambda e: self.fail('asked tmux')), 'env')
        self.assertEqual(env['ORCA_TERMINAL_HANDLE'], 'term_a')

    def test_tmux_client_env_replaces_a_leftover_orca_handle(self):
        env = {'TMUX': '/tmp/t,6068,53', 'ORCA_TERMINAL_HANDLE': 'term_old', 'HERDR_TAB_ID': 'stale'}
        self.assertEqual(h.recover(env, tmux=lambda e: dict(PANE)), 'tmux')
        self.assertEqual(env['HERDR_PANE_ID'], 'wE:p1')
        self.assertNotIn('ORCA_TERMINAL_HANDLE', env)
        self.assertNotIn('HERDR_TAB_ID', env)  # nothing kept from a stale set
        self.assertEqual(coord.detect_backend(env), 'herdr')

    def test_a_running_orca_keeps_its_terminal(self):
        env = {'ORCA_TERMINAL_HANDLE': 'term_a'}
        got = h.recover(env, orca_up=lambda: True, tmux=lambda e: {},
                        sessions=lambda e, c: self.fail('asked herdr'))
        self.assertIsNone(got)
        self.assertEqual(env, {'ORCA_TERMINAL_HANDLE': 'term_a'})

    def test_orca_down_falls_to_the_herdr_sessions(self):
        env = {'ORCA_TERMINAL_HANDLE': 'term_a'}
        ctx = {'HERDR_ENV': '1', 'HERDR_SOCKET_PATH': '/s/herdr.sock', 'HERDR_WORKSPACE_ID': 'wE'}
        self.assertEqual(h.recover(env, orca_up=lambda: False, tmux=lambda e: {}, sessions=lambda e, c: ctx),
                         'session')
        self.assertEqual(coord.detect_backend(env), 'herdr')  # no pane id, but the session's socket
        self.assertNotIn('ORCA_TERMINAL_HANDLE', env)

    def test_nothing_found_changes_nothing(self):
        env = {'TMUX': '/tmp/t,1,0'}
        self.assertIsNone(h.recover(env, tmux=lambda e: {}, sessions=lambda e, c: {}))
        self.assertEqual(env, {'TMUX': '/tmp/t,1,0'})
        self.assertIsNone(h.recover({'HERDR_DISCOVER': '0'}, tmux=lambda e: self.fail('asked tmux')))


class SessionsTest(unittest.TestCase):
    SESSIONS = [{'name': 'default', 'socket_path': '/a.sock'}, {'name': 'clarity', 'socket_path': '/b.sock'}]

    def panes(self, by_sock):
        return lambda sock: by_sock.get(sock, [])

    def test_the_pane_herdr_reports_for_this_claude_session(self):
        panes = {'/b.sock': [{'pane_id': 'w1:p8', 'tab_id': 'w1:t8', 'workspace_id': 'w1', 'cwd': '/x',
                              'agent_session': {'value': 'sess-1'}}]}
        got = h.from_sessions({'CLAUDE_CODE_SESSION_ID': 'sess-1'}, '/elsewhere', self.SESSIONS, self.panes(panes))
        self.assertEqual((got['HERDR_PANE_ID'], got['HERDR_SOCKET_PATH'], got['HERDR_SESSION']),
                         ('w1:p8', '/b.sock', 'clarity'))

    def test_the_one_session_with_a_pane_in_the_project(self):
        with mock.patch.object(h, 'same_path', lambda a, b: a == b):
            panes = {'/a.sock': [{'pane_id': 'wE:p1', 'workspace_id': 'wE', 'cwd': '/repo'},
                                 {'pane_id': 'wE:p2', 'workspace_id': 'wE', 'foreground_cwd': '/repo'}],
                     '/b.sock': [{'pane_id': 'w1:p1', 'workspace_id': 'w1', 'cwd': '/other'}]}
            got = h.from_sessions({}, '/repo', self.SESSIONS, self.panes(panes))
            self.assertEqual(got, {'HERDR_ENV': '1', 'HERDR_SESSION': 'default', 'HERDR_SOCKET_PATH': '/a.sock',
                                   'HERDR_WORKSPACE_ID': 'wE'})
            panes['/b.sock'].append({'pane_id': 'w1:p2', 'workspace_id': 'w1', 'cwd': '/repo'})
            self.assertEqual(h.from_sessions({}, '/repo', self.SESSIONS, self.panes(panes)), {})  # ambiguous


class TmuxTest(unittest.TestCase):
    def fake(self, server_pid='6068'):
        def run(argv, env=None, timeout=5):
            if argv[1] == 'display-message':
                return done(f'{server_pid}\t$7\n')
            if argv[1] == 'list-clients':
                self.assertEqual(argv[3], '$7')
                return done('100\t11\n300\t33\n200\t22\n')
            raise AssertionError(argv)
        return run

    def test_the_most_recent_herdr_client(self):
        envs = {'33': {}, '22': dict(PANE), '11': {**PANE, 'HERDR_PANE_ID': 'old'}}
        with mock.patch.object(h, 'run', self.fake()), mock.patch.object(h.shutil, 'which', lambda n: '/x/tmux'):
            got = h.from_tmux({'TMUX': '/tmp/t,6068,53', 'TMUX_PANE': '%53'}, read_env=envs.get)
        self.assertEqual(got['HERDR_PANE_ID'], 'wE:p1')

    def test_a_server_that_lost_its_socket_is_not_asked(self):
        with mock.patch.object(h, 'run', self.fake(server_pid='45525')), \
                mock.patch.object(h.shutil, 'which', lambda n: '/x/tmux'):
            got = h.from_tmux({'TMUX': '/tmp/t,6068,53', 'TMUX_PANE': '%53'}, read_env=lambda p: dict(PANE))
        self.assertEqual(got, {})

    def test_ps_env_words(self):
        out = 'tmux attach -t =x HOME=/u HERDR_ENV=1 HERDR_PANE_ID=wE:p1 HERDR_SOCKET_PATH=/s.sock TERM=x'
        self.assertEqual(h.parse_env_words(out),
                         {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'wE:p1', 'HERDR_SOCKET_PATH': '/s.sock'})


class CopiesTest(unittest.TestCase):
    def test_agent_bus_ships_the_same_file(self):
        other = HERE.parents[2] / 'agent-bus' / 'scripts' / 'herdr_env.py'
        if not other.exists():
            self.skipTest('agent-bus not beside coordinator')
        self.assertEqual(other.read_text(), (HERE.parent / 'herdr_env.py').read_text())


if __name__ == '__main__':
    os.environ.pop('HERDR_DISCOVER', None)
    unittest.main()
