import sys
from pathlib import Path

# bridge.py lives at the repo root, not in an installable package — make it
# importable as `import bridge` from tests/ without needing a setup.py.
sys.path.insert(0, str(Path(__file__).parent))
