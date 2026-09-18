#!/usr/bin/env python3
"""Start Billie.

    python run.py

Adds backend/ to the import path so the package can be run without installing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from billie.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
