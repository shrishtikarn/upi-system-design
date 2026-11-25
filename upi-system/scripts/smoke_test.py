"""End-to-end smoke test: python scripts/smoke_test.py [--wait 120]

Thin wrapper around `scripts/demo.py smoke` (kept as its own file for CI).
Exit code 0 = every check passed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from demo import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "smoke", *sys.argv[1:]]
    sys.exit(main())
