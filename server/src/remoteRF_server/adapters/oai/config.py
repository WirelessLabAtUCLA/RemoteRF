"""Read-only OAI gNB/UE cell configuration parser and 3GPP NR math helpers."""
from __future__ import annotations

import math
from pathlib import Path
import re


MAX_CONF_BYTES = 1024 * 1024

# 3GPP TS 38.331 TDD-UL-DL-Pattern dl-UL-TransmissionPeriodicity enum -> ms.
TDD_PERIODICITY_MS = {
    0: 0.5,
    1: 0.625,
    2: 1.0,
    3: 1.25,
    4: 2.0,
    5: 2.5,
    6: 5.0,
    7: 10.0,
}

# 3GPP TS 38.104 / 38.101 common NR TDD bands (used for UE UL frequency inference).
TDD_NR_BANDS = frozenset({
    34, 38, 39, 40, 41, 46, 47, 48, 50, 51, 53,
    77, 78, 79, 90, 96, 102, 104,
    257, 258, 259, 260, 261, 262, 263,
})

_SC_LEAF_KEYS = frozenset({
    "physCellId",
    "absoluteFrequencySSB",
    "dl_frequencyBand",
    "dl_absoluteFrequencyPointA",
    "dl_offstToCarrier",
    "dl_subcarrierSpacing",
    "dl_carrierBandwidth",
    "ul_frequencyBand",
    "ul_absoluteFrequencyPointA",
    "ul_offstToCarrier",
    "ul_subcarrierSpacing",
    "ul_carrierBandwidth",
    "subcarrierSpacing",
    "referenceSubcarrierSpacing",
    "dl_UL_TransmissionPeriodicity",
    "nrofDownlinkSlots",
    "nrofDownlinkSymbols",
    "nrofUplinkSlots",
    "nrofUplinkSymbols",
    "initialDLBWPsubcarrierSpacing",
})

_GNB_LEAF_KEYS = frozenset({
    "nr_cellid",
    "pdsch_AntennaPorts_XP",
    "pdsch_AntennaPorts_N1",
    "pdsch_AntennaPorts_N2",
    "pusch_AntennaPorts",
})

_RU_LEAF_KEYS = frozenset({
    "nb_tx",
    "nb_rx",
    "if_freq",
})

_MAC_LEAF_KEYS = frozenset({
    "dl_min_mcs",
    "dl_max_mcs",
    "ul_min_mcs",
    "ul_max_mcs",
})

_L1_LEAF_KEYS = frozenset({
    "max_ldpc_iterations",
})

_NUM_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F]+[lL]?|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[lL]?)$")
_INT_STR_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F]+|\d+)[lL]?$")
_SEG_INDEX_RE = re.compile(r"^\[(\d+)\]$|^(\d+)$")


def nr_arfcn_to_hz(arfcn) -> int | None:
    """Convert 3GPP TS 38.104 NR-ARFCN to frequency in Hz."""
    if type(arfcn) is not int or not 0 <= arfcn <= 3_279_165:
        return None
    if arfcn <= 599_999:
        return 5_000 * arfcn
    if arfcn <= 2_016_666:
        return 3_000_000_000 + 15_000 * (arfcn - 600_000)
    return 24_250_080_000 + 60_000 * (arfcn - 2_016_667)


def hz_to_nr_arfcn(freq_hz) -> int | None:
    """Inverse of nr_arfcn_to_hz when freq_hz sits exactly on the NR global raster."""
    if type(freq_hz) is not int or freq_hz < 0:
        return None
    if freq_hz <= 2_999_995_000:
        return freq_hz // 5_000 if freq_hz % 5_000 == 0 else None
    if 3_000_000_000 <= freq_hz <= 24_249_990_000:
        delta = freq_hz - 3_000_000_000
        return 600_000 + delta // 15_000 if delta % 15_000 == 0 else None
    if 24_250_080_000 <= freq_hz <= 99_999_960_000:
        delta = freq_hz - 24_250_080_000
        return 2_016_667 + delta // 60_000 if delta % 60_000 == 0 else None
    return None


