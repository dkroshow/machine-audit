"""Run the repository's synthetic checks sequentially; stop on the first failure."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    if sys.version_info < (3, 12):
        raise SystemExit("Run this command with Python 3.12 or newer.")

    root = Path(__file__).resolve().parent.parent
    python = sys.executable
    env = dict(os.environ, MACHINE_AUDIT_PYTHON=python)
    commands = [
        [python, "-m", "unittest", "discover", "-s", f"tests/{suite}", "-v"]
        for suite in ("core", "sources", "scanner", "onboarding")
    ]
    commands += [
        [python, "-m", "unittest", "replay.test_replay", "replay.test_storage_replay", "-v"],
        [python, "-m", "compileall", "-q", "machine_audit"],
        ["node", "--test", *map(str, sorted((root / "dashboard/test").glob("*.test.mjs")))],
        ["git", "diff", "--check"],
    ]
    for command in commands:
        print("Running:", " ".join(command), flush=True)
        result = subprocess.run(command, cwd=root, env=env)
        if result.returncode:
            return result.returncode if result.returncode > 0 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
