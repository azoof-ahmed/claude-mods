"""Build a compact, book-style index of a project's code so agents read the index, not whole files.

Per file: its line count and purpose (first comment / docstring), then every class/struct/enum/interface/trait,
function and method with its line and a one-line summary (the comment just above it), plus STATUS(<task>) tags.
Markdown files list their H1/H2 headings with line ranges [start-end].

  python build_code_index.py [--root DIR] [--index-path P] [--include GLOBS] [--exclude GLOBS]
                             [--status-tag TAG] [--format md|json] [--full]

Incremental: unchanged files (same size and mtime) come from a cache beside the index; --full ignores it.
Languages (regex, on purpose: no toolchain needed): Python, TypeScript/JavaScript, C/C++, C#, Go, Rust,
Java, Kotlin, Markdown. Config: flags > .claude/claude-mods.json "code-index" > env (CODE_INDEX_*) > defaults.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import find_root, list_files, rel_of, resolve, utf8_stdout  # noqa: E402

INDEX_VERSION = 1  # bump when the parser changes: invalidates every cache

LANG_BY_EXT = {
    '.py': 'python', '.pyi': 'python',
    '.ts': 'ts', '.tsx': 'ts', '.mts': 'ts', '.cts': 'ts', '.js': 'ts', '.jsx': 'ts', '.mjs': 'ts', '.cjs': 'ts',
    '.c': 'c', '.h': 'c', '.cc': 'c', '.cpp': 'c', '.cxx': 'c', '.hpp': 'c', '.hh': 'c', '.hxx': 'c', '.inl': 'c',
    '.cs': 'cs', '.go': 'go', '.rs': 'rust', '.java': 'java', '.kt': 'kotlin', '.kts': 'kotlin',
    '.md': 'md', '.markdown': 'md', '.mdx': 'md',
}
LINE_COMMENT = {'python': ('#',), 'md': ()}
DEFAULT_COMMENT = ('//', '/*', '*')
# lines skipped when walking up from a symbol to the comment above it (decorators, attributes, annotations)
DECORATOR = {'python': re.compile(r'^\s*@'), 'cs': re.compile(r'^\s*\['), 'java': re.compile(r'^\s*@'),
             'kotlin': re.compile(r'^\s*@'), 'rust': re.compile(r'^\s*#!?\['),
             'ts': re.compile(r'^\s*@'), 'c': re.compile(r'^\s*template\s*<')}
KEYWORDS = {'if', 'for', 'foreach', 'while', 'switch', 'return', 'sizeof', 'catch', 'decltype', 'static_assert',
            'new', 'delete', 'throw', 'else', 'do', 'case', 'using', 'typedef', 'template', 'goto', 'function',
            'typeof', 'await', 'yield', 'super', 'this', 'base', 'lock', 'fixed', 'when', 'match', 'loop', 'defined',
            'assert', 'alignas', 'with', 'import', 'export', 'from', 'constructor_', 'synchronized'}

MODS_CS = r'(?:public|private|protected|internal|static|virtual|override|abstract|async|sealed|extern|unsafe|new|partial|readonly)'
MODS_JAVA = r'(?:public|private|protected|static|final|abstract|synchronized|native|default|strictfp)'
RULES = {
    'python': [
        (re.compile(r'^(?P<ind>\s*)class\s+(?P<name>\w+)'), 'class'),
        (re.compile(r'^(?P<ind>\s*)(?:async\s+)?def\s+(?P<name>\w+)'), 'def'),
    ],
    'ts': [
        (re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?'
                    r'(?P<kind>class|interface|enum|namespace|module)\s+(?P<name>[A-Za-z_$][\w$]*)'), None),
        (re.compile(r'^\s*(?:export\s+)?(?:declare\s+)?type\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^=]*>)?\s*='), 'type'),
        (re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?function\s*\*?\s*'
                    r'(?P<name>[A-Za-z_$][\w$]*)'), 'function'),
        (re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]*)?=\s*'
                    r'(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]*)?=>|\([^)]*$|[A-Za-z_$][\w$]*\s*=>)'), 'function'),
        (re.compile(r'^\s+(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*'
                    r'\*?(?P<name>[A-Za-z_$#][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^{=;]+)?\{\s*$'), 'method'),
    ],
    'c': [
        (re.compile(r'^\s*(?:template\s*<[^>]*>\s*)?(?:typedef\s+)?(?P<kind>class|struct|enum\s+class|enum|union)\s+'
                    r'(?:\w+_API\s+|__declspec\([^)]*\)\s+|alignas\([^)]*\)\s+)*(?P<name>[A-Za-z_]\w*)'
                    r'(?:\s+final)?\s*(?::(?!:)[^;{]*)?(?:\{|$)'), None),
        (re.compile(r'^(?:[\w:<>,\*&~]+[\s\*&]+)+(?P<name>~?[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*|operator\s*\S+?)\s*'
                    r'\('), 'function'),
    ],
    'cs': [
        (re.compile(r'^\s*(?:' + MODS_CS + r'\s+)*(?P<kind>class|struct|interface|enum|record)\s+(?P<name>\w+)'), None),
        (re.compile(r'^\s*(?:' + MODS_CS + r'\s+)+[\w<>\[\],\.\?\s]+?\s+(?P<name>\w+)\s*(?:<[^>]*>)?\s*\((?!.*;\s*$)'),
         'method'),
    ],
    'go': [
        (re.compile(r'^func\s+(?:\([^)]*\)\s*)?(?P<name>\w+)'), 'func'),
        (re.compile(r'^type\s+(?P<name>\w+)\s+(?P<kind>struct|interface)\b'), None),
        (re.compile(r'^type\s+(?P<name>\w+)\b'), 'type'),
    ],
    'rust': [
        (re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe|extern\s+"[^"]*")\s+)*fn\s+(?P<name>\w+)'), 'fn'),
        (re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?P<kind>struct|enum|trait|union|mod|type)\s+(?P<name>\w+)'), None),
        (re.compile(r'^\s*(?:unsafe\s+)?impl(?:<[^>]*>)?\s+(?P<name>[^{]+?)\s*(?:where\b.*)?\{?\s*$'), 'impl'),
    ],
    'java': [
        (re.compile(r'^\s*(?:' + MODS_JAVA + r'\s+|sealed\s+|non-sealed\s+)*(?P<kind>class|interface|enum|record|@interface)\s+'
                    r'(?P<name>\w+)'), None),
        (re.compile(r'^\s*(?:' + MODS_JAVA + r'\s+)+(?:<[^>]*>\s+)?[\w<>\[\],\.\?\s]+?\s+(?P<name>\w+)\s*\((?!.*;\s*$)'),
         'method'),
    ],
    'kotlin': [
        (re.compile(r'^\s*(?:(?:public|private|protected|internal|open|abstract|sealed|data|enum|inner|annotation|value)\s+)*'
                    r'(?P<kind>class|interface|object)\s+(?P<name>\w+)'), None),
        (re.compile(r'^\s*(?:(?:public|private|protected|internal|open|override|abstract|suspend|inline|operator|infix|tailrec)\s+)*'
                    r'fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(?P<name>\w+)'), 'fun'),
    ],
}
HEAD_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def status_re(tag: str) -> re.Pattern:
    """A STATUS(<task>) tag in any common comment style: //, #, --, ;, /* */, <!-- -->."""
    return re.compile(r'(?:^|\s)(?://+|#+|--|;+|/\*+|\*|<!--)\s*' + re.escape(tag) +
                      r'\((?P<task>[^)\s]+)\):\s*(?P<state>[A-Z]+)\b\s*(?:--)?\s*(?P<text>.*?)\s*'
                      r'(?:\[(?P<owner>[^\]]+)\])?\s*(?:\*/|-->)?\s*$')


