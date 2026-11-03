# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Small, shared pieces of the machine-readable serverrf output."""

from __future__ import annotations

import json
from datetime import datetime


def print_json(value) -> None:
    print(json.dumps(value, separators=(",", ":")))


def local_iso(value) -> str:
    """Render a stored local datetime with the server's UTC offset."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip())
    return parsed.astimezone().isoformat()


def epoch_ms_iso(value) -> str | None:
    milliseconds = int(value or 0)
    if milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000).astimezone().isoformat()
