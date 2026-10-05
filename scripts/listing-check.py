#!/usr/bin/env python3
"""Check listing texts against AWS Marketplace form limits.

Usage: listing-check.py <path>:<max characters> ...   (0 = no length limit)
       listing-check.py <path>:keywords                (one search keyword per line)

Each file must stay within its limit and use only printable ASCII (32-126)
plus tab and newlines, which every Marketplace text field accepts. A <commit>
placeholder is counted as the 40-character SHA that replaces it. A keywords file
holds 1 to 15 keywords of at most 50 characters each, 250 combined.
"""
import sys

ALLOWED_CONTROL = {"\t", "\n", "\r"}
COMMIT_PLACEHOLDER = "<commit>"


def check(path, limit):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # The file's final newline is not part of what is pasted into the field.
    if text.endswith("\n"):
        text = text[:-1]
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


def check_keywords(path):
    with open(path, encoding="utf-8") as fh:
        keywords = [line.strip() for line in fh if line.strip()]
    problems = []
    if not 1 <= len(keywords) <= 15:
        problems.append(f"{len(keywords)} keywords, allowed 1-15")
    long_ones = [k for k in keywords if len(k) > 50]
    if long_ones:
        problems.append("longer than 50 characters: " + ", ".join(long_ones))
    combined = sum(len(k) for k in keywords)
    if combined > 250:
        problems.append(f"{combined} characters combined, limit 250")
    bad = sorted({c for k in keywords for c in k if not 32 <= ord(c) <= 126})
    if bad:
        problems.append("characters outside ASCII 32-126: " + ", ".join(f"U+{ord(c):04X}" for c in bad))
    print(f"  {path}: {len(keywords)} keywords, {combined} characters combined - {'; '.join(problems) or 'ok'}")
    return not problems


def main(args):
    if not args:
        print(__doc__)
        return 2
    ok = True
    for arg in args:
        path, _, limit = arg.rpartition(":")
        if limit == "keywords":
            ok = check_keywords(path) and ok
        else:
            ok = check(path, int(limit)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
