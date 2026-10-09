"""STATUS tags: one greppable comment line that says where a task stands at that spot in the code, so the next
agent reads the tag instead of the whole file.

Format (one line, its own comment; `//`, `#`, `--`, `;`, `/* */` or `<!-- -->`):
    // STATUS(<task-id>): DONE|PARTIAL|OPEN|FROZEN -- <one line> [owner]
  DONE    finished for that task; nothing more to do here
  PARTIAL started; the line says what is left
  OPEN    not started / still to do
  FROZEN  do not touch (another task or person owns it)
Tags are temporary: strip them when the task closes. The tag name is configurable (--tag, CODE_INDEX_STATUS_TAG,
or "statusTag" in .claude/claude-mods.json "code-index").

  python status_tags.py list [--task X] [--state S] [--path P] [TEXT]   every tag as file:line
  python status_tags.py summary [--path P]                             counts per task and state
  python status_tags.py strip --task X [--dry-run] [--path P]          remove every tag of task X (exact id)
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import find_root, list_files, rel_of, resolve, utf8_stdout  # noqa: E402
from build_code_index import status_re  # noqa: E402

STATES = ('DONE', 'PARTIAL', 'OPEN', 'FROZEN')


def scan(root: Path, tag: str, include, exclude, under: str | None = None):
    """Yield (rel, line_no, match) for every tag line."""
    rx = status_re(tag)
    needle = (tag + '(').encode()
    prefix = rel_of(root, under).rstrip('/') if under else None
    for rel in list_files(root, include, exclude):
        if prefix and not (rel == prefix or rel.startswith(prefix + '/')):
            continue
        try:
            raw = (root / rel).read_bytes()
        except OSError:
            continue
        if needle not in raw or b'\0' in raw[:4096]:
            continue
        for i, line in enumerate(raw.decode('utf-8', 'replace').splitlines()):
            m = rx.search(line)
            if m and m.group('state') in STATES + ('TODO',):
                yield rel, i + 1, m


def fmt(rel: str, ln: int, m, tag: str) -> str:
    return (f'{rel}:{ln}: {tag}({m["task"]}) {m["state"]} -- {m["text"]}'
            + (f' [{m["owner"]}]' if m['owner'] else ''))


def strip(root: Path, tag: str, task: str, include, exclude, under=None, dry_run=False) -> dict:
    """Remove every line that is exactly a tag of `task`; returns {rel: [line numbers]}."""
    rx = status_re(tag)
    hits = {}
    for rel, ln, m in scan(root, tag, include, exclude, under):
        if m['task'] == task:
            hits.setdefault(rel, []).append(ln)
    for rel, lns in hits.items():
        p = root / rel
        raw = p.read_bytes()
        nl = b'\r\n' if b'\r\n' in raw else b'\n'
        lines = raw.split(nl)
        drop = {n - 1 for n in lns}
        for n in drop:   # only ever remove a line whose whole content is a tag of this task
            line = lines[n].decode('utf-8', 'replace')
            m = rx.search(line)
            if not (m and m['task'] == task and line[:m.start()].strip() == ''):
                raise SystemExit(f'status_tags: refusing to strip {rel}:{n + 1}: the tag shares its line with code')
        if not dry_run:
            p.write_bytes(nl.join(l for k, l in enumerate(lines) if k not in drop))
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root')
    ap.add_argument('--tag', help='tag name (default STATUS)')
    ap.add_argument('--include')
    ap.add_argument('--exclude')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('list')
    s.add_argument('--task')
    s.add_argument('--state', choices=STATES + ('TODO',))
    s.add_argument('--path')
    s.add_argument('text', nargs='?', help='substring of the tag line')
    s = sub.add_parser('summary')
    s.add_argument('--path')
    s = sub.add_parser('strip')
    s.add_argument('--task', required=True, help='exact task id; every tag of that task is removed')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--path')
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root).resolve() if a.root else find_root()
    tag = resolve('statusTag', a.tag, root)
    inc, exc = resolve('include', a.include, root), resolve('exclude', a.exclude, root)

    if a.cmd == 'list':
        n = 0
        for rel, ln, m in scan(root, tag, inc, exc, a.path):
            if (a.task and m['task'] != a.task) or (a.state and m['state'] != a.state):
                continue
            line = fmt(rel, ln, m, tag)
            if a.text and a.text.lower() not in line.lower():
                continue
            print(line)
            n += 1
        print(f'({n} tags)', file=sys.stderr)
    elif a.cmd == 'summary':
        c = Counter((m['task'], m['state']) for _, _, m in scan(root, tag, inc, exc, a.path))
        for (t, st), k in sorted(c.items()):
            print(f'{t:<24} {st:<8} {k}')
        print(f'({sum(c.values())} tags)', file=sys.stderr)
    else:
        hits = strip(root, tag, a.task, inc, exc, a.path, a.dry_run)
        for rel, lns in sorted(hits.items()):
            print(f'{rel}: {len(lns)} tag(s) at {sorted(lns)}' + (' (dry run)' if a.dry_run else ''))
        print(f'{sum(map(len, hits.values()))} tag(s) of {a.task} in {len(hits)} file(s)'
              + (' -- dry run, nothing changed' if a.dry_run else ' removed'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
