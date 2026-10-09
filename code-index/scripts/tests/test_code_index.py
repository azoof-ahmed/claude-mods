"""Unit tests for the code-index scripts. Run: python -m unittest discover -s scripts/tests"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import _common  # noqa: E402
import build_code_index as bci  # noqa: E402
import md_toc  # noqa: E402
import status_tags  # noqa: E402


def names(info):
    return [(s[1], s[2]) for s in info['symbols']]


class GlobTest(unittest.TestCase):
    def test_double_star_and_braces(self):
        rx = _common.glob_re('src/**/*.{ts,tsx}')
        self.assertTrue(_common.matches(rx, 'src/a.ts'))
        self.assertTrue(_common.matches(rx, 'src/x/y/b.tsx'))
        self.assertFalse(_common.matches(rx, 'lib/a.ts'))
        self.assertFalse(_common.matches(rx, 'src/a.js'))

    def test_bare_name_matches_any_depth(self):
        rx = _common.glob_re('node_modules/, *.min.js')
        self.assertTrue(_common.matches(rx, 'a/node_modules/x/y.js'))
        self.assertTrue(_common.matches(rx, 'web/app.min.js'))
        self.assertFalse(_common.matches(rx, 'web/app.js'))


class ParseTest(unittest.TestCase):
    def test_python(self):
        info = bci.parse_text(textwrap.dedent('''\
            """Tools for widgets."""
            import os

            # A widget.
            class Widget:
                def spin(self):
                    """Spin it."""
                    def inner():
                        pass

            @decorator
            async def run():
                pass
        '''), 'python')
        self.assertEqual(info['purpose'], 'Tools for widgets.')
        self.assertEqual(names(info), [('class', 'Widget'), ('method', 'Widget.spin'), ('def', 'run')])
        self.assertEqual(info['symbols'][0][3], 'A widget.')
        self.assertEqual(info['symbols'][1][3], 'Spin it.')

    def test_typescript(self):
        info = bci.parse_text(textwrap.dedent('''\
            // Router for the app.
            export interface Route { path: string }
            export type Handler = (r: Route) => void
            /** Starts it. */
            export async function start(port: number): Promise<void> {
              if (port) {
              }
            }
            export const stop = async () => {}
            class Server {
              listen(port: number): void {
              }
            }
        '''), 'ts')
        self.assertEqual(info['purpose'], 'Router for the app.')
        self.assertEqual(names(info), [('interface', 'Route'), ('type', 'Handler'), ('function', 'start'),
                                       ('function', 'stop'), ('class', 'Server'), ('method', 'listen')])
        self.assertEqual(info['symbols'][2][3], 'Starts it.')

    def test_cpp(self):
        info = bci.parse_text(textwrap.dedent('''\
            // Copyright someone
            // Engine core.
            #include <x>
            namespace a {
            // The engine.
            class ENGINE_API Engine : public Base {
            public:
                void Tick(float dt);
            };
            }
            void Engine::Tick(float dt)
            {
                if (dt) { Run(); }
            }
            int helper(int x);
        '''), 'c')
        self.assertEqual(info['purpose'], 'Engine core.')
        self.assertEqual(names(info), [('class', 'Engine'), ('function', 'Engine::Tick')])
        hdr = bci.parse_text('// API.\nLIB_API int lib_add(int a, int b);\n#define X(a) a\n', 'c', header=True)
        self.assertEqual(names(hdr), [('function', 'lib_add')])

    def test_go_rust_java_cs(self):
        go = bci.parse_text('package x\n\ntype Server struct {}\n\nfunc (s *Server) Run() error {\n}\n', 'go')
        self.assertEqual(names(go), [('struct', 'Server'), ('func', 'Run')])
        rs = bci.parse_text('pub struct A;\nimpl Display for A {\n    pub fn fmt(&self) {}\n}\nasync fn go() {}\n', 'rust')
        self.assertEqual(names(rs), [('struct', 'A'), ('impl', 'Display for A'), ('fn', 'fmt'), ('fn', 'go')])
        java = bci.parse_text('public class App {\n  public static void main(String[] a) {\n  }\n}\n', 'java')
        self.assertEqual(names(java), [('class', 'App'), ('method', 'main')])
        cs = bci.parse_text('public sealed class Svc {\n  public async Task<int> RunAsync(int x)\n  {\n  }\n}\n', 'cs')
        self.assertEqual(names(cs), [('class', 'Svc'), ('method', 'RunAsync')])

    def test_status_tags_any_comment_style(self):
        info = bci.parse_text('x = 1\n# STATUS(T1): PARTIAL -- half done [ana]\n', 'python')
        self.assertEqual(info['symbols'], [[2, 'STATUS', 'T1', 'PARTIAL -- half done [ana]']])
        info = bci.parse_text('int x;\n/* WIP(T2): OPEN -- later */\n', 'c', tag='WIP')
        self.assertEqual(info['symbols'], [[2, 'STATUS', 'T2', 'OPEN -- later']])

    def test_markdown(self):
        text = '# Title\nintro\n## A\na\n```\n# not a heading\n```\n## B\nb\n### deep\n'
        info = bci.parse_text(text, 'md')
        self.assertEqual([(s[2], s[3]) for s in info['symbols']], [('Title', '[1-10]'), ('A', '[3-7]'), ('B', '[8-10]')])
        self.assertEqual(md_toc.toc(text, depth=3, grep='deep'), ['    [10-10] ### deep'])


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / '.claude').mkdir()
        (self.root / 'src').mkdir()
        (self.root / 'node_modules' / 'dep').mkdir(parents=True)
        (self.root / 'src' / 'app.py').write_text('def main():\n    pass\n# STATUS(T9): OPEN -- wire it\n')
        (self.root / 'src' / 'util.ts').write_text('export function util() {}\n')
        (self.root / 'node_modules' / 'dep' / 'x.js').write_text('function skipped() {}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_root_and_config_layers(self):
        sub = self.root / 'src'
        self.assertEqual(_common.find_root(sub), self.root)
        os.environ['CODE_INDEX_STATUS_TAG'] = 'ENV'
        try:
            self.assertEqual(_common.resolve('statusTag', None, self.root), 'ENV')
            mods = self.root / '.claude' / 'claude-mods.json'
            mods.write_text(json.dumps({'code-index': {'statusTag': 'TASK'}}))
            self.assertEqual(_common.resolve('statusTag', None, self.root), 'TASK')    # project file beats env
            self.assertEqual(_common.resolve('statusTag', 'FLAG', self.root), 'FLAG')  # a typed flag beats both
            mods.unlink()
        finally:
            del os.environ['CODE_INDEX_STATUS_TAG']
        self.assertEqual(_common.resolve('indexPath', None, self.root), '.claude/index/code_index.md')

    def test_build_md_then_cached(self):
        idx = self.root / '.claude' / 'index' / 'code_index.md'
        inc, exc = _common.DEFAULTS['include'], _common.DEFAULTS['exclude']
        r = bci.build(self.root, idx, inc, exc, 'STATUS', 'md')
        self.assertEqual((r['files'], r['parsed']), (2, 2))
        text = idx.read_text(encoding='utf-8')
        self.assertIn('- L1 def `main`', text)
        self.assertIn('**STATUS(T9)** OPEN -- wire it', text)
        self.assertIn('- L1 function `util`', text)
        self.assertNotIn('skipped', text)
        r = bci.build(self.root, idx, inc, exc, 'STATUS', 'md')
        self.assertEqual((r['parsed'], r['cached']), (0, 2))

    def test_build_json(self):
        idx = self.root / 'out.json'
        bci.build(self.root, idx, '**/*.py', '', 'STATUS', 'json')
        data = json.loads(idx.read_text(encoding='utf-8'))
        self.assertEqual(data['files']['src/app.py']['symbols'][0]['name'], 'main')

    def test_status_list_and_strip(self):
        inc, exc = _common.DEFAULTS['include'], _common.DEFAULTS['exclude']
        found = list(status_tags.scan(self.root, 'STATUS', inc, exc))
        self.assertEqual([(r, ln, m['task']) for r, ln, m in found], [('src/app.py', 3, 'T9')])
        hits = status_tags.strip(self.root, 'STATUS', 'T9', inc, exc, dry_run=True)
        self.assertEqual(hits, {'src/app.py': [3]})
        self.assertIn('STATUS(T9)', (self.root / 'src' / 'app.py').read_text())
        status_tags.strip(self.root, 'STATUS', 'T9', inc, exc)
        self.assertEqual((self.root / 'src' / 'app.py').read_text(), 'def main():\n    pass\n')

    def test_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / 'build_code_index.py'), '--root', str(self.root)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('.claude/index/code_index.md: 2 files', r.stdout)
        r = subprocess.run([sys.executable, str(SCRIPTS / 'status_tags.py'), '--root', str(self.root), 'list'],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), 'src/app.py:3: STATUS(T9) OPEN -- wire it')


if __name__ == '__main__':
    unittest.main()