def md_headings(text: str):
    """[(level, title, start_line)], skipping fenced code blocks."""
    out, fence = [], None
    for i, line in enumerate(text.splitlines()):
        s = line.lstrip()
        if s.startswith(('```', '~~~')):
            mark = s[:3]
            fence = None if fence == mark else (fence or mark)
            continue
        if fence:
            continue
        m = HEAD_RE.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2), i + 1))
    return out


def with_ranges(heads, nlines: int):
    """Add each heading's last line: up to the next heading of the same or a higher level."""
    res = []
    for k, (lv, t, s) in enumerate(heads):
        e = nlines
        for lv2, _, s2 in heads[k + 1:]:
            if lv2 <= lv:
                e = s2 - 1
                break
        res.append((lv, t, s, e))
    return res


def _strip_comment(s: str) -> str:
    s = re.sub(r'^(?:///?|/\*+!?|\*+/?|#+!?|--|<!--)\s*', '', s.strip())
    return re.sub(r'\s*(?:\*+/|-->)\s*$', '', s).strip()


def comment_above(lines, i, lang, srx) -> str:
    prefixes = LINE_COMMENT.get(lang, DEFAULT_COMMENT)
    deco = DECORATOR.get(lang)
    j = i - 1
    while j >= 0 and deco and deco.match(lines[j]):
        j -= 1
    block = []
    while j >= 0 and prefixes:
        s = lines[j].strip()
        if s and s.startswith(prefixes) and not s.startswith('#!'):
            if not srx.search(lines[j]):
                block.append(s)
            j -= 1
        else:
            break
    for raw in reversed(block):        # first line of the block that says something
        t = _strip_comment(raw)
        if t:
            return t[:110]
    if lang == 'python' and i + 1 < len(lines):   # a docstring on the next line
        m = re.match(r'^\s*[rRbBuU]?("""|\'\'\')\s*(.*?)\s*(?:\1)?\s*$', lines[i + 1])
        if m and m.group(2):
            return m.group(2)[:110]
    return ''


