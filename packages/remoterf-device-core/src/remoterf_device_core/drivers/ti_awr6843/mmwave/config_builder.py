"""
config_builder.py

Translates human-readable radar settings and presets into valid
TI mmWave SDK command scripts without requiring users to hand-edit .cfg files.
"""

import os
from typing import List, Optional


PRESETS = {
    "default": {
        "description": "Standard 3D room sensing (All static & moving objects, 10 FPS, 3D mode)",
        "mode": "3d",
        "clutter_removal": False,
        "fps": 10.0,
        "sensitivity": "medium",
        "min_range": 0.25,
        "max_range": 8.0,
    },
    "indoor-people": {
        "description": "Clean human presence & tracking (Static walls/tables removed, 10 FPS)",
        "mode": "3d",
        "clutter_removal": True,
        "fps": 10.0,
        "sensitivity": "medium",
        "min_range": 0.50,
        "max_range": 8.0,
    },
    "gesture": {
        "description": "Close-range high-speed gesture & desktop tracking (< 1.5m, 30 FPS)",
        "mode": "3d",
        "clutter_removal": False,
        "fps": 30.0,
        "sensitivity": "high",
        "min_range": 0.10,
        "max_range": 1.5,
    },
    "fast-motion": {
        "description": "High-speed 2D tracking with clutter removal (30 FPS, 2D mode)",
        "mode": "2d",
        "clutter_removal": True,
        "fps": 30.0,
        "sensitivity": "medium",
        "min_range": 0.50,
        "max_range": 10.0,
    },
}


def build_config(
    base_config_path: Optional[str] = None,
    mode: str = "3d",
    clutter_removal: Optional[bool] = None,
    fps: Optional[float] = None,
    sensitivity: Optional[str] = None,
    min_range: Optional[float] = None,
    max_range: Optional[float] = None,
    preset: Optional[str] = None,
) -> str:
    """
    Builds a complete mmWave SDK configuration script based on human-readable options.
    Returns the configuration as a multiline string ready to be sent to the radar.
    """
    # Start with preset values if selected
    settings = {}
    if preset and preset in PRESETS:
        settings.update(PRESETS[preset])

    # Apply explicit overrides if provided
    if mode is not None:
        settings["mode"] = mode.lower()
    if clutter_removal is not None:
        settings["clutter_removal"] = clutter_removal
    if fps is not None:
        settings["fps"] = float(fps)
    if sensitivity is not None:
        settings["sensitivity"] = sensitivity.lower()
    if min_range is not None:
        settings["min_range"] = float(min_range)
    if max_range is not None:
        settings["max_range"] = float(max_range)

    # Defaults if not covered
    target_mode = settings.get("mode", "3d")
    target_clutter = settings.get("clutter_removal", False)
    target_fps = settings.get("fps", 10.0)
    target_sensitivity = settings.get("sensitivity", "medium")
    target_min_r = settings.get("min_range", 0.25)
    target_max_r = settings.get("max_range", 8.0)

    # Determine base template file
    if base_config_path and os.path.isfile(base_config_path):
        template_file = base_config_path
    else:
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if target_mode == "2d":
            template_file = os.path.join(root_dir, "configs", "awr6843_2d_default.cfg")
        else:
            template_file = os.path.join(root_dir, "configs", "awr6843_3d_default.cfg")

    with open(template_file, "r") as f:
        lines = f.readlines()

    modified_lines: List[str] = []

    # Map human-readable sensitivity to CFAR threshold in dB
    # Lower dB threshold = more sensitive (detects weaker reflections)
    sens_map = {
        "high": 12.0,
        "medium": 15.0,
        "low": 20.0,
    }
    cfar_db = sens_map.get(target_sensitivity, 15.0)

    # Compute frame periodicity in milliseconds from FPS
    periodicity_ms = 1000.0 / max(target_fps, 1.0)

    for line in lines:
        stripped = line.strip()

        # Update static clutter removal
        if stripped.startswith("clutterRemoval"):
            en_val = 1 if target_clutter else 0
            modified_lines.append(f"clutterRemoval -1 {en_val}\n")

        # Update frame periodicity (FPS)
        elif stripped.startswith("frameCfg"):
            parts = stripped.split()
            # frameCfg <startIdx> <endIdx> <numLoops> <numFrames> <periodicityMs> <trigger> <delay>
            if len(parts) >= 8:
                parts[5] = f"{periodicity_ms:.2f}"
                modified_lines.append(" ".join(parts) + "\n")
            else:
                modified_lines.append(line)

        # Update CFAR threshold (sensitivity)
        elif stripped.startswith("cfarCfg"):
            parts = stripped.split()
            # cfarCfg <subFrame> <procDir> <mode> <winLen> <guardLen> <div> <cyclic> <thresholdScale> <peakGroup>
            if len(parts) >= 10:
                parts[8] = f"{int(cfar_db)}"
                modified_lines.append(" ".join(parts) + "\n")
            else:
                modified_lines.append(line)

        # Update Range FOV (min and max distance)
        elif stripped.startswith("cfarFovCfg") and " 0 " in stripped:
            # cfarFovCfg <subFrame> <dir:0=Range> <minRange> <maxRange>
            modified_lines.append(f"cfarFovCfg -1 0 {target_min_r:.2f} {target_max_r:.2f}\n")

        else:
            modified_lines.append(line)

    return "".join(modified_lines)
