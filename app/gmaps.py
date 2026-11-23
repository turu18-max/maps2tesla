"""Google Maps share-link parsing.

Expands a maps.app.goo.gl short link and extracts the ordered stops.
The expanded URL carries `geocode` tokens; each token is a small protobuf
whose field5/field6 fixed64 values form the place FID (A:B). A Google
Place ID is base64url(protobuf{1:{1:A, 2:B}}). Tokens without an FID
(coordinate-only points) yield None.
"""
import base64
import struct
import subprocess
import urllib.parse


def expand(url: str) -> str:
    """Follow redirects and return the effective URL (the URL itself
    carries the route data even when Google blocks the page body)."""
    out = subprocess.run(
        ["curl", "-s", "-L", "--max-redirs", "10", "-A", "Mozilla/5.0",
         "-o", "/dev/null", "-w", "%{url_effective}", url],
        capture_output=True, text=True, timeout=30)
    return out.stdout.strip() or url


def parse_stops(url: str):
    """Extract ordered stops from an expanded Maps URL."""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    stops = []
    if "saddr" in qs:
        stops.append({"role": "origin", "value": qs["saddr"][0]})
    if "daddr" in qs:  # legacy format: stops joined by " to:"
        parts = qs["daddr"][0].split(" to:")
        for i, p in enumerate(parts):
            role = "destination" if i == len(parts) - 1 else "waypoint"
            stops.append({"role": role, "value": p.strip()})
    if not stops:
        if "origin" in qs:
            stops.append({"role": "origin", "value": qs["origin"][0]})
        if "waypoints" in qs:
            for p in qs["waypoints"][0].split("|"):
                if p.strip():
                    stops.append({"role": "waypoint", "value": p.strip()})
        if "destination" in qs:
            stops.append({"role": "destination", "value": qs["destination"][0]})
    if not stops:
        path = urllib.parse.urlparse(url).path
        if "/maps/dir/" in path:
            segs = [urllib.parse.unquote(s)
                    for s in path.split("/maps/dir/")[1].split("/")]
            # The trailing data=... segment is route metadata, not a
            # stop (Gemini-generated links append it in the path).
            for i, s in enumerate(segs):
                if s.startswith("data="):
                    segs = segs[:i]
                    break
            if segs and segs[-1] == "":
                segs.pop()  # trailing slash, not an empty destination
            # Empty segments are kept: an empty origin means
            # "current location" and dropping it would shift every
            # role by one (also Gemini-style links).
            for i, s in enumerate(segs):
                role = "origin" if i == 0 else (
                    "destination" if i == len(segs) - 1 else "waypoint")
                stops.append({"role": role, "value": s})
    return stops


def _fid_to_place_id(a: int, b: int) -> str:
    buf = bytearray(20)
    buf[0] = 0x0A
    buf[1] = 0x12
    buf[2] = 0x09
    struct.pack_into("<Q", buf, 3, a)
    buf[11] = 0x11
    struct.pack_into("<Q", buf, 12, b)
    return base64.urlsafe_b64encode(bytes(buf)).decode().rstrip("=")


def _parse_fields(raw: bytes):
    i = 0
    fields = {}
    while i < len(raw):
        tag = raw[i]
        i += 1
        field = tag >> 3
        wt = tag & 7
        if wt == 5:
            fields[field] = raw[i:i + 4]
            i += 4
        elif wt == 1:
            fields[field] = struct.unpack_from("<Q", raw, i)[0]
            i += 8
        elif wt == 0:
            v = 0
            s = 0
            while raw[i] & 0x80:
                v |= (raw[i] & 0x7F) << s
                s += 7
                i += 1
            v |= (raw[i] & 0x7F) << s
            i += 1
            fields[field] = v
        elif wt == 2:
            ln = raw[i]
            i += 1
            fields[field] = raw[i:i + ln]
            i += ln
        else:
            break
    return fields


