"""
Central registry for all prompt versions.

To add a new version:
  1. Create scripts/versions/vN/ and add gj.py (and hr.py if changing Haryana).
  2. Add an entry to VERSIONS below.
  3. Update CURRENT_VERSION.
  4. Restart the viewer server so the new version appears in the UI.
"""
import importlib.util
from pathlib import Path
from typing import NamedTuple


class Prompts(NamedTuple):
    structure_system: str
    structure_user: str
    single_call_system: str
    single_call_user: str


_VERSIONS_DIR = Path(__file__).parent / "versions"

CURRENT_VERSION = "v4"

# Order matters for the UI — list in ascending version order.
VERSIONS: dict = {
    "v0": {
        "label":       "v0",
        "description": "Initial — Gujarat-only prompt, basic schema.",
        "notes":       "All states used the Gujarat template. "
                       "No category rows, tower_type, proposed, or Haryana-specific logic.",
    },
    "v1": {
        "label":       "v1",
        "description": "Category rows, tower_type, proposed column, wide-table & "
                       "landscape rules. Dedicated Haryana prompt (A1/A2, "
                       "page-continuity check).",
        "notes":       "Added 2026-08-19.",
    },
    "v2": {
        "label":       "v2",
        "description": "Skip flag for missing-page documents (Haryana). "
                       "Tower label validation (all states): reject project names/locations.",
        "notes":       "Added 2026-08-19.",
    },
    "v3": {
        "label":       "v3",
        "description": "Strip alphanumeric S.No serials (B-1, A2) from activity names. "
                       "certified_towers validation: project names → [] (all states).",
        "notes":       "Added 2026-08-19.",
    },
    "v4": {
        "label":       "v4",
        "description": "Haryana: stricter missing-page detection. Any unresolvable S.No "
                       "gap → skip: true. Forbids 'choosing a fragment' as a resolution.",
        "notes":       "Added 2026-08-19.",
    },
}

_HARYANA = {"haryana", "hr"}


def get_prompts(state: str, version: str) -> Prompts:
    """Load Prompts for (state, version).

    Raises KeyError  if the version string is not in VERSIONS.
    Raises FileNotFoundError if the prompt file for that state+version doesn't exist.
    """
    if version not in VERSIONS:
        raise KeyError(
            f"Unknown prompt version {version!r}. "
            f"Available: {sorted(VERSIONS)}"
        )

    resolved = state.strip().lower()
    # Haryana has its own prompt starting from v1; v0 used the Gujarat template.
    use_haryana = resolved in _HARYANA and version != "v0"
    mod_file = _VERSIONS_DIR / version / ("hr.py" if use_haryana else "gj.py")

    if not mod_file.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {mod_file}\n"
            f"Create scripts/versions/{version}/{'hr.py' if use_haryana else 'gj.py'} "
            f"to support this state+version combination."
        )

    spec = importlib.util.spec_from_file_location(
        f"_pv_{version}_{mod_file.stem}", mod_file
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    return Prompts(
        structure_system=mod.STRUCTURE_SYSTEM,
        structure_user=mod.STRUCTURE_USER,
        single_call_system=mod.SINGLE_CALL_SYSTEM,
        single_call_user=mod.SINGLE_CALL_USER,
    )
