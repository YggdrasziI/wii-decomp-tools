#!/usr/bin/env python3
"""Port the units of another dtk project built from the same libraries.

The reference project must be built (`ninja` there) so that its compiled objects
(build/<REF>/src/<unit>.o) and its assembly exist; this project must be built too
(build/<ID>/asm). Functions are paired with `ref_match.py` fingerprints and by
name. For each paired function, every relocation of the reference's compiled object
is paired with this DOL's symbol reference at the same offset of the same function:
this gives where each data section of the unit starts here, and the names of the
symbols it references.

Commands:
  place <unit>...      functions (reference size, address and size here), section
                       starts and names to give, for each reference unit
  plan <prefix>        splits.txt blocks for the reference units under <prefix>, in
                       reference order; .text from paired functions, data sections
                       from relocation pairing, each end at the next unit's start
                       (capped at the reference size), with warnings to check
  names <prefix>       renames of generic labels (fn_/lbl_) to reference names;
                       --write applies them to symbols.txt
  fixdata <unit> <section>...
                       copy the data symbols (offset, size, name, scope) of this
                       project's compiled object into symbols.txt, at the unit's
                       split addresses (run after a build of the split unit)

Every command reads this project's build: run `ninja` again after `names --write`,
`fixdata` or a splits.txt change before running the tool again.

The output is evidence to check, not a split: version differences show up as
missing functions, size differences and several candidate starts. Unit names are as
in the reference splits.txt (e.g. RVL_SDK/os/OSAlarm.c).

Usage:
  python tools/wii/ref_port.py path/to/other-decomp <ID> place RVL_SDK/os/OSAlarm.c
  python tools/wii/ref_port.py path/to/other-decomp <ID> plan RVL_SDK/dvd/
  python tools/wii/ref_port.py path/to/other-decomp <ID> names RVL_SDK/dvd/ --write
  python tools/wii/ref_port.py - - fixdata RVL_SDK/os/OSExec.c .sbss .sdata2
"""

import argparse
import os
import re
import subprocess
import sys
from collections import Counter, OrderedDict, namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ref_match  # noqa: E402

ROOT = ref_match.ROOT
DEFAULT_VERSION = ref_match.DEFAULT_VERSION
VERSIONS = ref_match.VERSIONS
OBJDUMP = os.path.join(ROOT, "build", "binutils",
                       "powerpc-eabi-objdump" + (".exe" if os.name == "nt" else ""))

SYMBOL_RE = re.compile(r"^(\S+) = (\S+):0x([0-9A-Fa-f]+); // (.*)$")
SPLIT_SECTION_RE = re.compile(r"^\s+(\S+)\s+start:0x([0-9A-Fa-f]+) end:0x([0-9A-Fa-f]+)")
GENERIC_RE = re.compile(r"^(fn|lbl)_[0-9A-F]{8}$")
# Names the reference itself gives by address or by compiler number: never copied.
REF_GENERIC_RE = re.compile(r"^(@|\.\.\.|fn_|lbl_|jumptable_|switch_)|_[0-9A-F]{8}$")
INSN_RE = re.compile(r"^/\* ([0-9A-F]{8}) [0-9A-F]{8}  (?:[0-9A-F]{2} ){4}\*/\t(\S+)\s*(.*)$")
REF_OPERAND_RE = re.compile(r'"?([@\w$.?<>,\\-]+?)"?(\+0x[0-9A-Fa-f]+)?@(?:ha|h|l|sda21)\b')
# dtk's "# .data:0x0 | 0x804F3870 | size: 0x10" comment before each definition
LOCAL_DEF_RE = re.compile(r"^# \.\S+:0x[0-9A-Fa-f]+ \| 0x([0-9A-Fa-f]{8}) \| size: 0x[0-9A-Fa-f]+\n"
                          r"\.(?:obj|fn) (\S+),", re.M)
