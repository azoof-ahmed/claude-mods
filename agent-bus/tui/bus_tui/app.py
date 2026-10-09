"""Entry point: main(root, role, theme) -> exit code. Textual is optional for the plugin: without it, exit 2."""
import os
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[2] / 'vendor'


def find_bus_dir(start=None):
    """AGENT_BUS_DIR, else .claude/bus under the nearest ancestor holding .git or .claude (as the plugin does)."""
    env = os.environ.get('AGENT_BUS_DIR')
    if env:
        return Path(env)
    here = Path(start or os.getcwd()).resolve()
    root = here
    for d in [here, *here.parents]:
        if (d / '.git').exists() or (d / '.claude').is_dir():
            root = d
            break
    return root / '.claude' / 'bus'


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
