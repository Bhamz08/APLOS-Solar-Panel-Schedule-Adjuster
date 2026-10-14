"""Run the pipeline immediately (same as `python -m aplos run-now`)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aplos.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["run-now", *sys.argv[1:]]))