OBJ_SYMBOL_RE = re.compile(r"^([0-9a-f]{8}) (.)(.{6}) (\S+)\t([0-9a-f]{8}) (.*)$")
OBJ_RELOC_RE = re.compile(r"^([0-9a-f]{8}) (\S+)\s+(\S+?)(?:([+-])0x([0-9a-f]+))?$")
# Largest drift of a function's shift within a unit (functions added or removed by
# this game's library version).
MAX_SHIFT = 0x800
CODE_SECTIONS = (".text", ".init")
DATA_SECTIONS = (".rodata", ".data", ".bss", ".sdata", ".sbss", ".sdata2", ".sbss2")


# One function of a reference unit: its code section, offset and size in the reference
# object, our address (None when not found), name and size, and its binding (l/g/w).
Func = namedtuple("Func", "name code value size here our_name our_size binding")


def read_symbols(path):
    """[(name, section, address, attributes)] of a symbols.txt, in file order."""
    out = []
    for line in open(path, encoding="utf-8"):
        m = SYMBOL_RE.match(line.rstrip("\r\n"))
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3), 16), m.group(4)))
    return out


def symbol_size(attrs):
    m = re.search(r"size:0x([0-9A-Fa-f]+)", attrs)
    return int(m.group(1), 16) if m else 0


def read_splits(path):
    """OrderedDict unit -> OrderedDict section -> (start, end)."""
    units, unit = OrderedDict(), None
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\r\n")
        if line and not line[0].isspace() and line.endswith(":"):
            unit = line[:-1]
            units[unit] = OrderedDict()
            continue
        m = SPLIT_SECTION_RE.match(line)
        if m and unit and unit != "Sections":
            units[unit][m.group(1)] = (int(m.group(2), 16), int(m.group(3), 16))
    units.pop("Sections", None)
    return units


def objdump(*args):
    if not os.path.exists(OBJDUMP):
        sys.exit(f"{OBJDUMP} not found: run ninja first")
    return subprocess.run([OBJDUMP, *args], capture_output=True, text=True,
                          check=True).stdout


def object_symbols(path):
    """[(section, value, size, name, binding, flags)] of an object; binding is l, g
    or w, flags the six objdump flag characters after it."""
    out = []
    for line in objdump("-t", path).splitlines():
        m = OBJ_SYMBOL_RE.match(line)
        if m:
            binding = m.group(2) if m.group(2) in "lgw" else "g"
            if m.group(2) == " " and "w" in m.group(3):
                binding = "w"
            out.append((m.group(4), int(m.group(1), 16), int(m.group(5), 16),
                        m.group(6).strip(), binding, m.group(3)))
    return out


def code_relocations(path):
    """{section: [(offset, symbol, addend)]} of an object's .text and .init relocations."""
    out, section = {}, None
    for line in objdump("-r", path).splitlines():
        m = re.match(r"^RELOCATION RECORDS FOR \[(\S+)\]:", line)
        if m:
            section = m.group(1) if m.group(1) in CODE_SECTIONS else None
            continue
        m = OBJ_RELOC_RE.match(line)
        if m and section:
            addend = int(m.group(5), 16) if m.group(5) else 0
            out.setdefault(section, []).append(
                (int(m.group(1), 16), m.group(3), -addend if m.group(4) == "-" else addend))
    return out


def object_path(root, version, unit):
    return os.path.join(root, "build", version, "src", re.sub(r"\.(c|cpp|cp)$", ".o", unit))


