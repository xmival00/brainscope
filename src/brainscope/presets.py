"""Lead presets: geometry of a lead model. Built-ins are read-only; user presets live in the app-data folder.

Geometry, measured along the lead from its physical tip:
- `tip_offset_mm`: physical tip to the distal edge of contact 1 (the deepest contact);
- `spacing_mm`: center-to-center distance between neighbouring contacts, one number (uniform) or a list
  of n_contacts - 1 numbers (non-uniform), deepest first.
"""
from __future__ import annotations

import json
import os
import re
import sys
from importlib import resources
from pathlib import Path

import numpy as np

FIELDS = {
    "id": str, "name": str, "manufacturer": str, "type": str, "n_contacts": int,
    "contact_length_mm": float, "diameter_mm": float, "spacing_mm": None, "tip_offset_mm": float,
    "label_pattern": str, "first_index": int, "material": str, "source": str, "notes": str,
}
DEFAULTS = {"manufacturer": "", "type": "depth", "tip_offset_mm": 0.0, "label_pattern": "{lead}{n}",
            "first_index": 1, "material": "", "source": "", "notes": ""}


def user_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return Path(os.environ.get("BRAINSCOPE_HOME", base / "brainscope"))


def validate(p: dict) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in p.items() if k in FIELDS})
    missing = [k for k in FIELDS if k not in out]
    if missing:
        raise ValueError(f"preset is missing: {', '.join(missing)}")
    for k, t in FIELDS.items():
        if t is not None:
            out[k] = t(out[k])
    if not re.fullmatch(r"[A-Za-z0-9._-]+", out["id"]):
        raise ValueError("preset id may contain only letters, digits, '.', '_' and '-'")
    n = out["n_contacts"]
    if n < 1 or out["contact_length_mm"] <= 0 or out["diameter_mm"] <= 0:
        raise ValueError("n_contacts, contact_length_mm and diameter_mm must be positive")
    sp = out["spacing_mm"]
    sp = [float(x) for x in sp] if isinstance(sp, (list, tuple)) else float(sp)
    if isinstance(sp, list) and len(sp) != n - 1:
        raise ValueError(f"spacing_mm list needs {n - 1} values (one per gap)")
    if np.any(np.asarray(sp) < out["contact_length_mm"]) and n > 1:
        raise ValueError("contacts would overlap: spacing_mm is smaller than contact_length_mm")
    out["spacing_mm"] = sp
    return out


def centers(p: dict) -> np.ndarray:
    """Contact centers relative to contact 1 (mm, deepest first)."""
    n, sp = p["n_contacts"], p["spacing_mm"]
    gaps = sp if isinstance(sp, list) else [sp] * (n - 1)
    return np.concatenate([[0.0], np.cumsum(gaps)])


def label(p: dict, lead: str, k: int) -> str:
    return p["label_pattern"].format(lead=lead, n=p["first_index"] + k)


def builtin() -> dict:
    out = {}
    for f in resources.files("brainscope").joinpath("lead_presets").iterdir():
        if f.name.endswith(".json"):
            for p in json.loads(f.read_text()):
                p = validate(p)
                out[p["id"]] = p
    return out


def load_all() -> dict:
    """{id: preset}, each with a `builtin` flag."""
    out = {k: dict(v, builtin=True) for k, v in builtin().items()}
    d = user_dir() / "presets"
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                p = validate(json.loads(f.read_text()))
            except (ValueError, json.JSONDecodeError):
                continue
            if p["id"] not in out:
                out[p["id"]] = dict(p, builtin=False)
    return out


def save_user(p: dict) -> dict:
    p = validate(p)
    if p["id"] in builtin():
        raise ValueError("a built-in preset has this id; choose another id")
    d = user_dir() / "presets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{p['id']}.json").write_text(json.dumps(p, indent=2))
    return p


def delete_user(pid: str):
    if pid in builtin():
        raise ValueError("built-in presets cannot be deleted")
    f = user_dir() / "presets" / f"{pid}.json"
    if f.exists():
        f.unlink()