def file_purpose(lines, lang) -> str:
    """The first meaningful comment or docstring line in the file head (skips licence and copyright lines)."""
    for raw in lines[:40]:
        s = raw.strip()
        if not s or s.startswith('#!'):
            continue
        if lang == 'python':
            m = re.match(r'^[rRbBuU]?("""|\'\'\')\s*(.*?)\s*(?:\1)?\s*$', s)
            if m:
                if m.group(2):
                    return m.group(2)[:140]
                continue
        if lang == 'md':
            m = HEAD_RE.match(s)
            if m:
                return m.group(2)[:140]
            continue
        prefixes = LINE_COMMENT.get(lang, DEFAULT_COMMENT)
        if s.startswith(prefixes) or (lang == 'python' and s.startswith('#')):
            t = _strip_comment(s)
            if t and not re.match(r'(?i)(copyright|\(c\)|spdx|license|licence|-\*-|=+$|-+$|eslint|@ts-|prettier|'
                                  r'@flow|pylint|noqa|type:)', t):
                return t[:140]
            continue
        if lang == 'c' and s.startswith('#'):
            continue
        if lang in ('python', 'go', 'java', 'kotlin', 'rust', 'cs', 'ts') and re.match(
                r'^(from|import|package|using|use|"use |\'use |#!?\[)', s):
            continue
        if lang == 'python' and s.startswith('#'):
            continue
        break
    return ''


def parse_text(text: str, lang: str, tag: str = 'STATUS', header: bool = False) -> dict:
    """header: a C/C++ header, where column-0 prototypes (`int f(int);`) are the API and are indexed."""
    lines = text.splitlines()
    srx = status_re(tag)
    syms = []
    if lang == 'md':
        for lv, t, s, e in with_ranges(md_headings(text), len(lines)):
            if lv <= 2:
                syms.append([s, f'h{lv}', t[:100], f'[{s}-{e}]'])
    else:
        rules = RULES.get(lang, [])
        stack = []          # python: (indent, kind) of the enclosing class/def blocks
        in_block = False
        for i, line in enumerate(lines):
            m = srx.search(line)
            if m:
                syms.append([i + 1, 'STATUS', m.group('task'),
                             f'{m.group("state")} -- {m.group("text")}' + (f' [{m.group("owner")}]' if m.group('owner') else '')])
                continue
            s = line.strip()
            if lang != 'python':
                if in_block:
                    in_block = '*/' not in s
                    continue
                if s.startswith('/*') and '*/' not in s:
                    in_block = True
                    continue
                if s.startswith(('//', '*', '#')):
                    continue
            elif s.startswith('#'):
                continue
            if not s:
                continue
            for rx, fixed in rules:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group('name').strip()
                base = re.split(r'::|\.', name)[-1].lstrip('~')
                if base in KEYWORDS or not name:
                    break
                kind = fixed or re.sub(r'\s+', ' ', m.group('kind'))
                if lang == 'python':
                    ind = len(m.group('ind').expandtabs(4))
                    while stack and stack[-1][0] >= ind:
                        stack.pop()
                    parent = stack[-1] if stack else None
                    stack.append((ind, kind, name))
                    if parent and parent[1] != 'class':
                        break          # a function nested in a function: not an API
                    if parent:
                        name, kind = f'{parent[2]}.{name}', ('method' if kind == 'def' else kind)
                if lang == 'c' and fixed == 'function' and (
                        re.match(r'^\s', line) or (not header and line.rstrip().endswith(';'))):
                    break              # C/C++: column 0 only; prototypes only in headers
                syms.append([i + 1, kind, name, comment_above(lines, i, lang, srx)])
                break
            else:
                if lang == 'python':
                    ind = len(line) - len(line.lstrip())
                    while stack and stack[-1][0] >= ind:
                        stack.pop()
    return {'lines': len(lines), 'purpose': file_purpose(lines, lang), 'symbols': syms}


def parse_file(path: Path, tag: str) -> dict | None:
    lang = LANG_BY_EXT.get(path.suffix.lower())
    if not lang:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b'\0' in raw[:4096]:
        return None
    return parse_text(raw.decode('utf-8', 'replace'), lang, tag,
                      header=path.suffix.lower() in ('.h', '.hh', '.hpp', '.hxx'))