class Project:
    """This game's symbols, assembly references and function pairing."""

    def __init__(self, args):
        self.args = args
        self.symbols_path = os.path.join(ROOT, "config", args.version, "symbols.txt")
        self.splits_path = os.path.join(ROOT, "config", args.version, "splits.txt")
        self.symbols = read_symbols(self.symbols_path)
        self.by_name = {}
        self.by_addr = {}
        self.attrs_at = {}  # attributes of the by_addr symbol
        self.code_by_name = {}  # static function names repeat: every address
        for name, section, addr, attrs in self.symbols:
            self.by_name.setdefault(name, (section, addr, attrs))
            if section in CODE_SECTIONS:
                self.code_by_name.setdefault(name, []).append(addr)
            # several symbols can share an address (labels of size 0): keep the function
            if (section, addr) not in self.by_addr or "type:function" in attrs:
                self.by_addr[(section, addr)] = name
                self.attrs_at[(section, addr)] = attrs
        self._refs = None
        self._pairs = None

    def label_address(self, label):
        if label in self.by_name:
            return self.by_name[label][1]
        m = re.search(r"_([0-9A-F]{8})$", label)  # dtk's name_ADDRESS for local symbols
        return int(m.group(1), 16) if m else None

    def in_section(self, label, addr, section):
        """Whether a referenced label lies in the given section. Its resolved
        address decides, not its name: static names repeat across units and
        sections."""
        return (section, addr) in self.by_addr

    def refs(self):
        """{instruction address: (label, addend, address)} from this project's assembly.

        A split unit's .s file names its local symbols without the address suffix, and
        static names repeat across units, so a label is resolved with the definitions
        of its own file first (address None when unresolved). dtk leaves the files of
        earlier splits in place: they are read oldest first, so the newest file wins.
        """
        if self._refs is None:
            self._refs = {}
            asm = os.path.join(ROOT, "build", self.args.version, "asm")
            paths = ref_match.glob.glob(os.path.join(asm, "**", "*.s"), recursive=True)
            for path in sorted(paths, key=os.path.getmtime):
                text = open(path, encoding="utf-8").read()
                local = {m.group(2): int(m.group(1), 16) for m in LOCAL_DEF_RE.finditer(text)}
                for line in text.split("\n"):
                    m = INSN_RE.match(line)
                    if not m:
                        continue
                    r = REF_OPERAND_RE.search(m.group(3))
                    if r:
                        label = r.group(1)
                        addend = int(r.group(2)[1:], 16) if r.group(2) else 0
                    elif m.group(2) in ("b", "bl") and not m.group(3).startswith(".L_"):
                        label, addend = m.group(3).strip(), 0
                    else:
                        continue
                    addr = local.get(label)
                    if addr is None:
                        addr = self.label_address(label)
                    self._refs[int(m.group(1), 16)] = (label, addend, addr)
        return self._refs

    def pairs(self):
        """{reference function name: [our addresses]} from ref_match fingerprints."""
        if self._pairs is None:
            ours = ref_match.functions(os.path.join(ROOT, "build", self.args.version, "asm"))
            theirs = ref_match.functions(
                os.path.join(self.args.ref_root, "build", self.args.ref_version, "asm"))
            self._pairs = {}
            for addr, _, ref_name, _ in ref_match.match_functions(ours, theirs,
                                                                   self.args.min_size):
                self._pairs.setdefault(ref_name, []).append(addr)
        return self._pairs

    def function_candidates(self, name):
        """Our possible addresses of a reference function: same name, else ref_match."""
        return self.code_by_name.get(name) or self.pairs().get(name, [])


def place(project, ref_root, ref_version, unit):
    """(functions, section starts, names) of one reference unit.

    functions: [Func]
    starts: {section: Counter(our start address)}
    names: {reference name: (our label, our address, reference binding l/g/w)}
    """
    path = object_path(ref_root, ref_version, unit)
    if not os.path.exists(path):
        sys.exit(f"{path} not found: build the reference project first")
    osyms = object_symbols(path)
    sym_at = {name: (section, value) for section, value, _, name, _, _ in osyms}
    binding = {name: b for _, _, _, name, b, _ in osyms}
    funcs = sorted((section, value, name, size) for section, value, size, name, _, flags in osyms
                   if section in CODE_SECTIONS and "F" in flags)
    relocs = code_relocations(path)
    refs = project.refs()
    functions, starts, names = [], {}, {}
    cands = {name: project.function_candidates(name) for _, _, name, _ in funcs}
    # Static names repeat across units (PackArgs, OnShutdown): per code section, keep
    # the candidate whose shift from the reference agrees with the most common shift.
    shifts = {}
    for code, value, name, _ in funcs:
        for here in cands[name]:
            shifts.setdefault(code, Counter())[here - value] += 1
    base = {code: c.most_common(1)[0][0] for code, c in shifts.items()}
    for code, value, name, size in funcs:
        near = [h for h in cands[name]
                if code in base and abs(h - value - base[code]) <= MAX_SHIFT]
        here = min(near, key=lambda h: abs(h - value - base[code])) if near else None
        our_name = project.by_addr.get((code, here)) if here is not None else None
        # the size of the symbol at that address, not of the first one with its name
        our_size = symbol_size(project.attrs_at[(code, here)]) if our_name else None
        functions.append(Func(name, code, value, size, here, our_name, our_size,
                              binding.get(name, "g")))
        if here is None:
            continue
        for offset, sym, addend in relocs.get(code, []):
            if not value <= offset < value + size:
                continue
            ref = refs.get(here + ((offset - value) & ~3))
            if ref is None:
                continue
            label, label_addend, addr = ref
            if addr is None:
                continue
            section, sym_value = sym_at.get(sym, (sym, 0))
            if section in DATA_SECTIONS and project.in_section(label, addr, section):
                starts.setdefault(section, Counter())[addr + label_addend - sym_value - addend] += 1
            # a name goes to our label only when both point at the symbol's start
            if (not sym.startswith(".") and sym != label and label_addend == addend
                    and not REF_GENERIC_RE.search(sym)):
                names.setdefault(sym, (label, addr, binding.get(sym, "g")))
    return functions, starts, names


