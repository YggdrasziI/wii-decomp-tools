#!/usr/bin/env python3
"""Compile a test source with every mwcc version and compare functions to the DOL.

Each pair maps a symbol of the compiled test object to a function of the original
DOL (its symbols.txt name). Instruction words are compared one by one; fields
covered by a relocation in the test object are masked on both sides and trailing
zero padding is ignored. This is a quick way to tell compiler versions and flags
apart on small functions before writing a unit; only a linked unit proves anything.

The flags are those build.ninja gives to a game unit (the first one, or --unit),
so the game version is the one configure.py was last run for; --flags adds more
(later flags win). Keep test sources in the session scratchpad, not in the
repository.

Usage:
  python tools/wii/compiler_sweep.py test.cpp f__1AFv=fn_80012345
  python tools/wii/compiler_sweep.py --flags "-opt nopeephole" test.cpp f__1AFv=fn_80012345
  python tools/wii/compiler_sweep.py --unit unit_80012300 test.cpp f__1AFv=fn_80012345
  python tools/wii/compiler_sweep.py --compilers GC/3.0a5.2,Wii/1.0 test.cpp f__1AFv=fn_80012345
"""

import argparse
import glob
import os
import re
import shlex
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wii_config  # noqa: E402

ROOT = str(wii_config.ROOT)
COMPILERS = os.path.join(ROOT, "build", "compilers")
OBJDUMP = os.path.join(ROOT, "build", "binutils", "powerpc-eabi-objdump.exe")
# Instruction bits filled by each relocation type; other types fill the low half.
RELOC_MASKS = {
    "R_PPC_REL24": 0x03FFFFFC,
    "R_PPC_REL14": 0x0000FFFC,
    "R_PPC_EMB_SDA21": 0x001FFFFF,  # the linker also rewrites rA (r0 -> r13 / r2)
}
HALF_MASK = 0x0000FFFF


def game_unit_flags(unit):
    """(version, mw_version, cflags) of a game unit in build.ninja."""
    path = os.path.join(ROOT, "build.ninja")
    if not os.path.exists(path):
        sys.exit("build.ninja not found: run python configure.py first")
    text = open(path, encoding="utf-8").read().replace("$\n", "")
    name = re.escape(unit) if unit else r"[^\s:]+"
    m = re.search(r"^build build[\\/](\w+)[\\/]src[\\/]game[\\/]" + name +
                  r"\.o: mwcc\S*.*?\n((?:  .*\n)+)", text, re.M)
    if not m:
        sys.exit(f"no game unit {unit or ''} in build.ninja")
    variables = dict(re.findall(r"^\s+(\w+) = (.*)$", m.group(2), re.M))
    return m.group(1), variables["mw_version"].replace("\\", "/"), shlex.split(variables["cflags"])


def read_dol(path):
    data = open(path, "rb").read()
    offsets = struct.unpack(">18I", data[0:72])
    addrs = struct.unpack(">18I", data[72:144])
    sizes = struct.unpack(">18I", data[144:216])

    def read_words(addr, size):
        for off, start, length in zip(offsets, addrs, sizes):
            if start <= addr and addr + size <= start + length:
                raw = data[off + addr - start:off + addr - start + size]
                return list(struct.unpack(f">{size // 4}I", raw))
        sys.exit(f"0x{addr:08X} is not in a DOL section")

    return read_words


def original_functions(version):
    symbols = open(os.path.join(ROOT, "config", version, "symbols.txt"), encoding="utf-8").read()
    funcs = {}
    for m in re.finditer(r"^(\S+) = \.\w+:(0x[0-9A-Fa-f]+); // type:function size:(0x[0-9A-Fa-f]+)",
                         symbols, re.M):
        funcs[m.group(1)] = (int(m.group(2), 16), int(m.group(3), 16))
    return funcs


