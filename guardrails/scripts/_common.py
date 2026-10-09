"""Shared helpers for the guardrails runner: project root, layered config, globs, file listing. Stdlib only.

Config resolution, highest first: CLI flag > `<root>/.claude/claude-mods.json` section > environment variable
(the plugin's options arrive this way) > built-in default. The section is "guardrails".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PLUGIN = 'guardrails'

DEFAULTS = {
    'rulesFile': '',            # empty: .claude/guardrails.toml, else .claude/guardrails.json
    'baselineFile': '.claude/guardrails.baseline.json',
    'paths': '**',
}
ENV = {
    'rulesFile': 'GUARDRAILS_RULES',
    'baselineFile': 'GUARDRAILS_BASELINE',
    'paths': 'GUARDRAILS_PATHS',
}
# never scanned by a full `check` (a rules file can add more under [scan] exclude)
SCAN_EXCLUDE = ('**/node_modules/**,**/.git/**,**/dist/**,**/build/**,**/out/**,**/target/**,**/bin/**,'
                '**/obj/**,**/vendor/**,**/.venv/**,**/venv/**,**/__pycache__/**,**/.next/**,**/coverage/**')


def find_root(start: str | os.PathLike | None = None) -> Path:
    """Nearest ancestor (or start itself) holding .git or .claude; else the start directory."""
    here = Path(start or os.getcwd()).resolve()
    for d in (here, *here.parents):
        if (d / '.git').exists() or (d / '.claude').is_dir():
            return d
    return here


def project_section(root: Path, name: str = PLUGIN) -> dict:
    p = root / '.claude' / 'claude-mods.json'
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        print(f'{name}: ignoring {p}: {e}', file=sys.stderr)
        return {}
    sec = data.get(name) if isinstance(data, dict) else None
    return sec if isinstance(sec, dict) else {}


def resolve(key: str, cli_value, root: Path, defaults: dict = DEFAULTS, env: dict = ENV, name: str = PLUGIN):
    """CLI flag > project file section > env var > default. A project's own file beats the person's plugin
    options, which reach the scripts as environment variables and always carry defaults."""
    if cli_value not in (None, ''):
        return cli_value
    sec = project_section(root, name)
    if sec.get(key) not in (None, ''):
        return sec[key]
    env_name = env.get(key)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return defaults.get(key)


def split_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    out, cur, depth = [], '', 0
    for ch in str(value):          # commas inside {a,b} belong to the glob
        if ch == ',' and depth == 0:
            out.append(cur)
            cur = ''
            continue
        depth += (ch == '{') - (ch == '}')
        cur += ch
    out.append(cur)
    return [v.strip() for v in out if v.strip()]


def _expand_braces(pat: str) -> list[str]:
    m = re.search(r'\{([^{}]*)\}', pat)
    if not m:
        return [pat]
    out = []
    for alt in m.group(1).split(','):
        out += _expand_braces(pat[:m.start()] + alt + pat[m.end():])
    return out


def _one_glob(pat: str) -> str:
    pat = pat.replace('\\', '/')
    while pat.startswith('./'):
        pat = pat[2:]
    if '/' not in pat.rstrip('/'):
        pat = '**/' + pat            # gitignore style: a bare name matches at any depth
    if pat.endswith('/'):
        pat += '**'
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if pat.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif pat.startswith('**', i):
            out.append('.*')
            i += 2
        elif c == '*':
            out.append('[^/]*')
            i += 1
        elif c == '?':
            out.append('[^/]')
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return ''.join(out)


def glob_re(patterns) -> re.Pattern | None:
    """One compiled regex matching a posix relative path against any of the globs (`**`, `*`, `?`, `{a,b}`)."""
    alts = [_one_glob(p) for pat in split_list(patterns) for p in _expand_braces(pat)]
    if not alts:
        return None
    flags = re.IGNORECASE if os.name == 'nt' else 0
    return re.compile(r'\A(?:' + '|'.join(alts) + r')\Z', flags)


def matches(rx: re.Pattern | None, rel: str) -> bool:
    return bool(rx and rx.match(rel))


def rel_of(root: Path, p: str | os.PathLike) -> str:
    p = Path(p)
    if not p.is_absolute():
        p = (Path.cwd() / p)
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def list_files(root: Path, include, exclude) -> list[str]:
    """Relative posix paths under root matching include and not exclude. Uses `git ls-files` (tracked plus
    untracked, .gitignore honoured) when root is a git work tree, else a directory walk."""
    inc, exc = glob_re(include), glob_re(exclude)
    rels = None
    if (root / '.git').exists():
        try:
            r = subprocess.run(['git', '-C', str(root), 'ls-files', '-co', '--exclude-standard', '-z'],
                               capture_output=True, timeout=60)
            if r.returncode == 0:
                rels = [x for x in r.stdout.decode('utf-8', 'replace').split('\0') if x]
        except (OSError, subprocess.SubprocessError):
            rels = None
    if rels is None:
        rels = []
        for dp, dns, fns in os.walk(root):
            drel = Path(dp).relative_to(root).as_posix()
            drel = '' if drel == '.' else drel + '/'
            dns[:] = sorted(d for d in dns if not matches(exc, f'{drel}{d}/x'))
            rels += [drel + f for f in sorted(fns)]
    out = [r for r in rels if matches(inc, r) and not matches(exc, r) and (root / r).is_file()]
    return sorted(set(out))


def utf8_stdout():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors='replace')
        except AttributeError:
            pass
