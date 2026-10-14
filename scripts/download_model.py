"""Download a Gemma 2 2B instruction-tuned GGUF into models/.

Requires `huggingface_hub` (pip install huggingface_hub). Gemma is released
under Google's Gemma Terms of Use; you may need to accept them on Hugging Face
and run `huggingface-cli login` first.

Usage:
    python scripts/download_model.py
    python scripts/download_model.py --repo bartowski/gemma-2-2b-it-GGUF --file gemma-2-2b-it-Q4_K_M.gguf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "bartowski/gemma-2-2b-it-GGUF"
DEFAULT_FILE = "gemma-2-2b-it-Q4_K_M.gguf"  # ~1.7 GB, good quality/speed balance on CPU


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--file", default=DEFAULT_FILE)
    args = parser.parse_args()
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("huggingface_hub is not installed. Either run:\n  pip install huggingface_hub\n"
              f"or download {args.file} manually from https://huggingface.co/{args.repo} into {ROOT / 'models'}")
        return 1
    path = hf_hub_download(repo_id=args.repo, filename=args.file, local_dir=ROOT / "models")
    print(f"Downloaded to {path}\nVerify with:  python -m aplos check-model --generate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
