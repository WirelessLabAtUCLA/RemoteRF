"""Tolerant in-process parsers for OAI nrMAC_stats.log and nrL1_stats.log."""
from __future__ import annotations

import math
from pathlib import Path
import re
import time


MAX_STATS_BYTES = 256 * 1024
MAX_UE_ROWS = 64

_CELL_HDR_RE = re.compile(r"^===\s*Cell\s+(\d+)\s*===")
_UE_HDR_RE = re.compile(r"^UE\s+RNTI\s+([0-9a-fA-F]+)\b(.*)")
_UE_LINE_RE = re.compile(r"^UE\s+([0-9a-fA-F]+):\s*(.*)")
_DLSCH_L1_RE = re.compile(r"^DLSCH\s+RNTI\s+([0-9a-fA-F]+)\b(.*)")
_ULSCH_L1_RE = re.compile(r"^ULSCH\s+RNTI\s+([0-9a-fA-F]+)\b(.*)")

_INT_RE = r"(-?\d+)"
_UINT_RE = r"(\d+)"
_FLOAT_RE = r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"


def _finite_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _opt_int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m is not None else None


def _opt_num(pattern: str, text: str) -> int | float | None:
    m = re.search(pattern, text)
    if m is None:
        return None
    raw = m.group(1)
    if "." not in raw and "e" not in raw and "E" not in raw:
        try:
            return int(raw)
        except ValueError:
            return None
    return _finite_float(raw)


def _opt_float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    return _finite_float(m.group(1)) if m is not None else None


def _empty_ue(rnti: str) -> dict:
    return {
        "rnti": rnti,
        "in_sync": None,
        "cqi": None,
        "ri": None,
        "pmi": None,
        "ul_ri": None,
        "tpmi": None,
        "ph_db": None,
        "pcmax_dbm": None,
        "dlsch_rounds": None,
        "dlsch_errors": None,
        "ulsch_rounds": None,
        "ulsch_errors": None,
        "dl_bler": None,
        "ul_bler": None,
        "dl_mcs": None,
        "ul_mcs": None,
        "dl_qm": None,
        "ul_qm": None,
        "nprb": None,
        "dl_snr_db": None,
        "ul_snr_db": None,
        "snr_db": None,
        "dl_rssi_dbm": None,
        "ul_rssi_dbm": None,
        "rssi_dbm": None,
        "goodput_dl_mbps": None,
        "goodput_ul_mbps": None,
        "dl_total_bytes_tx": None,
        "ul_total_bytes_rx": None,
        "ul_total_bytes_sched": None,
    }


def _empty_cell() -> dict:
    return {
        "prach_i0_db": None,
        "avg_i0_db": None,
        "max_i0_db": None,
        "min_i0_db": None,
        "blacklisted_prb_count": None,
        "total_prb_count": None,
    }


def _parse_rounds(pattern: str, text: str) -> list[int] | None:
    m = re.search(pattern, text)
    if m is None:
        return None
    return [int(part) for part in m.group(1).split("/")]


