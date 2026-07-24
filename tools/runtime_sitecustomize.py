"""Add the packaged application directory to isolated Embedded Python."""
from __future__ import annotations

import os
import sys
from pathlib import Path


configured = os.environ.get("MRA_APP_ROOT")
if configured:
    app_root = str(Path(configured).expanduser().resolve())
    if app_root not in sys.path:
        sys.path.insert(0, app_root)
