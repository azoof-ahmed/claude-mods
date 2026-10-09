"""Unit tests for guard.py. Run: python -m unittest discover -s scripts/tests"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
PLUGIN = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import guard  # noqa: E402

RULES = textwrap.dedent('''\
    [[rules]]
    id = "no-console-log"
    pattern = 'console\\.log\\('
    message = "Use the logger."
    paths = ["src/**/*.ts"]
    exclude = ["**/*.test.ts"]

    [[rules]]
    id = "no-debugger"
    pattern = 'debugger;'
    message = "Debugger left in."
    severity = "warn"
''')

CHECKER = textwrap.dedent('''\
    import sys
    for p in sys.argv[1:]:
        for i, line in enumerate(open(p, encoding="utf-8").read().splitlines(), 1):
            if "eval(" in line:
                print(f"{p}:{i}: no-eval: eval is not allowed")
''')


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / '.claude').mkdir()
        (self.root / 'src').mkdir()
        (self.root / '.claude' / 'guardrails.toml').write_text(RULES, encoding='utf-8')
        self.app = self.root / 'src' / 'app.ts'
        self.app.write_text('const a = 1\nconsole.log(a)\n', encoding='utf-8')
        (self.root / 'src' / 'app.test.ts').write_text('console.log(1)\n', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin or '')
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = guard.main(['--root', str(self.root), *args])
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue(), err.getvalue()

    def edit(self, **ti):
        return json.dumps({'tool_name': ti.pop('tool', 'Edit'), 'tool_input': ti})

    # ------------------------------------------------------------------ check / baseline
    def test_check_fails_then_baseline_ratchets(self):
        code, out, _ = self.run_cli('check')
        self.assertEqual(code, 1)
        self.assertIn('src/app.ts:2: no-console-log: Use the logger.', out)
        self.assertNotIn('app.test.ts', out)
        code, out, _ = self.run_cli('baseline')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_cli('check')[0], 0)
        # moving the baselined line is not new
        self.app.write_text('// header\n\nconst a = 1\n   console.log(a)\n', encoding='utf-8')
        self.assertEqual(self.run_cli('check', '--no-cache')[0], 0)
        # a second copy is
        self.app.write_text('console.log(a)\nconsole.log(a)\n', encoding='utf-8')
        code, out, _ = self.run_cli('check', '--no-cache')
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), 'src/app.ts:2: no-console-log: Use the logger.')

    def test_escape_needs_a_reason(self):
        self.app.write_text('console.log(a) // guardrails-ok: CLI output\nconsole.log(b) // guardrails-ok\n',
                            encoding='utf-8')
        code, out, _ = self.run_cli('check', 'src/app.ts')
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), 'src/app.ts:2: no-console-log: Use the logger.')

    def test_warn_never_fails(self):
        self.app.write_text('debugger;\n', encoding='utf-8')
        code, out, _ = self.run_cli('check')
        self.assertEqual(code, 0)
        self.assertIn('no-debugger: Debugger left in. (warn)', out)

    def test_json_rules_and_config_layers(self):
        (self.root / '.claude' / 'guardrails.toml').unlink()
        (self.root / 'rules.json').write_text(json.dumps({'rules': [
            {'id': 'no-x', 'pattern': 'XXX', 'message': 'no X'}]}), encoding='utf-8')
        (self.root / '.claude' / 'claude-mods.json').write_text(
            json.dumps({'guardrails': {'rulesFile': 'rules.json'}}), encoding='utf-8')
        (self.root / 'a.txt').write_text('XXX\n', encoding='utf-8')
        code, out, _ = self.run_cli('check', 'a.txt')
        self.assertEqual((code, out.strip()), (1, 'a.txt:1: no-x: no X'))

    def test_bad_config_exits_2(self):
        (self.root / '.claude' / 'guardrails.toml').write_text('[[rules]]\nid = "x"\npattern = "("\n')
        code, _, err = self.run_cli('check')
        self.assertEqual(code, 2)
        self.assertIn('bad pattern', err)

    def test_no_rules_is_clean(self):
        (self.root / '.claude' / 'guardrails.toml').unlink()
        self.assertEqual(self.run_cli('check')[0], 0)

    # ------------------------------------------------------------------ check-edit
    def test_edit_adding_a_violation_is_denied(self):
        self.run_cli('baseline')
        code, out, _ = self.run_cli('check-edit', stdin=self.edit(
            file_path=str(self.app), old_string='const a = 1', new_string='const a = 1\nconsole.log("x")'))
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), 'src/app.ts:2: no-console-log: Use the logger.')

    def test_edit_keeping_a_baselined_violation_is_allowed(self):
        self.run_cli('baseline')
        code, out, _ = self.run_cli('check-edit', stdin=self.edit(
            file_path=str(self.app), old_string='const a = 1', new_string='const a = 2'))
        self.assertEqual((code, out), (0, ''))

    def test_edit_on_a_file_with_unbaselined_old_violations_judges_only_new_content(self):
        code, _, _ = self.run_cli('check-edit', stdin=self.edit(
            file_path=str(self.app), old_string='const a = 1', new_string='const b = 1'))
        self.assertEqual(code, 0)

    def test_edit_out_of_scope_or_excluded_is_allowed(self):
        test_file = self.root / 'src' / 'app.test.ts'
        code, _, _ = self.run_cli('check-edit', stdin=self.edit(
            tool='Write', file_path=str(test_file), content='console.log(1)\nconsole.log(2)\n'))
        self.assertEqual(code, 0)
        code, _, err = self.run_cli('--paths', 'lib/**', 'check-edit', stdin=self.edit(
            tool='Write', file_path=str(self.root / 'src' / 'new.ts'), content='console.log(1)\n'))
        self.assertEqual(code, 0)
        self.assertIn('out of scope', err)

    def test_write_and_multiedit(self):
        code, out, _ = self.run_cli('check-edit', stdin=self.edit(
            tool='Write', file_path=str(self.root / 'src' / 'new.ts'), content='x\nconsole.log(1)\n'))
        self.assertEqual((code, out.strip()), (1, 'src/new.ts:2: no-console-log: Use the logger.'))
        code, out, _ = self.run_cli('check-edit', stdin=self.edit(
            tool='MultiEdit', file_path=str(self.app), edits=[
                {'old_string': 'const a = 1', 'new_string': 'const a = 2'},
                {'old_string': 'const a = 2', 'new_string': 'console.log(2)'}]))
        self.assertEqual((code, out.strip()), (1, 'src/app.ts:1: no-console-log: Use the logger.'))

    def test_flat_event_from_the_mod(self):
        ev = json.dumps({'tool': 'Write', 'file_path': str(self.root / 'src' / 'n.ts'), 'content': 'console.log(1)'})
        self.assertEqual(self.run_cli('check-edit', stdin=ev)[0], 1)

    def test_edit_that_does_not_apply_is_left_to_the_tool(self):
        code, _, err = self.run_cli('check-edit', stdin=self.edit(
            file_path=str(self.app), old_string='nope', new_string='console.log(1)'))
        self.assertEqual(code, 0)
        self.assertIn('does not apply', err)

    def test_notebook_edit(self):
        (self.root / '.claude' / 'guardrails.toml').write_text(
            '[[rules]]\nid = "no-print"\npattern = "print\\\\("\nmessage = "no print"\n', encoding='utf-8')
        nb = self.root / 'n.ipynb'
        nb.write_text(json.dumps({'cells': [{'id': 'c1', 'cell_type': 'code', 'source': ['x = 1\n']}]}))
        code, out, _ = self.run_cli('check-edit', stdin=self.edit(
            tool='NotebookEdit', notebook_path=str(nb), cell_id='c1', new_source='print(x)'))
        self.assertEqual((code, out.strip()), (1, 'n.ipynb:1: no-print: no print'))

    # ------------------------------------------------------------------ command rules
    def test_command_rule_on_files_and_on_edits(self):
        (self.root / 'checker.py').write_text(CHECKER, encoding='utf-8')
        (self.root / '.claude' / 'guardrails.toml').write_text(textwrap.dedent(f'''\
            [[rules]]
            id = "evals"
            type = "command"
            command = [{json.dumps(sys.executable)}, "checker.py"]
            paths = ["**/*.py"]
        '''), encoding='utf-8')
        mod = self.root / 'm.py'
        mod.write_text('x = 1\ny = eval("2")\n', encoding='utf-8')
        code, out, _ = self.run_cli('check', 'm.py')
        self.assertEqual((code, out.strip()), (1, 'm.py:2: no-eval: eval is not allowed'))
        self.run_cli('baseline')
        self.assertEqual(self.run_cli('check')[0], 0)
        code, out, _ = self.run_cli('check-edit', stdin=self.edit(
            file_path=str(mod), old_string='x = 1', new_string='x = eval("1")'))
        self.assertEqual((code, out.strip()), (1, 'm.py:1: no-eval: eval is not allowed'))
        code, _, _ = self.run_cli('check-edit', stdin=self.edit(
            file_path=str(mod), old_string='x = 1', new_string='x = 3'))
        self.assertEqual(code, 0)

    def test_example_rules_load(self):
        for name in ('guardrails.toml', 'guardrails.json'):
            shutil.copy(PLUGIN / 'examples' / name, self.root / '.claude' / name)
            if name.endswith('.json'):
                (self.root / '.claude' / 'guardrails.toml').unlink()
            code, out, _ = self.run_cli('rules')
            self.assertEqual(code, 0, out)
            self.assertIn('no-hardcoded-secret', out)
        (self.root / 'src' / 'cfg.ts').write_text('const apiKey = "abcd1234efgh5678"\n// TODO later\n')
        code, out, _ = self.run_cli('check', 'src/cfg.ts')
        self.assertEqual(code, 1)
        self.assertIn('src/cfg.ts:1: no-hardcoded-secret', out)
        self.assertIn('src/cfg.ts:2: no-todo-in-src', out)

    def test_cli_subprocess(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / 'guard.py'), '--root', str(self.root), 'check-edit'],
                           input=self.edit(tool='Write', file_path=str(self.root / 'src' / 'z.ts'),
                                           content='console.log(1)\n'),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stdout.strip(), 'src/z.ts:1: no-console-log: Use the logger.')


if __name__ == '__main__':
    unittest.main()
