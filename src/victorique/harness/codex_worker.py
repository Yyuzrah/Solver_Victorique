"""Private fixed helper entrypoint; not a user-facing command or model agent."""
from pathlib import Path
import importlib
import sys


def main():
    if len(sys.argv) != 4:
        return 64
    # Work both under the source 'harness' package and a standalone renamed
    # 'victorique_harness' distribution, importing only this package's codex_process.
    package = Path(__file__).resolve().parent
    sys.path.insert(0, str(package.parent))
    module = importlib.import_module(package.name + ".codex_process")
    return module._run_worker(sys.argv[1], sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    raise SystemExit(main())
