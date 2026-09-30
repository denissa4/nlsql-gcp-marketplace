#!/usr/bin/env python3
"""Check listing texts against AWS Marketplace form limits.

Usage: listing-check.py <path>:<max characters> ...   (0 = no length limit)

Each file must stay within its limit and use only printable ASCII (32-126)
plus tab and newlines, which every Marketplace text field accepts. A <commit>
placeholder is counted as the 40-character SHA that replaces it.
"""
import sys

ALLOWED_CONTROL = {"\t", "\n", "\r"}
COMMIT_PLACEHOLDER = "<commit>"


def check(path, limit):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    counted = len(text.replace(COMMIT_PLACEHOLDER, "0" * 40))
    bad = sorted({c for c in text if not (32 <= ord(c) <= 126) and c not in ALLOWED_CONTROL})
    problems = []
    if limit and counted > limit:
        problems.append(f"too long, limit {limit}")
    if bad:
        problems.append("characters outside ASCII 32-126: " + ", ".join(f"U+{ord(c):04X}" for c in bad))
    shown = f"{counted} of {limit}" if limit else str(counted)
    print(f"  {path}: {shown} characters - {'; '.join(problems) or 'ok'}")
    return not problems


def main(args):
    if not args:
        print(__doc__)
        return 2
    ok = True
    for arg in args:
        path, _, limit = arg.rpartition(":")
        ok = check(path, int(limit)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