def ref_units(ref_root, ref_version, prefix):
    units = read_splits(os.path.join(ref_root, "config", ref_version, "splits.txt"))
    return OrderedDict((u, s) for u, s in units.items() if u.startswith(prefix))


def cmd_place(project, args):
    for unit in args.units:
        functions, starts, names = place(project, args.ref_root, args.ref_version, unit)
        print(f"== {unit}")
        for f in functions:
            where = ("-" if f.here is None
                     else f"0x{f.here:08X} {f.our_name} size 0x{f.our_size or 0:X}")
            flag = "" if f.here is not None and f.our_size == f.size else "  <<"
            print(f"  {f.name:36s} ref size 0x{f.size:X}  here {where}{flag}")
        for section, counts in starts.items():
            cands = ", ".join(f"0x{a:08X} x{n}" for a, n in counts.most_common(4))
            print(f"  start {section:8s} {cands}")
        for name, (label, addr, _) in sorted(names.items(), key=lambda n: n[1][1]):
            print(f"  name {name} <- {label} (0x{addr:08X})")


def cmd_plan(project, args):
    units = ref_units(args.ref_root, args.ref_version, args.prefix)
    if not units:
        sys.exit(f"no reference unit starts with {args.prefix}")
    info = OrderedDict()
    for unit, secs in units.items():
        functions, data_starts, _ = place(project, args.ref_root, args.ref_version, unit)
        starts = {s: c.most_common(1)[0][0] for s, c in data_starts.items()}
        # code sections: the first paired function, moved back by its reference offset
        for f in functions:
            if f.here is not None:
                starts.setdefault(f.code, f.here - f.value)
        info[unit] = (secs, starts, {s: len(c) for s, c in data_starts.items()})
    order = list(info)
    for i, unit in enumerate(order):
        secs, starts, ncands = info[unit]
        print(f"{unit}:")
        for section, (ref_start, ref_end) in secs.items():
            size = ref_end - ref_start
            start = starts.get(section)
            if start is None:
                print(f"\t# {section}: no start found (reference size 0x{size:X})")
                continue
            nxt = None
            for other in order[i + 1:]:
                o_secs, o_starts, _ = info[other]
                o = o_starts.get(section)
                if section in o_secs and o is not None:
                    nxt = o
                    break
            end = nxt if nxt is not None else start + size
            notes = []
            if end <= start:
                notes.append(f"next unit starts at 0x{end:08X}, before this one: "
                             "reference size used")
                end = start + size
            elif end - start > size and section not in CODE_SECTIONS:
                notes.append(f"0x{end - start:X} to the next unit, capped at the reference size")
                end = start + size
            elif end - start != size:
                notes.append(f"size 0x{end - start:X}, reference 0x{size:X}")
            elif nxt is None:
                notes.append("last unit of the group: reference size")
            if ncands.get(section, 1) > 1:
                notes.append(f"{ncands[section]} candidate starts")
            note = f"  # {'; '.join(notes)}" if notes else ""
            print(f"\t{section:11s} start:0x{start:08X} end:0x{end:08X}{note}")
        print()


