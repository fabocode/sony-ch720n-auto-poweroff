import sys
from pathlib import Path

# The project is a set of scripts (no package), so put its root on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