def compiled_functions(obj):
    """{symbol: (words, relocation masks)} for every function of an object file."""
    out = subprocess.run([OBJDUMP, "-dr", obj], capture_output=True, text=True, check=True).stdout
    funcs, current = {}, None
    for line in out.split("\n"):
        header = re.match(r"^[0-9a-f]+ <(.+)>:$", line)
        if header:
            current = funcs.setdefault(header.group(1), ([], []))
            continue
        if current is None:
            continue
        words, masks = current
        reloc = re.search(r"\b(R_PPC_\w+)\b", line)
        if reloc:
            if masks:
                masks[-1] |= RELOC_MASKS.get(reloc.group(1), HALF_MASK)
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{2}( [0-9a-f]{2}){3}", parts[1].strip()):
            words.append(int(parts[1].replace(" ", ""), 16))
            masks.append(0)
    for words, masks in funcs.values():
        while words and words[-1] == 0:
            words.pop()
            masks.pop()
    return funcs


def compare(ours, original):
    words, masks = ours
    if len(words) != len(original):
        return f"len {len(words)}/{len(original)}"
    bad = sum(1 for w, o, k in zip(words, original, masks) if (w & ~k) != (o & ~k))
    return "OK" if bad == 0 else f"{bad} diff"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="test C/C++ file")
    parser.add_argument("pairs", nargs="+", metavar="ours=original",
                        help="test-object symbol = symbols.txt function name")
    parser.add_argument("--flags", default="", help="extra compiler flags")
    parser.add_argument("--unit", default="", help="game unit whose flags to use (default: the first)")
    parser.add_argument("--compilers", default="",
                        help="comma-separated list such as GC/3.0a5.2,Wii/1.0 (default: all)")
    args = parser.parse_args()

    pairs = []
    for pair in args.pairs:
        if "=" not in pair:
            parser.error(f"expected ours=original, got {pair!r}")
        pairs.append(tuple(pair.split("=", 1)))

    version, unit_compiler, cflags = game_unit_flags(args.unit)
    cflags += shlex.split(args.flags)
    print(f"version {version}, flags of {args.unit or 'the first game unit'} "
          f"(built with {unit_compiler}) + {args.flags or 'nothing'}")

    funcs = original_functions(version)
    read_words = read_dol(os.path.join(ROOT, "orig", version, "sys", "main.dol"))
    originals = {}
    for _, name in pairs:
        if name not in funcs:
            sys.exit(f"{name} is not a function in config/{version}/symbols.txt")
        originals[name] = read_words(*funcs[name])

    if args.compilers:
        tags = [t.strip() for t in args.compilers.split(",") if t.strip()]
    else:
        tags = [os.path.relpath(os.path.dirname(exe), COMPILERS).replace(os.sep, "/")
                for family in ("GC", "Wii")
                for exe in sorted(glob.glob(os.path.join(COMPILERS, family, "*", "mwcceppc.exe")))]

    source = os.path.abspath(args.source)
    with tempfile.TemporaryDirectory() as tmp:
        obj = os.path.join(tmp, "test.o")
        for tag in tags:
            exe = os.path.join(COMPILERS, *tag.split("/"), "mwcceppc.exe")
            if not os.path.exists(exe):
                print(f"{tag:16} no mwcceppc.exe")
                continue
            if os.path.exists(obj):
                os.remove(obj)
            # Run from the project root: the include paths of build.ninja are relative.
            run = subprocess.run([exe, *cflags, "-c", source, "-o", obj],
                                 capture_output=True, text=True, cwd=ROOT)
            if run.returncode != 0 or not os.path.exists(obj):
                lines = (run.stdout + run.stderr).strip().splitlines()
                print(f"{tag:16} compile error: {lines[-1] if lines else run.returncode}")
                continue
            compiled = compiled_functions(obj)
            results = []
            for ours_name, orig_name in pairs:
                ours = compiled.get(ours_name)
                result = "missing" if ours is None else compare(ours, originals[orig_name])
                results.append(f"{orig_name}:{result}")
            print(f"{tag:16} " + "  ".join(results))


if __name__ == "__main__":
    main()