def cmd_names(project, args):
    renames, conflicts, bindings = {}, {}, {}
    for unit in ref_units(args.ref_root, args.ref_version, args.prefix):
        functions, _, names = place(project, args.ref_root, args.ref_version, unit)
        found = {name: (label, addr) for name, (label, addr, _) in names.items()}
        found.update((f.name, (f.our_name, f.here)) for f in functions
                     if f.here is not None and f.our_name)
        unit_bindings = {name: b for name, (_, _, b) in names.items()}
        unit_bindings.update((f.name, f.binding) for f in functions)
        for name, (label, _) in found.items():
            if not GENERIC_RE.match(label or "") or REF_GENERIC_RE.search(name):
                continue
            renames.setdefault(label, set()).add(name)
            bindings[name] = unit_bindings.get(name, "g")
    plan = {}
    for label, cands in sorted(renames.items()):
        if len(cands) > 1:
            conflicts[label] = f"several names: {', '.join(sorted(cands))}"
            continue
        name = next(iter(cands))
        if name in project.by_name or name in plan.values():
            conflicts[label] = f"{name} already used"
            continue
        if label not in project.by_name:
            continue  # dtk's name_ADDRESS label, not in symbols.txt
        plan[label] = name
    for label, name in plan.items():
        print(f"{label} -> {name}{' (local)' if bindings.get(name) == 'l' else ''}")
    for label, why in conflicts.items():
        print(f"skip {label}: {why}")
    if not args.write:
        return
    text = open(project.symbols_path, "rb").read().decode("utf-8")

    def sub(m):
        name = plan.get(m.group(1))
        if name is None:
            return m.group(0)
        rest = m.group(2)
        if bindings.get(name) == "l" and "scope:" not in rest:
            rest += " scope:local"
        return name + rest
    # [^\r\n]: in a CRLF file the \r stays after the added scope
    text = re.sub(r"^(\S+)( = [^\r\n]*)", sub, text, flags=re.M)
    open(project.symbols_path, "wb").write(text.encode("utf-8"))
    print(f"{len(plan)} symbols renamed in {project.symbols_path}")


