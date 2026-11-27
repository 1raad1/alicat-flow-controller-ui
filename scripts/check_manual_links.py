"""Check the code references in docs/BAYESIAN_OPTIMISER_MANUAL.md.

Every reference of the form [`file.py:N`](../path/file.py#LN) must name the
same line in its text and its anchor, and that line must exist and not be
blank. Use --list to print each reference with the code line it points at,
for reviewing that it is the statement the manual describes.

    python scripts/check_manual_links.py [--list]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "docs" / "BAYESIAN_OPTIMISER_MANUAL.md"
REFERENCE = re.compile(r"\[`([\w/.]+\.py):(\d+)`\]\(\.\./([\w/.]+\.py)#L(\d+)\)")


def main(argv):
    listing = "--list" in argv
    sources, total, bad = {}, 0, 0
    for number, line in enumerate(MANUAL.read_text(encoding="utf-8").splitlines(), 1):
        for match in REFERENCE.finditer(line):
            total += 1
            name, shown, path, anchor = (match.group(1), int(match.group(2)),
                                         match.group(3), int(match.group(4)))
            source = ROOT / path
            if path not in sources:
                sources[path] = (source.read_text(encoding="utf-8").splitlines()
                                 if source.is_file() else None)
            code = sources[path]
            problem = None
            if code is None:
                problem = "file not found"
            elif shown != anchor:
                problem = f"text says {shown}, link says {anchor}"
            elif not path.endswith(name):
                problem = "text and link name different files"
            elif not 1 <= anchor <= len(code):
                problem = "line out of range"
            elif not code[anchor - 1].strip():
                problem = "blank line"
            if problem:
                bad += 1
                print(f"manual:{number}: {path}:{anchor}: {problem}")
            elif listing:
                print(f"manual:{number}: {name}:{anchor}: {code[anchor - 1].strip()[:100]}")
    print(f"{total} references, {bad} invalid")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
