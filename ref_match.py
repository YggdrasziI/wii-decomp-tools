#!/usr/bin/env python3
"""Find this game's functions in another dtk project built from the same libraries.

Both projects' dtk assembly (build/<ID>/asm, after `ninja`) is read. Each function is
reduced to its instruction text with symbol operands replaced by a placeholder and
local branch targets turned into offsets from the function start, so the same code
at another address gives the same fingerprint. A function of this game matches a
reference function when their fingerprints are equal and the fingerprint is unique
on both sides; short functions (under --min-size bytes) are skipped because they
collide too easily.

Output: one line per run of consecutive matched functions that fall in the same
reference unit (from the reference splits.txt), with this game's address range, the
number of functions matched and the reference unit name. Use --symbols to also list
each matched function with its reference name. The result is evidence to check, not
a split: compare the neighbours and data before writing splits.txt.

Usage:
  python tools/wii/ref_match.py path/to/other-decomp <ID>
  python tools/wii/ref_match.py path/to/other-decomp <ID> --symbols
"""

import argparse
import glob
import hashlib
import os
import bisect
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wii_config  # noqa: E402

ROOT = str(wii_config.ROOT)
DEFAULT_VERSION = wii_config.DEFAULT_VERSION
VERSIONS = wii_config.VERSIONS

FN_RE = re.compile(r"^\.fn (\S+), \w+\n(.*?)^\.endfn \1$", re.S | re.M)
INSN_RE = re.compile(r"^/\* ([0-9A-F]{8}) [0-9A-F]{8}  (?:[0-9A-F]{2} ){4}\*/\t(.*)$")
LABEL_RE = re.compile(r"\.L_([0-9A-F]{8})")
# Operands that name a symbol: name@ha / @l / @h / @sda21 (the name may be quoted, as
# in "@1_804F2108"@ha), or the target of a branch to another function.
SYM_RE = re.compile(r'(?:"[^"]+"|[A-Za-z_.$@?][\w.$@?<>,\\-]*)@(?:ha|h|l|sda21)')
BRANCH_RE = re.compile(r"^(b|bl)\s+(?!\.L_)(\S+)$")
# Runs of matches in the reference's unsplit code are cut at gaps larger than this.
MAX_GAP = 0x1000


def functions(asm_dir):
    """{address: (name, size, fingerprint)} for every function of a dtk asm tree.

    Keyed by address: dtk leaves the .s files of earlier splits in place, so the
    same function may appear in several files.
    """
    out = {}
    paths = glob.glob(os.path.join(asm_dir, "**", "*.s"), recursive=True)
    if not paths:
        sys.exit(f"no assembly in {asm_dir}: run ninja in that project first")
    for path in paths:
        text = open(path, encoding="utf-8").read()
        for m in FN_RE.finditer(text):
            name, body = m.group(1), m.group(2)
            insns = [INSN_RE.match(line) for line in body.split("\n")]
            insns = [i for i in insns if i]
            if not insns:
                continue
            start = int(insns[0].group(1), 16)
            norm = []
            for i in insns:
                op = i.group(2).strip()
                op = BRANCH_RE.sub(r"\1 SYM", op)
                op = SYM_RE.sub("SYM", op)
                op = LABEL_RE.sub(lambda l: f"L{int(l.group(1), 16) - start:+x}", op)
                norm.append(op)
            digest = hashlib.sha1("\n".join(norm).encode()).hexdigest()
            out[start] = (name, 4 * len(insns), digest)
    return out


def unit_ranges(splits_path):
    """[(start, end, unit)] of the .text ranges in a splits.txt."""
    ranges, unit = [], None
    for line in open(splits_path, encoding="utf-8"):
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            unit = line.rstrip()[:-1]
            continue
        m = re.match(r"\s+\.(?:text|init)\s+start:(0x[0-9A-Fa-f]+) end:(0x[0-9A-Fa-f]+)", line)
        if m and unit and unit != "Sections":
            ranges.append((int(m.group(1), 16), int(m.group(2), 16), unit))
    return sorted(ranges)


def unit_of(ranges, starts, addr):
    i = bisect.bisect_right(starts, addr) - 1
    if i >= 0 and addr < ranges[i][1]:
        return ranges[i][2]
    return None


def match_functions(ours, theirs, min_size):
    """[(our address, our name, their name, their address)] of the functions whose
    fingerprint is unique on both sides, sorted by our address."""
    ours_count = Counter(d for _, size, d in ours.values() if size >= min_size)
    theirs_count = Counter(d for _, size, d in theirs.values() if size >= min_size)
    by_digest = {d: (name, addr) for addr, (name, size, d) in theirs.items()
                 if size >= min_size and theirs_count[d] == 1}
    matches = []
    for addr, (name, size, d) in ours.items():
        if size < min_size or ours_count[d] != 1 or d not in by_digest:
            continue
        ref_name, ref_addr = by_digest[d]
        matches.append((addr, name, ref_name, ref_addr))
    return sorted(matches)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ref_root", help="root of the reference dtk project")
    parser.add_argument("ref_version", help="game ID of the reference project, e.g. RM7P4F")
    parser.add_argument("--version", default=DEFAULT_VERSION, type=str.upper,
                        choices=VERSIONS, help="this game's version (default: %(default)s)")
    parser.add_argument("--min-size", type=lambda v: int(v, 0), default=0x20,
                        help="smallest function size compared (default: 0x20)")
    parser.add_argument("--symbols", action="store_true", help="list every matched function")
    args = parser.parse_args()

    ours = functions(os.path.join(ROOT, "build", args.version, "asm"))
    theirs = functions(os.path.join(args.ref_root, "build", args.ref_version, "asm"))
    ranges = unit_ranges(os.path.join(args.ref_root, "config", args.ref_version, "splits.txt"))
    starts = [r[0] for r in ranges]

    # (our address, our name, their name, their address, their unit)
    matches = [m + (unit_of(ranges, starts, m[3]),)
               for m in match_functions(ours, theirs, args.min_size)]

    runs = []  # [first, last, unit, matches]
    for m in matches:
        unit = m[4]
        same = runs and runs[-1][2] == unit
        if same and unit is None:
            # Unsplit reference code: keep a run only while both sides stay close.
            prev = runs[-1][3][-1]
            same = m[0] - prev[0] <= MAX_GAP and 0 < m[3] - prev[3] <= MAX_GAP
        if same:
            runs[-1][1] = m[0]
            runs[-1][3].append(m)
        else:
            runs.append([m[0], m[0], unit, [m]])
    for first, last, unit, run in runs:
        label = unit or f"(not split, reference 0x{run[0][3]:08X}-0x{run[-1][3]:08X})"
        print(f"0x{first:08X}-0x{last:08X}  {len(run):4d}  {label}")
        if args.symbols:
            for addr, name, ref_name, _, _ in run:
                print(f"    0x{addr:08X} {name} = {ref_name}")
    print(f"{len(matches)} functions matched")


if __name__ == "__main__":
    main()