def cmd_fixdata(project, args):
    unit = args.unit
    splits = read_splits(project.splits_path)
    if unit not in splits:
        sys.exit(f"{unit} is not in {project.splits_path}")
    starts = splits[unit]
    path = object_path(ROOT, args.version, unit)
    if not os.path.exists(path):
        sys.exit(f"{path} not found: run ninja first")
    mine = sorted((s, value, size, name, binding)
                  for s, value, size, name, binding, flags in object_symbols(path)
                  if "O" in flags and s in args.sections)
    raw = open(project.symbols_path, "rb").read().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.replace("\r\n", "\n").split("\n")

    def index():
        """{(section, address): [line indices]}: several symbols can share an
        address (dtk labels of size 0 beside an object)."""
        out = {}
        for i, line in enumerate(lines):
            if line is None:  # dropped above, removed when the file is written
                continue
            m = SYMBOL_RE.match(line)
            if m:
                out.setdefault((m.group(2), int(m.group(3), 16)), []).append(i)
        return out

    def where_named(label):
        """(section, address) of every line that defines label."""
        return {(m.group(2), int(m.group(3), 16))
                for m in (SYMBOL_RE.match(line) for line in lines if line is not None)
                if m and m.group(1) == label}

    def taken(label, binding, section, addr):
        """Whether a global or weak label is already defined at another address
        (dtk refuses two globals with one name; local names may repeat)."""
        return binding != "l" and bool(where_named(label) - {(section, addr)})

    def change(i, new):
        if lines[i] != new:
            print(f"- {lines[i]}\n+ {new}")
            lines[i] = new

    idx = index()
    for section, value, size, name, binding in mine:
        if section not in starts:
            sys.exit(f"{unit} has no {section} split")
        addr = starts[section][0] + value
        end = starts[section][1]
        if addr + size > end:
            # Our section is longer than the DOL's (data of a dead-stripped
            # function, notes/MATCHING.md): writing it would drop or shorten the
            # next unit's symbols
            print(f"! {name} ({section} +0x{value:X}, size 0x{size:X}) ends past the "
                  f"split end 0x{end:08X}: not written; put the code that emits it "
                  f"under #if 0 if the DOL lacks it")
            continue
        scope = {"l": "scope:local", "w": "scope:weak"}.get(binding, "scope:global")
        for (s, a), ids in sorted(idx.items()):
            if s != section:
                continue
            for i in ids:
                if a < addr:  # a dtk symbol running into the object is shortened
                    old = re.search(r"size:0x([0-9A-Fa-f]+)", lines[i])
                    if old and a + int(old.group(1), 16) > addr:
                        change(i, lines[i].replace(old.group(0), f"size:0x{addr - a:X}"))
                elif addr < a < addr + size:  # dtk pieces inside the object are dropped
                    print(f"- {lines[i]}")
                    lines[i] = None
        idx = index()
        if (section, addr) in idx:
            ids = idx[(section, addr)]
            # The line to update: the one already named like our symbol, else an
            # object, else one with a size, else the first one; the others at this
            # address (labels of size 0) are kept and reported
            def rank(i):
                m = SYMBOL_RE.match(lines[i])
                return (m.group(1) == name, "type:object" in m.group(4), "size:" in m.group(4))
            i = max(ids, key=rank)  # max keeps the first of equal ranks
            for other in ids:
                if other != i:
                    print(f"! also at 0x{addr:08X}, kept: {lines[other]}")
            m = SYMBOL_RE.match(lines[i])
            old, attrs = m.group(1), m.group(4)
            new = name if GENERIC_RE.match(old) and not name.startswith("@") else old
            if new != old and taken(new, binding, section, addr):
                print(f"! {new} is already defined elsewhere: {old} kept at 0x{addr:08X}")
                new = old
            attrs = re.sub(r"size:0x[0-9A-Fa-f]+", f"size:0x{size:X}", attrs)
            if "scope:" in attrs:
                attrs = re.sub(r"scope:\w+", scope, attrs)
            elif binding == "l" or new != old:
                attrs += " " + scope
            change(i, f"{new} = {m.group(2)}:0x{m.group(3)}; // {attrs}")
        else:
            label = name if not name.startswith("@") else f"lbl_{addr:08X}"
            if label != f"lbl_{addr:08X}" and taken(label, binding, section, addr):
                print(f"! {label} is already defined elsewhere: lbl_{addr:08X} used")
                label = f"lbl_{addr:08X}"
            new = f"{label} = {section}:0x{addr:08X}; // type:object size:0x{size:X} {scope}"
            same = [(a, i) for (s, a), ids in idx.items() if s == section for i in ids]
            if not same:
                sys.exit(f"symbols.txt has no {section} symbol to place {label} after")
            before = [i for a, i in same if a < addr]
            at = max(before) + 1 if before else min(i for _, i in same)
            lines.insert(at, new)
            print(f"+ {new}")
            idx = index()
    text = newline.join(line for line in lines if line is not None)
    open(project.symbols_path, "wb").write(text.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ref_root", help="root of the reference dtk project ('-' for fixdata)")
    parser.add_argument("ref_version", help="game ID of the reference project ('-' for fixdata)")
    parser.add_argument("--version", default=DEFAULT_VERSION, type=str.upper,
                        choices=VERSIONS, help="this game's version (default: %(default)s)")
    parser.add_argument("--min-size", type=lambda v: int(v, 0), default=0x20,
                        help="smallest function size paired by fingerprint, as in "
                             "ref_match.py (default: 0x20); functions with the same name "
                             "are paired at any size")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("place", help="one or more reference units")
    p.add_argument("units", nargs="+")
    p = sub.add_parser("plan", help="splits.txt blocks for a group of reference units")
    p.add_argument("prefix")
    p = sub.add_parser("names", help="renames of generic labels to reference names")
    p.add_argument("prefix")
    p.add_argument("--write", action="store_true", help="apply them to symbols.txt")
    p = sub.add_parser("fixdata", help="data symbols of our compiled object into symbols.txt")
    p.add_argument("unit")
    p.add_argument("sections", nargs="+")
    args = parser.parse_args()
    project = Project(args)
    {"place": cmd_place, "plan": cmd_plan, "names": cmd_names,
     "fixdata": cmd_fixdata}[args.command](project, args)


if __name__ == "__main__":
    main()