def parse_mac_stats(text: str) -> list[dict]:
    """Parse OAI nrMAC_stats.log tolerantly into per-UE rows."""
    ues: dict[str, dict] = {}
    seen_cells: set[str] = set()

    def ensure_ue(rnti_raw: str) -> dict:
        rnti = rnti_raw.lower()
        row = ues.get(rnti)
        if row is None:
            row = _empty_ue(rnti)
            ues[rnti] = row
        return row

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cell_match = _CELL_HDR_RE.match(line)
        if cell_match is not None:
            cell_id = cell_match.group(1)
            if cell_id in seen_cells:
                ues.clear()
                seen_cells.clear()
            seen_cells.add(cell_id)
            continue

        hdr = _UE_HDR_RE.match(line)
        if hdr is not None:
            row = ensure_ue(hdr.group(1))
            rest = hdr.group(2)
            if "in-sync" in rest:
                row["in_sync"] = True
            elif "out-of-sync" in rest:
                row["in_sync"] = False
            ph = _opt_num(rf"\bPH\s+{_FLOAT_RE}\s*dB\b", rest)
            if ph is not None:
                row["ph_db"] = ph
            pcmax = _opt_num(rf"\bPCMAX\s+{_FLOAT_RE}\s*dBm\b", rest)
            if pcmax is not None:
                row["pcmax_dbm"] = pcmax
            continue

        detail = _UE_LINE_RE.match(line)
        if detail is None:
            continue
        row = ensure_ue(detail.group(1))
        rest = detail.group(2)

        if "CQI" in rest or "PMI" in rest or "UL-RI" in rest or "TPMI" in rest or "CSI" in rest or "SRS" in rest:
            cqi = _opt_int(rf"\bCQI\s+{_UINT_RE}", rest)
            if cqi is not None:
                row["cqi"] = cqi
            ri = _opt_int(rf"(?<![A-Za-z0-9_-])RI\s+{_UINT_RE}", rest)
            if ri is not None:
                row["ri"] = ri
            pmi_m = re.search(rf"\bPMI\s*\(\s*{_INT_RE}\s*,\s*{_INT_RE}\s*\)", rest)
            if pmi_m is not None:
                row["pmi"] = [int(pmi_m.group(1)), int(pmi_m.group(2))]
            ul_ri = _opt_int(rf"\bUL-RI\s+{_UINT_RE}", rest)
            if ul_ri is not None:
                row["ul_ri"] = ul_ri
            tpmi = _opt_int(rf"\bTPMI\s+{_UINT_RE}", rest)
            if tpmi is not None:
                row["tpmi"] = tpmi

        if "dlsch_rounds" in rest or "dlsch_errors" in rest:
            rounds = _parse_rounds(r"\bdlsch_rounds\s+(\d+(?:/\d+)*)", rest)
            if rounds is not None:
                row["dlsch_rounds"] = rounds
            errors = _opt_int(rf"\bdlsch_errors\s+{_UINT_RE}", rest)
            if errors is not None:
                row["dlsch_errors"] = errors
            bler = _opt_float(rf"\bBLER\s+{_FLOAT_RE}", rest)
            if bler is not None:
                row["dl_bler"] = bler
            mcs = _opt_int(rf"\bMCS\s+(?:\(\d+\)\s+)?{_UINT_RE}", rest)
            if mcs is not None:
                row["dl_mcs"] = mcs
            qm = _opt_int(rf"\bQm\s+{_UINT_RE}", rest)
            if qm is not None:
                row["dl_qm"] = qm
            snr = _opt_float(rf"\bSNR\s+{_FLOAT_RE}", rest)
            if snr is not None:
                row["dl_snr_db"] = snr
            rssi = _opt_float(rf"\bRSSI\s+{_FLOAT_RE}", rest)
            if rssi is not None:
                row["dl_rssi_dbm"] = rssi

        elif "ulsch_rounds" in rest or "ulsch_errors" in rest:
            rounds = _parse_rounds(r"\bulsch_rounds\s+(\d+(?:/\d+)*)", rest)
            if rounds is not None:
                row["ulsch_rounds"] = rounds
            errors = _opt_int(rf"\bulsch_errors\s+{_UINT_RE}", rest)
            if errors is not None:
                row["ulsch_errors"] = errors
            bler = _opt_float(rf"\bBLER\s+{_FLOAT_RE}", rest)
            if bler is not None:
                row["ul_bler"] = bler
            mcs = _opt_int(rf"\bMCS\s+(?:\(\d+\)\s+)?{_UINT_RE}", rest)
            if mcs is not None:
                row["ul_mcs"] = mcs
            qm = _opt_int(rf"\bQm\s+{_UINT_RE}", rest)
            if qm is not None:
                row["ul_qm"] = qm
            nprb = _opt_int(rf"\bNPRB\s+{_UINT_RE}", rest)
            if nprb is not None:
                row["nprb"] = nprb
            snr = _opt_float(rf"\bSNR\s+{_FLOAT_RE}", rest)
            if snr is not None:
                row["ul_snr_db"] = snr
            rssi = _opt_float(rf"\bRSSI\s+{_FLOAT_RE}", rest)
            if rssi is not None:
                row["ul_rssi_dbm"] = rssi

        if "goodput" in rest:
            after = rest.split("goodput", 1)[1]
            dl_gp = _opt_float(rf"\bDL\s+{_FLOAT_RE}", after)
            if dl_gp is not None:
                row["goodput_dl_mbps"] = dl_gp
            ul_gp = _opt_float(rf"\bUL\s+{_FLOAT_RE}", after)
            if ul_gp is not None:
                row["goodput_ul_mbps"] = ul_gp

    rows = list(ues.values())
    for row in rows:
        row["snr_db"] = row["ul_snr_db"] if row["ul_snr_db"] is not None else row["dl_snr_db"]
        row["rssi_dbm"] = row["ul_rssi_dbm"] if row["ul_rssi_dbm"] is not None else row["dl_rssi_dbm"]
    return rows


