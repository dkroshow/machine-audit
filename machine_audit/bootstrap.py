"""Run an installed source snapshot with an explicit, isolated Python runtime."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from machine_audit.cli import main

raise SystemExit(main())
