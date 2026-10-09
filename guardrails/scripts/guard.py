"""guardrails: a lint gate with a baseline ratchet. The project writes the rules; this runs them.

Existing violations are recorded once in a baseline; only NEW ones fail. A violation is keyed by
(rule, file, the offending line's text with whitespace collapsed), never by line number, so moving or
editing around a baselined line is not "new", while adding another copy of it is.

  python guard.py check [PATH ...] [--all]   lint files (default: the whole project); exit 1 on new errors
  python guard.py baseline                   record every current error in the baseline file
  python guard.py check-edit < tool.json     judge a proposed Edit/Write/MultiEdit/NotebookEdit: compute the
                                             file as it would be after the edit, in memory, and report only
                                             the errors the edit adds (exit 1), for a PreToolUse gate
  python guard.py rules                      list the loaded rules

Common flags: --root DIR, --rules FILE, --baseline FILE, --paths GLOBS (check-edit: the files the gate covers).
Config: flags > .claude/claude-mods.json "guardrails" > env (GUARDRAILS_RULES, GUARDRAILS_BASELINE,
GUARDRAILS_PATHS; the plugin's options arrive this way) > defaults (.claude/guardrails.toml or .json, .claude/guardrails.baseline.json, every file).
Exit codes: 0 clean, 1 new errors, 2 the runner or the config is broken.

Rules file (TOML on Python 3.11+, or JSON with the same shape):

  [scan]                              # optional: what a full `check` walks
  include = ["src/**"]
  exclude = ["**/generated/**"]

  [[rules]]
  id = "no-console-log"
  pattern = 'console\\.log\\('        # Python regex, matched per line
  message = "Use the logger."
  paths = ["src/**/*.{ts,tsx}"]       # default: every file
  exclude = ["**/*.test.ts"]
  severity = "error"                  # or "warn": printed, never fails
  ignore_case = false

  [[rules]]
  id = "project-check"
  type = "command"                    # runs a project script with the file paths appended
  command = ["python", "scripts/check.py"]
  paths = ["**/*.py"]                 # it prints one violation per line: path:line: rule: message

A line carrying `guardrails-ok: <reason>` is exempt from regex rules; a bare `guardrails-ok` is not.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (SCAN_EXCLUDE, find_root, glob_re, list_files, matches, rel_of, resolve,  # noqa: E402
                     split_list, utf8_stdout)

CACHE_VERSION = 1
ESCAPE_RE = re.compile(r'guardrails-ok:\s*\S')
OUT_RE = re.compile(r'^(?P<path>.+?):(?P<line>\d+):(?:\d+:)?\s*(?P<rule>[^:\s][^:]*?):\s*(?P<msg>.*)$')
EDIT_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')


class ConfigError(Exception):
    pass


def norm(line: str) -> str:
    return ' '.join(line.split())


# ----------------------------------------------------------------------------------------------- rules
class Rule:
    def __init__(self, raw: dict, n: int):
        if not isinstance(raw, dict) or not raw.get('id'):
            raise ConfigError(f'rule #{n + 1} has no id')
        self.id = str(raw['id'])
        self.type = raw.get('type', 'regex')
        self.message = str(raw.get('message', ''))
        self.severity = raw.get('severity', 'error')
        if self.severity not in ('error', 'warn'):
            raise ConfigError(f'rule {self.id}: severity must be "error" or "warn", not {self.severity!r}')
        self.paths = glob_re(raw.get('paths') or '**')
        self.exclude = glob_re(raw.get('exclude'))
        self.timeout = float(raw.get('timeout', 120))
        if self.type == 'regex':
            if not raw.get('pattern'):
                raise ConfigError(f'rule {self.id}: a regex rule needs a pattern')
            try:
                self.rx = re.compile(raw['pattern'], re.IGNORECASE if raw.get('ignore_case') else 0)
            except re.error as e:
                raise ConfigError(f'rule {self.id}: bad pattern: {e}') from e
        elif self.type == 'command':
            cmd = raw.get('command')
            if isinstance(cmd, str):
                cmd = shlex.split(cmd, posix=os.name != 'nt')
            if not cmd or not isinstance(cmd, list):
                raise ConfigError(f'rule {self.id}: a command rule needs a command (a list, or a string)')
            self.command = [str(c) for c in cmd]
        else:
            raise ConfigError(f'rule {self.id}: unknown type {self.type!r} (regex or command)')

    def applies(self, rel: str) -> bool:
        return matches(self.paths, rel) and not matches(self.exclude, rel)


class Config:
    def __init__(self, root: Path, rules_file: str | None, baseline_file: str | None, gate_paths: str | None):
        self.root = root
        rf = resolve('rulesFile', rules_file, root)
        if rf:
            self.rules_path = self._abs(rf)
        else:
            toml, js = root / '.claude' / 'guardrails.toml', root / '.claude' / 'guardrails.json'
            self.rules_path = toml if toml.is_file() or not js.is_file() else js
        self.baseline_path = self._abs(resolve('baselineFile', baseline_file, root))
        self.gate = glob_re(resolve('paths', gate_paths, root) or '**')
        self.rules, self.scan_include, self.scan_exclude, self.digest = [], '**', SCAN_EXCLUDE, ''
        if self.rules_path.is_file():
            self._load()

    def _abs(self, p) -> Path:
        p = Path(p)
        return p if p.is_absolute() else self.root / p

    def _load(self):
        raw = self.rules_path.read_bytes()
        self.digest = hashlib.sha1(raw).hexdigest()
        if self.rules_path.suffix.lower() == '.toml':
            try:
                import tomllib
            except ImportError as e:   # Python < 3.11
                raise ConfigError(f'{self.rules_path.name}: TOML needs Python 3.11+; write the same rules as '
                                  f'.claude/guardrails.json instead') from e
            try:
                data = tomllib.loads(raw.decode('utf-8'))
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f'{self.rules_path}: {e}') from e
        else:
            try:
                data = json.loads(raw.decode('utf-8'))
            except ValueError as e:
                raise ConfigError(f'{self.rules_path}: {e}') from e
        rules = data.get('rules', data.get('rule', []))
        if not isinstance(rules, list):
            raise ConfigError(f'{self.rules_path}: "rules" must be a list')
        self.rules = [Rule(r, n) for n, r in enumerate(rules)]
        dup = [k for k, c in Counter(r.id for r in self.rules).items() if c > 1]
        if dup:
            raise ConfigError(f'{self.rules_path}: duplicate rule ids {dup}')
        scan = data.get('scan') or {}
        self.scan_include = scan.get('include') or '**'
        self.scan_exclude = split_list(SCAN_EXCLUDE) + split_list(scan.get('exclude'))

    def rel(self, p) -> str:
        return rel_of(self.root, p)


# ------------------------------------------------------------------------------------------ violations
def V(rule, path, line, text, message, severity):
    return {'rule': rule, 'path': path, 'line': line, 'text': norm(text), 'message': message, 'severity': severity}


def regex_violations(rule: Rule, rel: str, text: str) -> list[dict]:
    out = []
    for i, line in enumerate(text.splitlines()):
        if rule.rx.search(line) and not ESCAPE_RE.search(line):
            out.append(V(rule.id, rel, i + 1, line, rule.message, rule.severity))
    return out


def command_violations(cfg: Config, rule: Rule, targets: list[tuple[str, str, list[str]]]) -> list[dict]:
    """targets: (path given to the command, the relative path it stands for, that file's lines)."""
    if not targets:
        return []
    by_arg = {os.path.normcase(os.path.normpath(a)): (rel, lines) for a, rel, lines in targets}
    env = dict(os.environ, GUARDRAILS_ROOT=str(cfg.root))
    if len(targets) == 1:
        env['GUARDRAILS_ORIGINAL_PATH'] = targets[0][1]
    try:
        r = subprocess.run(rule.command + [a for a, _, _ in targets], cwd=cfg.root, env=env, capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=rule.timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ConfigError(f'rule {rule.id}: could not run {rule.command}: {e}') from e
    out = []
    for raw in r.stdout.splitlines():
        m = OUT_RE.match(raw.strip())
        if not m:
            continue
        p = m.group('path')
        cands = [p, str(cfg.root / p)]
        hit = next((by_arg[k] for k in (os.path.normcase(os.path.normpath(c)) for c in cands) if k in by_arg), None)
        rel, lines = hit if hit else (cfg.rel(p), [])
        ln = int(m.group('line'))
        text = lines[ln - 1] if 0 < ln <= len(lines) else ''
        out.append(V(m.group('rule'), rel, ln, text, m.group('msg'), rule.severity))
    if r.returncode not in (0, 1) and not out:
        raise ConfigError(f'rule {rule.id}: {rule.command[0]} exited {r.returncode}: '
                          f'{(r.stderr or r.stdout).strip()[:400]}')
    return out


def read_text(p: Path) -> str | None:
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if b'\0' in raw[:4096]:
        return None
    return raw.decode('utf-8', 'replace')


def lint_files(cfg: Config, rels: list[str], use_cache: bool = True) -> list[dict]:
    """Every violation in these files. Regex results are cached by file size and mtime."""
    cache_path = cfg.root / '.claude' / '.guardrails-cache.json'
    stamp = f'{CACHE_VERSION}:{cfg.digest}'
    cache = {}
    if use_cache and cache_path.is_file():
        try:
            c = json.loads(cache_path.read_text(encoding='utf-8'))
            cache = c.get('files', {}) if c.get('stamp') == stamp else {}
        except (OSError, ValueError):
            cache = {}
    regex_rules = [r for r in cfg.rules if r.type == 'regex']
    out, fresh = [], {}
    texts = {}
    for rel in rels:
        p = cfg.root / rel
        try:
            st = p.stat()
        except OSError:
            continue
        key = f'{st.st_size}:{st.st_mtime_ns}'
        hit = cache.get(rel)
        if hit and hit['key'] == key:
            found = hit['v']
        else:
            text = read_text(p)
            if text is None:
                continue
            texts[rel] = text
            found = [v for r in regex_rules if r.applies(rel) for v in regex_violations(r, rel, text)]
        fresh[rel] = {'key': key, 'v': found}
        out += found
    if use_cache:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({'stamp': stamp, 'files': fresh}), encoding='utf-8')
        except OSError:
            pass
    for rule in (r for r in cfg.rules if r.type == 'command'):
        targets = []
        for rel in rels:
            if rule.applies(rel) and (cfg.root / rel).is_file():
                text = texts.get(rel) if rel in texts else read_text(cfg.root / rel)
                if text is not None:
                    targets.append((rel, rel, text.splitlines()))
        out += command_violations(cfg, rule, targets)
    return out


def lint_text(cfg: Config, rel: str, text: str, rules: list | None = None) -> list[dict]:
    """Every violation in `text` as if it were the file `rel` (command rules see a temporary copy)."""
    out = []
    for rule in cfg.rules if rules is None else rules:
        if not rule.applies(rel):
            continue
        if rule.type == 'regex':
            out += regex_violations(rule, rel, text)
            continue
        with tempfile.TemporaryDirectory(prefix='guardrails-') as tmp:
            p = Path(tmp) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8', newline='')
            out += command_violations(cfg, rule, [(str(p), rel, text.splitlines())])
    return out


# --------------------------------------------------------------------------------------------- baseline
def key(v: dict) -> tuple:
    return (v['rule'], v['path'], v['text'])


def load_baseline(cfg: Config) -> Counter:
    c = Counter()
    if not cfg.baseline_path.is_file():
        return c
    try:
        data = json.loads(cfg.baseline_path.read_text(encoding='utf-8'))
        for e in data.get('entries', []):
            c[(e['rule'], e['path'], e['text'])] += int(e.get('count', 1))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ConfigError(f'{cfg.baseline_path}: {e}') from e
    return c


def write_baseline(cfg: Config, found: list[dict]) -> int:
    c = Counter(key(v) for v in found if v['severity'] == 'error')
    entries = [{'rule': r, 'path': p, 'text': t, 'count': n} for (r, p, t), n in sorted(c.items())]
    cfg.baseline_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.baseline_path.write_text(json.dumps({
        'note': 'Existing violations when the baseline was taken; only NEW ones fail. Keyed by rule, path and '
                'the line text, not the line number. Re-run `guard.py baseline` only after FIXING violations.',
        'entries': entries}, indent=1) + '\n', encoding='utf-8')
    return sum(c.values())


def surplus(found: list[dict], allowed: Counter, prefer_lines: set[int] | None = None) -> list[dict]:
    """The errors whose key occurs more often than allowed; for an edit, those on changed lines first."""
    groups = defaultdict(list)
    for v in found:
        if v['severity'] == 'error':
            groups[key(v)].append(v)
    out = []
    for k, lst in groups.items():
        extra = len(lst) - allowed.get(k, 0)
        if extra > 0:
            if prefer_lines:
                lst = sorted(lst, key=lambda v: v['line'] in prefer_lines)
            out += sorted(lst[-extra:], key=lambda v: v['line'])
    return sorted(out, key=lambda v: (v['path'], v['line']))


def fmt(v: dict) -> str:
    return f'{v["path"]}:{v["line"]}: {v["rule"]}: {v["message"] or "violation"}' + \
           (' (warn)' if v['severity'] == 'warn' else '')


# ------------------------------------------------------------------------------------------- edits
def apply_edit(text: str, old: str, new: str, replace_all: bool) -> str | None:
    """The file after one Edit, or None when the Edit would fail on its own."""
    if old == '':
        return new if text == '' else None
    if old not in text and '\r\n' in text:   # the Edit tool matches CRLF files with LF strings
        old, new = old.replace('\n', '\r\n'), new.replace('\n', '\r\n')
    if old not in text:
        return None
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def notebook_text(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        cells = json.loads(raw).get('cells', [])
    except ValueError:
        return []
    return [{'id': c.get('id'), 'source': ''.join(c.get('source', [])) if isinstance(c.get('source'), list)
             else str(c.get('source', ''))} for c in cells]


def edit_after(tool: str, ti: dict, old: str) -> str | None:
    if tool == 'Write':
        return ti.get('content', '')
    if tool == 'Edit':
        return apply_edit(old, ti.get('old_string', ''), ti.get('new_string', ''), bool(ti.get('replace_all')))
    if tool == 'MultiEdit':
        new = old
        for e in ti.get('edits') or []:
            new = apply_edit(new, e.get('old_string', ''), e.get('new_string', ''), bool(e.get('replace_all')))
            if new is None:
                return None
        return new
    return None


def check_edit(cfg: Config, ev: dict) -> tuple[list[dict], list[dict], str]:
    """(new errors, warnings on changed lines, a note) for one proposed edit."""
    tool = ev.get('tool_name') or ev.get('tool') or ''
    ti = ev.get('tool_input') if isinstance(ev.get('tool_input'), dict) else ev
    if tool not in EDIT_TOOLS:
        return [], [], f'not an edit tool: {tool or "?"}'
    path = ti.get('file_path') or ti.get('notebook_path') or ''
    if not path:
        return [], [], 'no file path'
    p = Path(path)
    if not p.is_absolute():
        p = cfg.root / p
    rel = cfg.rel(p)
    if rel.startswith('/') or re.match(r'^[A-Za-z]:', rel) or rel.startswith('../'):
        return [], [], f'outside the project: {path}'
    if not matches(cfg.gate, rel):
        return [], [], f'out of scope: {rel}'
    rules = [r for r in cfg.rules if r.applies(rel)]
    if not rules:
        return [], [], f'no rule covers {rel}'
    old = read_text(p) if p.is_file() else ''
    old = old or ''
    if tool == 'NotebookEdit':
        cells = notebook_text(old)
        before = '\n'.join(c['source'] for c in cells)
        mode, cid = ti.get('edit_mode') or 'replace', ti.get('cell_id')
        idx = next((i for i, c in enumerate(cells) if c['id'] == cid), None)
        src = ti.get('new_source', '')
        if mode == 'insert':
            cells.insert(0 if idx is None else idx + 1, {'id': None, 'source': src})
        elif idx is not None:
            if mode == 'delete':
                cells.pop(idx)
            else:
                cells[idx] = {'id': cid, 'source': src}
        else:
            cells.append({'id': None, 'source': src})
        old, new = before, '\n'.join(c['source'] for c in cells)
    else:
        new = edit_after(tool, ti, old)
        if new is None:
            return [], [], 'the edit does not apply; the tool will report it'
    found_new = lint_text(cfg, rel, new, rules)
    found_old = lint_text(cfg, rel, old, rules) if old else []
    changed = set()
    for tag, _i1, _i2, j1, j2 in SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False).get_opcodes():
        if tag in ('replace', 'insert'):
            changed.update(range(j1 + 1, j2 + 1))
    base, before = load_baseline(cfg), Counter(key(v) for v in found_old if v['severity'] == 'error')
    allowed = Counter({k: max(base.get(k, 0), before.get(k, 0)) for k in set(base) | set(before)})
    errs = surplus(found_new, allowed, changed)
    warns = [v for v in found_new if v['severity'] == 'warn' and v['line'] in changed]
    return errs, warns, f'{rel}: {len(rules)} rule(s)'


# ---------------------------------------------------------------------------------------------- CLI
def expand(cfg: Config, paths: list[str]) -> list[str]:
    out = [] if paths else list_files(cfg.root, cfg.scan_include, cfg.scan_exclude)
    for a in paths:
        p = Path(a) if Path(a).is_absolute() else Path.cwd() / a
        if not p.exists() and not Path(a).is_absolute():
            p = cfg.root / a        # relative to the project root when not relative to the cwd
        if p.is_dir():
            d = cfg.rel(p).rstrip('/')
            d = '' if d == '.' else d
            out += [r for r in list_files(cfg.root, cfg.scan_include, cfg.scan_exclude)
                    if not d or r.startswith(d + '/')]
        elif p.is_file():
            out.append(cfg.rel(p))
        else:
            print(f'guardrails: no such file: {a}', file=sys.stderr)
    own = {cfg.rel(cfg.rules_path), cfg.rel(cfg.baseline_path), '.claude/.guardrails-cache.json'}
    return sorted(set(out) - own)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root')
    ap.add_argument('--rules', help='rules file (.toml or .json)')
    ap.add_argument('--baseline', help='baseline file')
    ap.add_argument('--paths', help='comma-separated globs the edit gate covers')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('check')
    s.add_argument('files', nargs='*')
    s.add_argument('--all', action='store_true', help='report every violation, baseline ignored')
    s.add_argument('--no-cache', action='store_true')
    sub.add_parser('baseline')
    sub.add_parser('check-edit')
    sub.add_parser('rules')
    a = ap.parse_args(argv)
    utf8_stdout()
    try:
        root = Path(a.root).resolve() if a.root else find_root()
        cfg = Config(root, a.rules, a.baseline, a.paths)
        if not cfg.rules:
            print(f'guardrails: no rules ({cfg.rel(cfg.rules_path)} is missing or empty)')
            return 0
        if a.cmd == 'rules':
            for r in cfg.rules:
                print(f'{r.id} [{r.type}, {r.severity}] {r.message}')
            return 0
        if a.cmd == 'check-edit':
            try:
                ev = json.loads(sys.stdin.read() or '{}')
            except ValueError as e:
                raise ConfigError(f'check-edit: stdin is not JSON: {e}') from e
            errs, warns, note = check_edit(cfg, ev)
            for v in warns:
                print(fmt(v))
            if errs:
                print('\n'.join(fmt(v) for v in errs))
                return 1
            print(f'guardrails: ok ({note})', file=sys.stderr)
            return 0
        rels = expand(cfg, a.files) if a.cmd == 'check' else expand(cfg, [])
        found = lint_files(cfg, rels, use_cache=not getattr(a, 'no_cache', False))
        if a.cmd == 'baseline':
            n = write_baseline(cfg, found)
            print(f'guardrails: baseline of {n} error(s) in {len({v["path"] for v in found})} file(s) -> '
                  f'{cfg.rel(cfg.baseline_path)}')
            return 0
        base = Counter() if a.all else load_baseline(cfg)
        if a.files and not a.all:   # a partial check counts only those files' baseline entries
            keep = set(rels)
            base = Counter({k: n for k, n in base.items() if k[1] in keep})
        new = surplus(found, base)
        warns = [v for v in found if v['severity'] == 'warn']
        for v in warns:
            print(fmt(v))
        for v in new:
            print(fmt(v))
        errors = sum(1 for v in found if v['severity'] == 'error')
        print(f'guardrails: {len(rels)} files, {errors} error(s), {sum(base.values())} baselined, '
              f'{len(new)} NEW, {len(warns)} warning(s)', file=sys.stderr)
        return 1 if new else 0
    except ConfigError as e:
        print(f'guardrails: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
