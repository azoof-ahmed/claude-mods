import sys
from pathlib import Path

_TUI = Path(__file__).resolve().parents[1]
for p in (_TUI, _TUI.parent / 'vendor'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
