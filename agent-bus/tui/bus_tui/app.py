"""Entry point: main(root, role, theme) -> exit code. Textual is optional for the plugin: without it, exit 2."""
import os
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[2] / 'vendor'
SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'


def find_bus_dir(start=None):
    """The bus of the project `start` is in, found as bus.py finds it."""
    sys.path.insert(0, str(SCRIPTS))
    import bus
    return Path(bus.load_config({}, start=start)['dir'])


def main(root, role=None, theme=None) -> int:
    try:
        import textual  # noqa: F401
    except ImportError:
        print('bus-tui needs Textual: pip install textual', file=sys.stderr)
        return 2
    try:
        import orca_bus  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(VENDOR))
    from . import themes
    from .ui import BusTui
    if theme and not themes.resolve(theme):
        print(f'bus-tui: no theme {theme!r} (choose from {", ".join(themes.PALETTES)})', file=sys.stderr)
        return 2
    root = Path(root or find_bus_dir())
    if not root.is_dir():
        print(f'bus-tui: no bus at {root} (create it with the bus `init` verb; a bus elsewhere: set "busDir" under "agent-bus" in .claude/claude-mods.json)', file=sys.stderr)
        return 2
    role = role or os.environ.get('AGENT_BUS_ROLE') or 'user'
    BusTui(str(root), role, theme).run()
    return 0
