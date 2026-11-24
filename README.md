wii-decomp-tools
================

> [!IMPORTANT]
> AI Disclosure:
> AI was used for the following tasks:
> - Writing commentary (primarily translating into English and formatting).
> - Documenting and citing the various sources used in the project.
> - Writing code (C, C++, Python, etc.—I used it as a tool) and reviewing my own work (yes, some code was written by AI, but it was verified by a human; I didn't simply type "Claude, decompile this for me").

Shared tooling for matching decompilations of Wii (and GameCube) games built with
[decomp-toolkit](https://github.com/encounter/decomp-toolkit) (`dtk`) on the
[dtk-template](https://github.com/encounter/dtk-template) layout, mwcceppc and
[objdiff](https://github.com/encounter/objdiff). It is meant to be used as a git
submodule at `tools/wii/` of a project, next to
[decomp-tools](https://github.com/YggdrasziI/decomp-tools) at `tools/common/`, which
holds what does not depend on the platform (function comparison, report gate,
progress card, trace guard).

Projects using it:

- [mlab-decomp](https://github.com/YggdrasziI/mlab-decomp): Monster Lab (Wii)

Setup
=====

```sh
git submodule add https://github.com/YggdrasziI/wii-decomp-tools.git tools/wii
git submodule add https://github.com/YggdrasziI/decomp-tools.git tools/common
```

`configure.py` imports the build generator from here and points it at this folder
for its helper scripts:

```python
from tools.wii.project import ...
config.tools_dir = Path("tools/wii")
```

The other tools run from inside the project, whose root holds a `decomp.toml` (see
decomp-tools: `versions`, the first one being the default). The Wii settings are in
its `[wii]` table (see `wii_config.py`):

```toml
[wii.game_text]                    # unit_candidates.py: game code per version
RM7P4F = [0x80006B40, 0x80232AF8]  # [start, end)
```

Tools
=====

| Tool | Purpose |
|---|---|
| `project.py`, `ninja_syntax.py` | dtk-template's `build.ninja` / `objdiff.json` generator, used by the project's `configure.py` |
| `download_tool.py`, `transform_dep.py`, `decompctx.py`, `changes_fmt.py` | dtk-template's build helpers (tool downloads, dependency files, decomp.me context, report changes) |
| `wii_config.py` | Settings of these tools, from `decomp.toml` |
| `unit_candidates.py` | Game units not split yet, smallest first: ends from the static initializers in `.ctors`, with float and call counts |
| `compiler_sweep.py` | Compile a test source with every mwcc version and compare functions to the DOL |
| `ref_match.py` | Find this game's functions in another dtk project built from the same libraries (fingerprints of the dtk assembly) |
| `ref_port.py` | Port a reference project's library unit: data section starts, symbol names and sizes from relocation pairing |

Provenance
==========

`project.py`, `ninja_syntax.py` (whose own notice is Apache-2.0, from ninja),
`download_tool.py`, `transform_dep.py`, `decompctx.py` and `changes_fmt.py` come from
[dtk-template](https://github.com/encounter/dtk-template) (CC0-1.0); `decompctx.py`
and `changes_fmt.py` find the project root from `configure.py` instead of their
parent folder, and `decompctx.py` reads the source of a precompiled header
(`.pch++` / `.pch`) where a file includes its binary `.mch`. The other scripts were written in the projects that use them.

License: CC0-1.0 (see `LICENSE`), except where a file says otherwise.