def oai_fft_size(prb_count, numerology=None, *, threequarter_fs: bool = False) -> int | None:
    """Derive OAI's ofdm_symbol_size (FFT size) from PRB count, numerology and -E flag."""
    if type(prb_count) is not int or not 1 <= prb_count <= 275:
        return None
    if numerology is not None and (type(numerology) is not int or not 0 <= numerology <= 4):
        return None
    n_sc = 12 * prb_count
    fft = 128
    while fft < n_sc:
        fft <<= 1
    if threequarter_fs:
        three_quarter = (fft * 3) // 4
        if n_sc <= three_quarter:
            fft = three_quarter
    elif fft >= 512 and n_sc * 100 > fft * 85:
        # 15/30/60 MHz channel configs (e.g. 38, 78, 79, 160, 162 PRBs) use 3/2 * pow2.
        fft = (fft * 3) // 2
    return min(fft, 4096)


def _as_int(value) -> int | None:
    if type(value) is int:
        return value
    if type(value) is str and _INT_STR_RE.fullmatch(value.strip()):
        return _parse_int_literal(value.strip())
    return None


def _parse_int_literal(text: str) -> int:
    s = text[:-1] if text.endswith(("l", "L")) else text
    sign = -1 if s.startswith("-") else 1
    if s.startswith(("+", "-")):
        s = s[1:]
    if s.startswith(("0x", "0X")):
        return sign * int(s, 16)
    return sign * int(s, 10)


def _parse_scalar_value(text: str):
    s = text.strip()
    if s == "true":
        return True
    if s == "false":
        return False
    if _INT_STR_RE.fullmatch(s):
        return _parse_int_literal(s)
    if _NUM_RE.fullmatch(s):
        trimmed = s[:-1] if s.endswith(("l", "L")) else s
        val = float(trimmed)
        if math.isfinite(val):
            return val
    return s


