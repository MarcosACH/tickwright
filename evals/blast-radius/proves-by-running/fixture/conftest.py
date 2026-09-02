import sys
from pathlib import Path

# No packaging in this fixture on purpose — it is a throwaway sandbox tree, not
# a real project. This is the only thing standing in for `pip install -e .`.
sys.path.insert(0, str(Path(__file__).parent / "src"))
