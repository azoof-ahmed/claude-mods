"""The TOML the roles file needs, on any Python >= 3.9.

Uses tomllib (3.11+) or tomli when present. Otherwise a small parser for the subset the roles file uses: comments,
[table] headers, and `key = value` where value is a string, an integer, true/false, or a one-line array of those.
"""
from __future__ import annotations

import ast
import re

try:  # pragma: no cover - depends on the interpreter
    import tomllib as _toml
except ImportError:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore
    except ImportError:
        _toml = None


class TomlError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
            out.append(ch)
        elif ch == '#':
            break
        else:
            out.append(ch)
    return ''.join(out).strip()


def _value(text: str, lineno: int):
    t = text.strip()
    if t in ('true', 'false'):
        return t == 'true'
    if re.fullmatch(r'[+-]?\d+', t):
        return int(t)
    if t.startswith('[') and t.endswith(']'):
        inner = t[1:-1].strip()
        if not inner:
            return []
        parts, cur, quote = [], [], None
        for ch in inner:
            if quote:
                cur.append(ch)
                if ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
                cur.append(ch)
            elif ch == ',':
                parts.append(''.join(cur))
                cur = []
            else:
                cur.append(ch)
        if ''.join(cur).strip():
            parts.append(''.join(cur))
        return [_value(p, lineno) for p in parts]
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        return ast.literal_eval(t) if t[0] == '"' else t[1:-1]
    raise TomlError(f'line {lineno}: unsupported value {t!r}')


def parse_simple(text: str) -> dict:
    root: dict = {}
    table = root
    for i, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        m = re.fullmatch(r'\[\s*([\w.-]+)\s*\]', line)
        if m:
            table = root
            for part in m.group(1).split('.'):
                table = table.setdefault(part, {})
            continue
        m = re.fullmatch(r'([\w-]+|"[^"]+")\s*=\s*(.+)', line)
        if not m:
            raise TomlError(f'line {i}: cannot parse {raw.strip()!r}')
        table[m.group(1).strip('"')] = _value(m.group(2), i)
    return root


def loads(text: str) -> dict:
    if _toml is not None:
        try:
            return _toml.loads(text)
        except Exception as e:  # tomllib.TOMLDecodeError
            raise TomlError(str(e))
    return parse_simple(text)