def _tokenize_libconfig(text: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "#":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated block comment in OAI config")
            i = end + 2
            continue
        if ch == "@":
            # Ignore libconfig directives such as @include without reading arbitrary paths.
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if ch in "{}()[],;=:":
            tokens.append((ch, ch))
            i += 1
            continue
        if ch == '"':
            i += 1
            chars = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    esc = text[i + 1]
                    chars.append({"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc))
                    i += 2
                else:
                    chars.append(text[i])
                    i += 1
            if i >= n:
                raise ValueError("unterminated string literal in OAI config")
            i += 1
            val = "".join(chars)
            if tokens and tokens[-1][0] == "STRING":
                tokens[-1] = ("STRING", str(tokens[-1][1]) + val)
            else:
                tokens.append(("STRING", val))
            continue
        if ch.isdigit() or (ch in "+-" and i + 1 < n and text[i + 1].isdigit()):
            start = i
            if ch in "+-":
                i += 1
            if i + 1 < n and text[i] == "0" and text[i + 1] in "xX":
                i += 2
                while i < n and text[i] in "0123456789abcdefABCDEF":
                    i += 1
                if i < n and text[i] in "lL":
                    i += 1
                tokens.append(("NUMBER", _parse_int_literal(text[start:i])))
            else:
                is_float = False
                while i < n and text[i].isdigit():
                    i += 1
                if i < n and text[i] == "." and (i + 1 >= n or text[i + 1].isdigit()):
                    is_float = True
                    i += 1
                    while i < n and text[i].isdigit():
                        i += 1
                if i < n and text[i] in "eE":
                    is_float = True
                    i += 1
                    if i < n and text[i] in "+-":
                        i += 1
                    while i < n and text[i].isdigit():
                        i += 1
                if not is_float and i < n and text[i] in "lL":
                    i += 1
                raw = text[start:i]
                if is_float:
                    val = float(raw)
                    if not math.isfinite(val):
                        raise ValueError("non-finite number in OAI config")
                    tokens.append(("NUMBER", val))
                else:
                    tokens.append(("NUMBER", _parse_int_literal(raw)))
            continue
        if ch.isalpha() or ch in "_*":
            start = i
            i += 1
            while i < n and (text[i].isalnum() or text[i] in "_-"):
                i += 1
            ident = text[start:i]
            if ident.lower() == "true":
                tokens.append(("BOOL", True))
            elif ident.lower() == "false":
                tokens.append(("BOOL", False))
            else:
                tokens.append(("IDENT", ident))
            continue
        raise ValueError(f"unexpected character {ch!r} in OAI config")
    return tokens


def parse_libconfig(text: str) -> dict:
    """Parse an OAI libconfig-format .conf file into plain Python dicts/lists."""
    tokens = _tokenize_libconfig(text)
    pos = 0
    n = len(tokens)

    def peek():
        return tokens[pos] if pos < n else ("EOF", None)

    def consume(expected=None):
        nonlocal pos
        tok = peek()
        if expected is not None and tok[0] != expected:
            raise ValueError(f"expected {expected!r} in OAI config, got {tok[0]!r}")
        pos += 1
        return tok

    def parse_value():
        kind, val = peek()
        if kind in {"STRING", "NUMBER", "BOOL"}:
            consume()
            return val
        if kind == "{":
            return parse_group(braced=True)
        if kind == "(":
            return parse_sequence("(", ")")
        if kind == "[":
            return parse_sequence("[", "]")
        if kind == "IDENT":
            # Tolerate unquoted identifiers used as values in some OAI builds.
            consume()
            return val
        raise ValueError(f"unexpected token {kind!r} where a value was expected")

    def parse_sequence(open_tok, close_tok):
        consume(open_tok)
        items = []
        while peek()[0] != close_tok:
            if peek()[0] == "EOF":
                raise ValueError(f"unterminated {open_tok}...{close_tok} in OAI config")
            items.append(parse_value())
            if peek()[0] == ",":
                consume(",")
            else:
                break
        consume(close_tok)
        return items

    def parse_group(*, braced: bool):
        if braced:
            consume("{")
        out = {}
        while True:
            kind = peek()[0]
            if braced and kind == "}":
                consume("}")
                return out
            if not braced and kind == "EOF":
                return out
            if kind in {";", ","}:
                consume()
                continue
            if kind != "IDENT":
                raise ValueError(f"expected setting name in OAI config, got {kind!r}")
            key = str(consume("IDENT")[1])
            sep = peek()[0]
            if sep not in {"=", ":"}:
                raise ValueError(f"expected '=' or ':' after {key!r} in OAI config")
            consume()
            out[key] = parse_value()
            if peek()[0] in {";", ","}:
                consume()

    return parse_group(braced=False)


def _set_dotted_path(tree: dict, path: str, value) -> None:
    segments = [seg for seg in path.split(".") if seg]
    if not segments:
        return
    cur = tree
    for idx, seg in enumerate(segments):
        last = idx == len(segments) - 1
        m = _SEG_INDEX_RE.fullmatch(seg)
        if m is not None:
            index = int(m.group(1) if m.group(1) is not None else m.group(2))
            if type(cur) is not list:
                return
            while len(cur) <= index:
                cur.append({})
            if last:
                cur[index] = value
                return
            if cur[index] is None:
                cur[index] = {}
            cur = cur[index]
        else:
            if type(cur) is not dict:
                return
            if last:
                cur[seg] = value
                return
            next_seg = segments[idx + 1]
            next_is_index = _SEG_INDEX_RE.fullmatch(next_seg) is not None
            if seg not in cur or (next_is_index and type(cur[seg]) is not list) or (
                not next_is_index and type(cur[seg]) is not dict
            ):
                cur[seg] = [] if next_is_index else {}
            cur = cur[seg]


def _set_leaf_override(tree: dict, key: str, value) -> None:
    if key in _SC_LEAF_KEYS:
        _set_dotted_path(tree, f"gNBs.[0].servingCellConfigCommon.[0].{key}", value)
    elif key in _GNB_LEAF_KEYS:
        _set_dotted_path(tree, f"gNBs.[0].{key}", value)
    elif key in _RU_LEAF_KEYS:
        _set_dotted_path(tree, f"RUs.[0].{key}", value)
    elif key in _MAC_LEAF_KEYS:
        _set_dotted_path(tree, f"MACRLCs.[0].{key}", value)
    elif key in _L1_LEAF_KEYS:
        _set_dotted_path(tree, f"L1s.[0].{key}", value)


def _looks_like_value(token: str) -> bool:
    return not token.startswith("-") or bool(_NUM_RE.fullmatch(token))


def apply_gnb_arg_overrides(tree: dict, args: list[str]) -> tuple[dict, bool]:
    """Apply OAI gNB CLI overrides (--path val, --path=val, -E) on top of parsed conf."""
    threequarter_fs = False
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in {"-E", "--threequarter-fs"}:
            threequarter_fs = True
            i += 1
            continue
        if tok.startswith("--"):
            body = tok[2:]
            if "=" in body:
                key, raw_val = body.split("=", 1)
                val = _parse_scalar_value(raw_val)
                if "." in key:
                    _set_dotted_path(tree, key, val)
                else:
                    _set_leaf_override(tree, key, val)
                i += 1
                continue
            if "." in body:
                if i + 1 < len(args) and _looks_like_value(args[i + 1]):
                    _set_dotted_path(tree, body, _parse_scalar_value(args[i + 1]))
                    i += 2
                else:
                    _set_dotted_path(tree, body, True)
                    i += 1
                continue
            if (body in _SC_LEAF_KEYS or body in _GNB_LEAF_KEYS or body in _RU_LEAF_KEYS
                    or body in _MAC_LEAF_KEYS or body in _L1_LEAF_KEYS):
                if i + 1 < len(args) and _looks_like_value(args[i + 1]):
                    _set_leaf_override(tree, body, _parse_scalar_value(args[i + 1]))
                    i += 2
                    continue
        i += 1
    return tree, threequarter_fs


def _first_dict(value) -> dict:
    if type(value) is list and value and type(value[0]) is dict:
        return value[0]
    if type(value) is dict:
        return value
    return {}


def _build_cell_config(
    *,
    profile: str,
    band: int | None,
    ssb_arfcn: int | None,
    ssb_frequency_hz: int | None,
    point_a_arfcn: int | None,
    point_a_frequency_hz: int | None,
    dl_center_frequency_hz: int | None,
    ul_center_frequency_hz: int | None,
    if_freq_hz: int | None,
    numerology: int | None,
    dl_prb_count: int | None,
    ul_prb_count: int | None,
    threequarter_fs: bool,
    tdd_pattern: dict,
    mimo_mode: dict,
    phys_cell_id: int | None = None,
    nr_cell_id: int | None = None,
    mcs_limits: dict | None = None,
    max_ldpc_iterations: int | None = None,
    ssb_start_subcarrier: int | None = None,
) -> dict:
    prb_count = dl_prb_count if dl_prb_count is not None else ul_prb_count
    scs_khz = (15 * (1 << numerology)) if (type(numerology) is int and 0 <= numerology <= 4) else None
    scs_hz = scs_khz * 1000 if scs_khz is not None else None
    bandwidth_hz = (prb_count * 12 * scs_hz) if (prb_count is not None and scs_hz is not None) else None
    fft_size = oai_fft_size(prb_count, numerology, threequarter_fs=threequarter_fs)
    sample_rate_hz = (fft_size * scs_hz) if (fft_size is not None and scs_hz is not None) else None
    result = {
        "ok": True,
        "profile": profile,
        "band": band,
        "ssb_arfcn": ssb_arfcn,
        "ssb_frequency_hz": ssb_frequency_hz,
        "point_a_arfcn": point_a_arfcn,
        "point_a_frequency_hz": point_a_frequency_hz,
        "dl_center_frequency_hz": dl_center_frequency_hz,
        "ul_center_frequency_hz": ul_center_frequency_hz,
        "if_freq_hz": if_freq_hz,
        "numerology": numerology if scs_khz is not None else None,
        "scs_khz": scs_khz,
        "scs_hz": scs_hz,
        "prb_count": prb_count,
        "dl_prb_count": dl_prb_count,
        "ul_prb_count": ul_prb_count,
        "bandwidth_hz": bandwidth_hz,
        "fft_size": fft_size,
        "sample_rate_hz": sample_rate_hz,
        "threequarter_fs": bool(threequarter_fs),
        "tdd_pattern": tdd_pattern,
        "mimo_mode": mimo_mode,
        "phys_cell_id": phys_cell_id,
        "nr_cell_id": nr_cell_id,
        "mcs_limits": mcs_limits or {
            "dl_min_mcs": None,
            "dl_max_mcs": None,
            "ul_min_mcs": None,
            "ul_max_mcs": None,
        },
        "max_ldpc_iterations": max_ldpc_iterations,
    }
    if ssb_start_subcarrier is not None:
        result["ssb_start_subcarrier"] = ssb_start_subcarrier
    return result


def extract_gnb_cell_config(conf_path: str | Path, args: list[str] | tuple[str, ...] = ()) -> dict:
    """Parse an OAI gNB conf file with operator CLI arg overrides applied on top."""
    path = Path(conf_path)
    with path.open("rb") as handle:
        raw = handle.read(MAX_CONF_BYTES + 1)
    if len(raw) > MAX_CONF_BYTES:
        raise ValueError("OAI config file exceeds size limit")
    tree = parse_libconfig(raw.decode("utf-8", errors="replace"))
    tree, threequarter_fs = apply_gnb_arg_overrides(tree, list(args))

    gnb = _first_dict(tree.get("gNBs"))
    scc = _first_dict(gnb.get("servingCellConfigCommon"))
    ru = _first_dict(tree.get("RUs"))
    mac = _first_dict(tree.get("MACRLCs"))
    l1 = _first_dict(tree.get("L1s"))

    ru_bands = ru.get("bands")
    ru_band = _as_int(ru_bands[0]) if type(ru_bands) is list and ru_bands else None
    dl_band = _as_int(scc.get("dl_frequencyBand"))
    ul_band = _as_int(scc.get("ul_frequencyBand"))
    band = dl_band if dl_band is not None else (ul_band if ul_band is not None else ru_band)

    numerology = next(
        (v for v in (
            _as_int(scc.get("dl_subcarrierSpacing")),
            _as_int(scc.get("subcarrierSpacing")),
            _as_int(scc.get("initialDLBWPsubcarrierSpacing")),
            _as_int(scc.get("ul_subcarrierSpacing")),
        ) if v is not None),
        None,
    )
    scs_hz = (15_000 * (1 << numerology)) if (type(numerology) is int and 0 <= numerology <= 4) else None

    dl_prb = _as_int(scc.get("dl_carrierBandwidth"))
    ul_prb = _as_int(scc.get("ul_carrierBandwidth"))
    dl_offset = _as_int(scc.get("dl_offstToCarrier"))
    ul_offset = _as_int(scc.get("ul_offstToCarrier"))

    ssb_arfcn = _as_int(scc.get("absoluteFrequencySSB"))
    ssb_hz = nr_arfcn_to_hz(ssb_arfcn)
    point_a_arfcn = _as_int(scc.get("dl_absoluteFrequencyPointA"))
    point_a_hz = nr_arfcn_to_hz(point_a_arfcn)

    dl_center_hz = None
    if point_a_hz is not None and dl_prb is not None and scs_hz is not None:
        offset = dl_offset if dl_offset is not None else 0
        dl_center_hz = point_a_hz + (offset * 12 + dl_prb * 6) * scs_hz

    ul_point_a_arfcn = _as_int(scc.get("ul_absoluteFrequencyPointA"))
    ul_point_a_hz = nr_arfcn_to_hz(ul_point_a_arfcn)
    ul_mu = _as_int(scc.get("ul_subcarrierSpacing"))
    ul_scs_hz = (15_000 * (1 << ul_mu)) if (type(ul_mu) is int and 0 <= ul_mu <= 4) else scs_hz

    periodicity_enum = _as_int(scc.get("dl_UL_TransmissionPeriodicity"))
    ref_mu = _as_int(scc.get("referenceSubcarrierSpacing"))
    ref_scs_khz = (15 * (1 << ref_mu)) if (type(ref_mu) is int and 0 <= ref_mu <= 4) else None
    tdd_pattern = {
        "periodicity_enum": periodicity_enum,
        "periodicity_ms": TDD_PERIODICITY_MS.get(periodicity_enum) if periodicity_enum is not None else None,
        "reference_scs_khz": ref_scs_khz,
        "dl_slots": _as_int(scc.get("nrofDownlinkSlots")),
        "dl_symbols": _as_int(scc.get("nrofDownlinkSymbols")),
        "ul_slots": _as_int(scc.get("nrofUplinkSlots")),
        "ul_symbols": _as_int(scc.get("nrofUplinkSymbols")),
    }
    is_tdd = any(v is not None for v in tdd_pattern.values()) or (band in TDD_NR_BANDS)

    ul_center_hz = None
    if ul_point_a_hz is not None and ul_prb is not None and ul_scs_hz is not None:
        offset = ul_offset if ul_offset is not None else 0
        ul_center_hz = ul_point_a_hz + (offset * 12 + ul_prb * 6) * ul_scs_hz
    elif dl_center_hz is not None and is_tdd:
        ul_center_hz = dl_center_hz

    xp = _as_int(gnb.get("pdsch_AntennaPorts_XP"))
    n1 = _as_int(gnb.get("pdsch_AntennaPorts_N1"))
    n2 = _as_int(gnb.get("pdsch_AntennaPorts_N2"))
    pdsch_ports = None
    if any(v is not None for v in (xp, n1, n2)):
        prod = (1 if xp is None else xp) * (1 if n1 is None else n1) * (1 if n2 is None else n2)
        pdsch_ports = prod if prod > 0 else None
    pusch_ports = _as_int(gnb.get("pusch_AntennaPorts"))
    nb_tx = _as_int(ru.get("nb_tx"))
    nb_rx = _as_int(ru.get("nb_rx"))
    if nb_tx is not None and nb_rx is not None:
        mode = f"{nb_tx}x{nb_rx}"
    elif pdsch_ports is not None and pusch_ports is not None:
        mode = f"{pdsch_ports}x{pusch_ports}"
    else:
        mode = None
    mimo_mode = {
        "mode": mode,
        "pdsch_antenna_ports": pdsch_ports,
        "pdsch_antenna_ports_xp": xp,
        "pdsch_antenna_ports_n1": n1,
        "pdsch_antenna_ports_n2": n2,
        "pusch_antenna_ports": pusch_ports,
        "nb_tx": nb_tx,
        "nb_rx": nb_rx,
    }

    mcs_limits = {
        "dl_min_mcs": _as_int(mac.get("dl_min_mcs")),
        "dl_max_mcs": _as_int(mac.get("dl_max_mcs")),
        "ul_min_mcs": _as_int(mac.get("ul_min_mcs")),
        "ul_max_mcs": _as_int(mac.get("ul_max_mcs")),
    }

    return _build_cell_config(
        profile="gnb",
        band=band,
        ssb_arfcn=ssb_arfcn,
        ssb_frequency_hz=ssb_hz,
        point_a_arfcn=point_a_arfcn,
        point_a_frequency_hz=point_a_hz,
        dl_center_frequency_hz=dl_center_hz,
        ul_center_frequency_hz=ul_center_hz,
        if_freq_hz=_as_int(ru.get("if_freq")),
        numerology=numerology,
        dl_prb_count=dl_prb,
        ul_prb_count=ul_prb,
        threequarter_fs=threequarter_fs,
        tdd_pattern=tdd_pattern,
        mimo_mode=mimo_mode,
        phys_cell_id=_as_int(scc.get("physCellId")),
        nr_cell_id=_as_int(gnb.get("nr_cellid")),
        mcs_limits=mcs_limits,
        max_ldpc_iterations=_as_int(l1.get("max_ldpc_iterations")),
    )


def parse_ue_cell_config(args: list[str] | tuple[str, ...]) -> dict:
    """Parse OAI nr-uesoftmodem CLI arguments into the cell config schema."""
    band = None
    prb_count = None
    numerology = None
    dl_center_hz = None
    ul_offset_hz = None
    ssb_start_sc = None
    if_freq_hz = None
    threequarter_fs = False
    nb_tx = None
    nb_rx = None

    tokens = list(args)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        key, inline = (tok.split("=", 1) if tok.startswith("--") and "=" in tok else (tok, None))

        def take_int():
            nonlocal i
            if inline is not None:
                i += 1
                return _as_int(inline)
            if i + 1 < len(tokens) and _looks_like_value(tokens[i + 1]):
                val = _as_int(tokens[i + 1])
                i += 2
                return val
            i += 1
            return None

        if key in {"-r", "--prb"}:
            prb_count = take_int()
        elif key == "--numerology":
            numerology = take_int()
        elif key == "--band":
            band = take_int()
        elif key == "-C":
            dl_center_hz = take_int()
        elif key in {"-C0", "--C0", "-CO", "--CO"}:
            ul_offset_hz = take_int()
        elif key == "--ssb":
            ssb_start_sc = take_int()
        elif key in {"--if_freq", "--if-freq"}:
            if_freq_hz = take_int()
        elif key in {"-E", "--threequarter-fs"}:
            threequarter_fs = True
            i += 1
        elif key in {"--ue-nb-ant-tx", "--nb-tx", "--nb_tx"}:
            nb_tx = take_int()
        elif key in {"--ue-nb-ant-rx", "--nb-rx", "--nb_rx"}:
            nb_rx = take_int()
        else:
            i += 1

    scs_hz = (15_000 * (1 << numerology)) if (type(numerology) is int and 0 <= numerology <= 4) else None

    point_a_hz = None
    if dl_center_hz is not None and prb_count is not None and scs_hz is not None:
        point_a_hz = dl_center_hz - prb_count * 6 * scs_hz
    ssb_hz = None
    if point_a_hz is not None and ssb_start_sc is not None and scs_hz is not None:
        # SSB spans 20 PRBs (240 subcarriers); center is at start_subcarrier + 120.
        ssb_hz = point_a_hz + (ssb_start_sc + 120) * scs_hz

    ul_center_hz = None
    if dl_center_hz is not None:
        if ul_offset_hz is not None:
            ul_center_hz = dl_center_hz + ul_offset_hz
        elif band in TDD_NR_BANDS:
            ul_center_hz = dl_center_hz

    tdd_pattern = {
        "periodicity_enum": None,
        "periodicity_ms": None,
        "reference_scs_khz": None,
        "dl_slots": None,
        "dl_symbols": None,
        "ul_slots": None,
        "ul_symbols": None,
    }
    mimo_mode = {
        "mode": f"{nb_tx}x{nb_rx}" if (nb_tx is not None and nb_rx is not None) else None,
        "pdsch_antenna_ports": None,
        "pdsch_antenna_ports_xp": None,
        "pdsch_antenna_ports_n1": None,
        "pdsch_antenna_ports_n2": None,
        "pusch_antenna_ports": None,
        "nb_tx": nb_tx,
        "nb_rx": nb_rx,
    }

    return _build_cell_config(
        profile="ue",
        band=band,
        ssb_arfcn=hz_to_nr_arfcn(ssb_hz) if ssb_hz is not None else None,
        ssb_frequency_hz=ssb_hz,
        point_a_arfcn=hz_to_nr_arfcn(point_a_hz) if point_a_hz is not None else None,
        point_a_frequency_hz=point_a_hz,
        dl_center_frequency_hz=dl_center_hz,
        ul_center_frequency_hz=ul_center_hz,
        if_freq_hz=if_freq_hz,
        numerology=numerology,
        dl_prb_count=prb_count,
        ul_prb_count=prb_count,
        threequarter_fs=threequarter_fs,
        tdd_pattern=tdd_pattern,
        mimo_mode=mimo_mode,
        ssb_start_subcarrier=ssb_start_sc,
    )