def place_ids_from_expanded(url: str):
    """One Place ID (or None) per geocode token, in URL order."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    out = []
    for tok in q.get("geocode", [""])[0].split(";"):
        if not tok:
            continue
        f = _parse_fields(base64.urlsafe_b64decode(tok))
        out.append(_fid_to_place_id(f[5], f[6]) if 5 in f and 6 in f else None)
    return out


import re

_POSTAL = re.compile(r"^〒\d{3}-\d{4}\s*")
# Address tail: ...町1丁目157-1 / ...町681 / ...町608番地, then a space
# and the place name. Only applied when the head looks like an address.
_ADDR_TAIL = re.compile(
    r"^(.*?(?:都|道|府|県).*?(?:市|区|町|村).*?)"
    r"(?:\d+\s*丁目\s*)?(?:\d+\s*番地|\d+[-−]\d+|\d+)\s+(.+)$")


def short_name(value: str) -> str:
    """Best-effort place name from a stop value: strip the postal code
    and street address, keeping the trailing place name when present
    ('〒100-0001 東京都千代田区千代田1-1 テスト食堂'
    -> 'テスト食堂'). Falls back to the value with
    only the postal code removed."""
    v = _POSTAL.sub("", value).strip()
    # Comma-separated address form used by Gemini-generated links
    # ('日本,神奈川県箱根町湯本123,テスト温泉' -> 'テスト温泉'). Guarded by
    # the prefecture kanji so coordinate strings are untouched.
    if ("都" in v or "道" in v or "府" in v or "県" in v) \
            and ("," in v or "，" in v):
        last = re.split(r"[,，]", v)[-1].replace("+", " ").strip()
        if last:
            return last
    m = _ADDR_TAIL.match(v)
    if m and m.group(2):
        return m.group(2).strip()
    return v


_CHIJ = re.compile(r"ChIJ[A-Za-z0-9_-]+")
_LONLAT = re.compile(r"!1d(-?\d+(?:\.\d+)?)!2d(-?\d+(?:\.\d+)?)")


def data_param_blocks(url: str):
    """Per-stop blocks from a dir link's data= parameter, in order:
    [(place_id|None, (lat, lon)), ...] with one entry per stop block
    (each block carries a !1d<lon>!2d<lat> pair; a block's Place ID
    is the ChIJ string appearing before its coordinate pair and
    after the previous pair). Gemini-generated dir links carry the
    waypoint Place IDs ONLY here — the path segments are address
    text. Returns None when there is no data= segment."""
    path = urllib.parse.urlparse(url).path
    if "/data=" not in path:
        return None
    blob = urllib.parse.unquote(path.split("/data=", 1)[1])
    pairs = list(_LONLAT.finditer(blob))
    if not pairs:
        return None
    ids = [(m.start(), m.group(0)) for m in _CHIJ.finditer(blob)]
    blocks = []
    prev_end = 0
    for p in pairs:
        pid = None
        for pos, text in ids:
            if prev_end <= pos < p.start():
                pid = text
        blocks.append((pid, (float(p.group(2)), float(p.group(1)))))
        prev_end = p.end()
    return blocks


def parse_route(url: str):
    """Full pipeline: share link -> ordered stops with Place IDs.

    Returns dict with `stops` (origin included), `sendable` (waypoints +
    destination that have a Place ID), and `missing` (stops without one).
    """
    expanded = expand(url)
    stops = parse_stops(expanded)
    for stop in stops:
        stop["name"] = short_name(stop["value"])
    ids = place_ids_from_expanded(expanded)
    for stop, pid in zip(stops, ids):
        stop["place_id"] = pid
    for stop in stops[len(ids):]:
        stop["place_id"] = None
    # Gemini-style dir links: stops came from the path segments and
    # their Place IDs live only in the data= parameter. Fill what is
    # still missing when the block count lines up with the targets;
    # an unnamed ('') block with coordinates becomes a coordinate
    # stop, so the usual snap/drop handling applies to it.
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(expanded).query)
    if not any(k in qs for k in
               ("saddr", "daddr", "origin", "destination", "waypoints")):
        blocks = data_param_blocks(expanded)
        targets = [s for s in stops
                   if s["role"] in ("waypoint", "destination")]
        if blocks and len(blocks) == len(targets):
            for stop, (pid, (lat, lon)) in zip(targets, blocks):
                if pid and not stop.get("place_id"):
                    stop["place_id"] = pid
                if stop["value"].strip() in ("", "''"):
                    stop["value"] = f"{lat},{lon}"
                    stop["name"] = stop["value"]
    targets = [s for s in stops if s["role"] in ("waypoint", "destination")]
    return {
        "expanded": expanded,
        "stops": stops,
        "sendable": [s for s in targets if s.get("place_id")],
        "missing": [s for s in targets if not s.get("place_id")],
    }
