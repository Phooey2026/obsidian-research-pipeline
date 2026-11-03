#!/usr/bin/env python3
"""
searxng_engine_cleanup.py — turn off SearXNG's scraped engines that are always CAPTCHA'd
or suspended from this IP, and turn on a tolerant one.

Nov 3 2026: probes show brave, duckduckgo, startpage and yahoo come back "unresponsive"
on every query (too many requests / CAPTCHA / parsing error) while bing, braveapi and
"duckduckgo web" carry the load. Leaving the dead engines on wastes every query's time
budget and is a likely cause of the "restart SearXNG before any search works" problem.

Usage (works on a copy, never edits the container's file in place):
    docker cp searxng:/etc/searxng/settings.yml ./searxng_settings.orig.yml
    cp searxng_settings.orig.yml searxng_settings.new.yml
    python3 searxng_engine_cleanup.py searxng_settings.new.yml --dry-run     # show the plan
    python3 searxng_engine_cleanup.py searxng_settings.new.yml               # apply
    docker cp searxng_settings.new.yml searxng:/etc/searxng/settings.yml
    docker restart searxng
Undo: docker cp searxng_settings.orig.yml searxng:/etc/searxng/settings.yml && docker restart searxng
"""
import re, sys

DISABLE = ["brave", "duckduckgo", "startpage", "yahoo"]     # exact engine names
ENABLE = ["mojeek"]                                          # tolerant general engine


def process(lines, names, value):
    """Set `disabled: <value>` inside each engine block whose name matches exactly."""
    changes, i = [], 0
    n = len(lines)
    while i < n:
        m = re.match(r"^(\s*)- name:\s*(.+?)\s*$", lines[i])
        if m and m.group(2).strip("'\"") in names:
            indent, name = m.group(1), m.group(2).strip("'\"")
            j = i + 1
            while j < n and not re.match(r"^\s*- name:\s", lines[j]) and not re.match(r"^\S", lines[j]):
                j += 1
            block = range(i + 1, j)
            hit = next((k for k in block if re.match(r"^\s+disabled:\s*", lines[k])), None)
            want = "true" if value else "false"
            if hit is not None:
                cur = re.sub(r"^\s+disabled:\s*(\S+).*$", r"\1", lines[hit].rstrip("\n")).lower()
                if cur != want:
                    lines[hit] = re.sub(r"(disabled:\s*)\S+", r"\g<1>" + want, lines[hit], count=1)
                    changes.append(f"{name}: disabled {cur} -> {want}")
                else:
                    changes.append(f"{name}: already disabled: {want}")
            else:
                lines.insert(i + 1, f"{indent}  disabled: {want}\n")
                n += 1
                changes.append(f"{name}: added disabled: {want}")
            i = j
            continue
        i += 1
    return changes


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        sys.exit(__doc__)
    path = args[0]
    lines = open(path, encoding="utf-8").readlines()
    found = {re.match(r"^\s*- name:\s*(.+?)\s*$", l).group(1).strip("'\"")
             for l in lines if re.match(r"^\s*- name:\s*(.+?)\s*$", l)}
    missing = [x for x in DISABLE + ENABLE if x not in found]
    out = process(lines, set(DISABLE), True) + process(lines, set(ENABLE), False)
    for c in out:
        print("  " + c)
    if missing:
        print("  NOT FOUND in the file (no change possible):", ", ".join(missing))
    if dry:
        print("(dry run — file not written)")
        return
    open(path, "w", encoding="utf-8").writelines(lines)
    print(f"written: {path}")


if __name__ == "__main__":
    main()