def render_md(root: Path, index_rel: str, files: dict) -> str:
    total = sum(i['lines'] for i in files.values())
    nsym = sum(len(i['symbols']) for i in files.values())
    toc = Path(__file__).resolve().parent / 'md_toc.py'
    out = ['# Code index', '',
           f'Generated by code-index -- never edit by hand. {len(files)} files, {total} lines, {nsym} entries.',
           f'Use: rg -n "<symbol>" {index_rel} -> Read the source with offset/limit around the line. '
           f'Sections of this file: python "{toc.as_posix()}" {index_rel}', '',
           '## Files', '', '| file | lines | purpose |', '|---|---|---|']
    for rel, info in files.items():
        out.append(f'| {rel} | {info["lines"]} | {info["purpose"].replace("|", "/")[:90]} |')
    out += ['', '## Symbols', '']
    for rel, info in files.items():
        if not info['symbols']:
            continue
        out.append(f'### {rel} ({info["lines"]} lines)')
        for ln, kind, name, summ in info['symbols']:
            if kind == 'STATUS':
                out.append(f'- L{ln} **STATUS({name})** {summ}')
            elif kind.startswith('h') and kind[1:].isdigit():
                out.append(f'- {summ} {"#" * int(kind[1:])} {name}')
            else:
                out.append(f'- L{ln} {kind} `{name}`' + (f' -- {summ}' if summ else ''))
        out.append('')
    return '\n'.join(out) + '\n'


def render_json(files: dict) -> str:
    return json.dumps({'version': INDEX_VERSION, 'generator': 'code-index', 'files': {
        rel: {'lines': i['lines'], 'purpose': i['purpose'],
              'symbols': [{'line': s[0], 'kind': s[1], 'name': s[2], 'summary': s[3]} for s in i['symbols']]}
        for rel, i in files.items()}}, indent=1) + '\n'


def build(root: Path, index_path: Path, include, exclude, tag: str, fmt: str, full: bool = False) -> dict:
    t0 = time.time()
    cache_path = index_path.parent / ('.' + index_path.name + '.cache.json')
    cache = {}
    if not full and cache_path.is_file():
        try:
            c = json.loads(cache_path.read_text(encoding='utf-8'))
            if c.get('version') == INDEX_VERSION and c.get('tag') == tag:
                cache = c.get('files', {})
        except (OSError, ValueError):
            cache = {}
    index_rel = rel_of(root, index_path)
    files, fresh, parsed, cached = {}, {}, 0, 0
    for rel in list_files(root, include, exclude):
        if rel == index_rel:
            continue
        p = root / rel
        try:
            st = p.stat()
        except OSError:
            continue
        key = f'{st.st_size}:{st.st_mtime_ns}'
        hit = cache.get(rel)
        if hit and hit.get('key') == key:
            info, cached = hit['info'], cached + 1
        else:
            info, parsed = parse_file(p, tag), parsed + 1
        if info is None:
            continue
        files[rel] = info
        fresh[rel] = {'key': key, 'info': info}
    index_path.parent.mkdir(parents=True, exist_ok=True)
    text = render_json(files) if fmt == 'json' else render_md(root, index_rel, files)
    index_path.write_text(text, encoding='utf-8')
    try:
        cache_path.write_text(json.dumps({'version': INDEX_VERSION, 'tag': tag, 'files': fresh}), encoding='utf-8')
    except OSError as e:
        print(f'code-index: could not write the cache {cache_path}: {e}', file=sys.stderr)
    return {'index': index_rel, 'files': len(files), 'symbols': sum(len(i['symbols']) for i in files.values()),
            'lines': text.count('\n'), 'parsed': parsed, 'cached': cached, 'seconds': round(time.time() - t0, 2)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', help='project root (default: nearest ancestor with .git or .claude)')
    ap.add_argument('--index-path', help='where to write the index (.md, or .json for JSON)')
    ap.add_argument('--include', help='comma-separated globs of files to index')
    ap.add_argument('--exclude', help='comma-separated globs to skip')
    ap.add_argument('--status-tag', help='tag name of task comments (default STATUS)')
    ap.add_argument('--format', choices=('md', 'json'), help='default: from the index file extension')
    ap.add_argument('--full', action='store_true', help='ignore the cache and reparse every file')
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root).resolve() if a.root else find_root()
    index_path = Path(resolve('indexPath', a.index_path, root))
    if not index_path.is_absolute():
        index_path = root / index_path
    fmt = a.format or ('json' if index_path.suffix.lower() == '.json' else 'md')
    r = build(root, index_path, resolve('include', a.include, root), resolve('exclude', a.exclude, root),
              resolve('statusTag', a.status_tag, root), fmt, a.full)
    print(f'{r["index"]}: {r["files"]} files, {r["symbols"]} entries, {r["lines"]} lines '
          f'(parsed {r["parsed"]}, cached {r["cached"]}, {r["seconds"]}s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