def parse_l1_stats(text: str) -> tuple[dict, dict[str, dict]]:
    """Parse OAI nrL1_stats.log tolerantly into (cell_summary, per_ue_l1_map)."""
    cell = _empty_cell()
    l1_ues: dict[str, dict] = {}
    current_ul_rnti: str | None = None

    def ensure_l1(rnti_raw: str) -> dict:
        rnti = rnti_raw.lower()
        row = l1_ues.get(rnti)
        if row is None:
            row = {
                "dl_qm": None,
                "ri": None,
                "dl_total_bytes_tx": None,
                "ul_qm": None,
                "ul_ri": None,
                "ul_total_bytes_rx": None,
                "ul_total_bytes_sched": None,
            }
            l1_ues[rnti] = row
        return row

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            current_ul_rnti = None
            continue

        if raw_line[0].isspace() and current_ul_rnti is not None:
            row = ensure_l1(current_ul_rnti)
            qm = _opt_int(rf"\bcurrent_Qm\s+{_UINT_RE}", stripped)
            if qm is not None:
                row["ul_qm"] = qm
            ri = _opt_int(rf"\bcurrent_RI\s+{_UINT_RE}", stripped)
            if ri is not None:
                row["ul_ri"] = ri
            tot_m = re.search(rf"\btotal_bytes\s+RX/SCHED\s+{_UINT_RE}/{_UINT_RE}", stripped)
            if tot_m is not None:
                row["ul_total_bytes_rx"] = int(tot_m.group(1))
                row["ul_total_bytes_sched"] = int(tot_m.group(2))
            continue

        current_ul_rnti = None

        bl_m = re.search(rf"\bBlacklisted\s+PRBs\s+{_UINT_RE}(?:/{_UINT_RE})?", stripped)
        if bl_m is not None:
            cell["blacklisted_prb_count"] = int(bl_m.group(1))
            if bl_m.group(2) is not None:
                cell["total_prb_count"] = int(bl_m.group(2))
            continue

        if "I0" in stripped or "IO" in stripped:
            prach = _opt_float(rf"\bPRACH\s+I[0O]\s*=\s*{_FLOAT_RE}", stripped)
            if prach is not None:
                cell["prach_i0_db"] = prach
            avg_i0 = _opt_float(rf"\bavg_I[0O]\s*=\s*{_FLOAT_RE}", stripped)
            if avg_i0 is not None:
                cell["avg_i0_db"] = avg_i0
            max_i0 = _opt_float(rf"\bmax_I[0O]\s*=\s*{_FLOAT_RE}", stripped)
            if max_i0 is not None:
                cell["max_i0_db"] = max_i0
            min_i0 = _opt_float(rf"\bmin_I[0O]\s*=\s*{_FLOAT_RE}", stripped)
            if min_i0 is not None:
                cell["min_i0_db"] = min_i0

        dl_m = _DLSCH_L1_RE.match(stripped)
        if dl_m is not None:
            row = ensure_l1(dl_m.group(1))
            rest = dl_m.group(2)
            qm = _opt_int(rf"\bcurrent_Qm\s+{_UINT_RE}", rest)
            if qm is not None:
                row["dl_qm"] = qm
            ri = _opt_int(rf"\bcurrent_RI\s+{_UINT_RE}", rest)
            if ri is not None:
                row["ri"] = ri
            tx = _opt_int(rf"\btotal_bytes\s+TX\s+{_UINT_RE}", rest)
            if tx is not None:
                row["dl_total_bytes_tx"] = tx
            continue

        ul_m = _ULSCH_L1_RE.match(stripped)
        if ul_m is not None:
            current_ul_rnti = ul_m.group(1).lower()
            ensure_l1(current_ul_rnti)
            continue

    return cell, l1_ues


