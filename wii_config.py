"""Settings of the Wii / GameCube (dtk) tools, read from the project's `decomp.toml`.

Built on `decomp_config.py` of decomp-tools (submodule `tools/common/`, next to this
one): project root, versions (the first one is the default). Keys read here:

    [wii.game_text]                  # unit_candidates.py: game code per version,
    RM7P4F = [0x80006B40, 0x80232AF8]   # [start, end) of the code whose units end
                                     # with a static initializer (.ctors entry)
"""

import sys
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parent.parent / "common"))
import decomp_config  # noqa: E402
from decomp_config import CONFIG, DEFAULT_VERSION, ROOT, VERSIONS  # noqa: E402,F401

WII = CONFIG.get("wii", {})


def game_text(version: str) -> tuple[int, int]:
    """[start, end) of the game code of a version (`[wii.game_text]`)."""
    bounds = WII.get("game_text", {}).get(version)
    if not bounds:
        sys.exit(f"decomp.toml: no [wii.game_text] {version} = [start, end]")
    return bounds[0], bounds[1]