def merge_gnb_stats(mac_ues: list[dict], l1_cell: dict, l1_ues: dict[str, dict]) -> tuple[dict, list[dict]]:
    """Combine parsed MAC and L1 per-UE metrics without discarding partial data."""
    merged: dict[str, dict] = {row["rnti"]: dict(row) for row in mac_ues}
    for rnti, l1_row in l1_ues.items():
        row = merged.get(rnti)
        if row is None:
            row = _empty_ue(rnti)
            merged[rnti] = row
        for key in ("dl_total_bytes_tx", "ul_total_bytes_rx", "ul_total_bytes_sched"):
            if l1_row.get(key) is not None:
                row[key] = l1_row[key]
        for key in ("dl_qm", "ri", "ul_qm", "ul_ri"):
            if row.get(key) is None and l1_row.get(key) is not None:
                row[key] = l1_row[key]
    rows = list(merged.values())
    for row in rows:
        row["snr_db"] = row["ul_snr_db"] if row["ul_snr_db"] is not None else row["dl_snr_db"]
        row["rssi_dbm"] = row["ul_rssi_dbm"] if row["ul_rssi_dbm"] is not None else row["dl_rssi_dbm"]
    return l1_cell, rows


def _read_bounded_file(path: Path, now: float) -> tuple[str | None, float | None]:
    try:
        st = path.stat()
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATS_BYTES)
    except OSError:
        return None, None
    age_s = round(max(0.0, now - st.st_mtime), 3)
    return raw.decode("utf-8", errors="replace"), age_s


def read_gnb_stats(work_dir: str | Path, *, now: float | None = None) -> dict | None:
    """Read and parse nrMAC_stats.log and nrL1_stats.log from work_dir."""
    root = Path(work_dir)
    ts = time.time() if now is None else now
    mac_text, mac_age = _read_bounded_file(root / "nrMAC_stats.log", ts)
    l1_text, l1_age = _read_bounded_file(root / "nrL1_stats.log", ts)
    if mac_text is None and l1_text is None:
        return None

    mac_ues = parse_mac_stats(mac_text) if mac_text is not None else []
    l1_cell, l1_ues = parse_l1_stats(l1_text) if l1_text is not None else (_empty_cell(), {})
    cell, ues = merge_gnb_stats(mac_ues, l1_cell, l1_ues)
    truncated = len(ues) > MAX_UE_ROWS
    if truncated:
        ues = ues[:MAX_UE_ROWS]

    ages = [a for a in (mac_age, l1_age) if a is not None]
    return {
        "cell": cell,
        "ues": ues,
        "ue_count": len(ues),
        "truncated": truncated,
        "file_age_s": max(ages) if ages else None,
        "files": {
            "mac_stats_present": mac_text is not None,
            "mac_stats_age_s": mac_age,
            "l1_stats_present": l1_text is not None,
            "l1_stats_age_s": l1_age,
        },
    }


def _parse_ue_stats_text(text: str) -> dict:
    rnti_m = re.search(r"\bRNTI\s+([0-9a-fA-F]+)\b", text)
    return {
        "rnti": rnti_m.group(1).lower() if rnti_m is not None else None,
        "rsrp_dbm": _opt_float(rf"\bRSRP\s*[:=]?\s*{_FLOAT_RE}", text),
        "snr_db": _opt_float(rf"\bSNR\s*[:=]?\s*{_FLOAT_RE}", text),
        "rssi_dbm": _opt_float(rf"\bRSSI\s*[:=]?\s*{_FLOAT_RE}", text),
        "cqi": _opt_int(rf"\bCQI\s*[:=]?\s*{_UINT_RE}", text),
        "ri": _opt_int(rf"(?<![A-Za-z0-9_-])RI\s*[:=]?\s*{_UINT_RE}", text),
        "dl_mcs": _opt_int(rf"\b(?:DL\s+)?MCS\s*[:=]?\s*(?:\(\d+\)\s+)?{_UINT_RE}", text),
        "dl_bler": _opt_float(rf"\bBLER\s*[:=]?\s*{_FLOAT_RE}", text),
    }


def read_ue_stats(work_dir: str | Path, *, now: float | None = None) -> dict | None:
    """Return any UE-side stats files written into work_dir, or None when absent."""
    root = Path(work_dir)
    ts = time.time() if now is None else now
    ue_text, ue_age = _read_bounded_file(root / "nrUE_stats.log", ts)
    if ue_text is not None:
        return {
            "ue_stats": _parse_ue_stats_text(ue_text),
            "file_age_s": ue_age,
            "files": {"ue_stats_present": True, "ue_stats_age_s": ue_age},
        }
    return read_gnb_stats(root, now=ts)
