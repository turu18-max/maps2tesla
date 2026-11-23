"""Snap a coordinate pin to the nearest real place and return its
Google Place ID, using Google's internal search RPC (the same one the
Maps web client uses; no API key).

Why: the car only accepts `refId:<Place ID>` waypoints — raw
coordinates and address text are ignored (verified on a real car,
2026-11-23). So a pin sampled along a chosen route corridor is
converted to the nearest named place (convenience store, gas station,
roadside station, ...) sitting on/near that corridor, and the place's
ID is used as the waypoint instead.

Speed design: the RPC URL's `pb` parameter is a template — the query
is base64 in `!1z`, the bias center is plain `!2d<lon>` / `!3d<lat>`.
So after harvesting one genuine template from a single search page,
every search is ONE light request with substituted values (verified
2026-11-23: substituted searches return the same nearest places as
the genuine two-step flow). All pins and categories are searched in
parallel; a full 12-pin route resolves in a few seconds, versus
~15 s per pin for the original sequential two-step flow.

Unofficial endpoint: may break if Google changes it. Personal,
low-volume use only.
"""
import base64
import json
import math
import re
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_RPC_URL_RE = re.compile(r"/search\?tbm=map&amp;([^\"]+)")
_PLACE_ID_RE = re.compile(r"^ChIJ[\w-]{10,}$")

# General place categories searched when snapping a coordinate
# stop, after the facility categories below: convenience stores,
# restaurants and the like are what most coordinate points mean.
CATEGORIES = ("コンビニ", "サービスエリア", "レストラン",
              "パーキングエリア", "ガソリンスタンド", "カフェ", "道の駅")

# Categories for places that ARE destinations in themselves (a farm,
# a park, a station). Coordinate stops are snapped with these first:
# a big facility's own registered point is what the user means, even
# when a shop or restaurant inside it sits a little closer to the
# coordinate (a farm's point resolved to its in-farm restaurant
# on 2026-11-23 because only corridor categories were searched).
FACILITY_CATEGORIES = ("観光スポット", "テーマパーク", "公園", "駅",
                       "温泉", "牧場")

# Places that sit near a corridor but make bad waypoints (the car
# would route into a non-public facility).
_NAME_DENYLIST = ("工事事務所", "管理事務所", "営業所")

# Parallel snapping runs category-major waves (all open pins for one
# category at once). Concurrency is capped at 8: measured 2026-11-23,
# 12 concurrent searches made Google throttle (slower, emptier
# results), while 8 stayed fast and complete.

# A genuine RPC `pb` captured from a search page (2026-11-23), used
# only when harvesting a fresh template fails. The query (!1z) and
# bias center (!2d / !3d) are substituted per search.
PB_TEMPLATE = "!1z44Kz44Oz44OT44OL!4m8!1m3!1d25843.106723227327!2d138.25!3d36.2!3m2!1i1024!2i768!4f13.1!7i20!10b1!12m60!1m5!18b1!30b1!31m1!1b1!34e1!2m4!5m1!6e2!20e3!39b1!6m31!32i1!49b1!63m0!66b1!85b1!114b1!149b1!206b1!209b1!212b1!215b1!216b1!222b1!223b1!234b1!235b1!246b1!253b1!260b1!262b1!266b1!270b1!271b1!273b1!281b1!294b1!300b1!301b1!302i300!303i100!327m0!10b1!12b1!13b1!14b1!16b1!17m1!3e1!20m3!5e2!6b1!14b1!46m1!1b0!96b1!97m1!2b1!99b1!19m4!2m3!1i360!2i120!4i8!20m53!2m2!1i203!2i100!3m2!2i4!5b1!6m6!1m2!1i86!2i86!1m2!1i408!2i240!7m29!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2!1m3!1e10!2b0!3e4!2b1!9b0!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20!22m5!1srncDa8O0DebH0PEPjcTM8Qg!7e81!14m1!3srncDa8O0DebH0PEPjcTM8Qg!15i9937!24m108!1m25!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1!18m14!3b1!4b1!5b1!6b1!13b1!14b1!17b1!21b1!22b1!32b1!33m1!1b1!34b1!36e2!10m1!8e3!11m1!3e1!17b1!20m2!1e3!1e6!24b1!25b1!26b1!27b1!29b1!30m1!2b1!36b1!37b1!39m3!2m2!2i1!3i1!43b1!52b1!54m1!1b1!55b1!56m1!1b1!61m2!1m1!1e1!65m5!3m4!1m3!1m2!1i224!2i298!72m22!1m8!2b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!4b1!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4!3sother_user_google_review_posts__and__hotel_and_vr_partner_review_posts!6m1!1e1!9b1!89b1!90m2!1m1!1e2!98m4!1b1!2b1!3b1!6b0!103b1!113b1!114m3!1b1!2m1!1b1!117b1!122m1!1b1!126b1!127b1!128m1!1b1!26m4!2m3!1i80!2i92!4i8!30m28!1m6!1m2!1i0!2i0!2m2!1i530!2i768!1m6!1m2!1i974!2i0!2m2!1i1024!2i768!1m6!1m2!1i0!2i0!2m2!1i1024!2i20!1m6!1m2!1i0!2i748!2m2!1i1024!2i768!34m19!2b1!3b1!4b1!6b1!8m6!1b1!3b1!4b1!5b1!6b1!7b1!9b1!12b1!14b1!20b1!23b1!25b1!26b1!31b1!37m1!1e81!42b1!49m10!3b1!6m2!1b1!2b1!7m2!1e3!2b1!8b1!9b1!10e2!50m3!2e2!3m1!3b1!61b1!67m5!7b1!10b1!14b1!15m1!1b0!69i804!77b1"


def _curl(url):
    out = subprocess.run(["curl", "-s", "--compressed", "-A", _UA, url],
                         capture_output=True, timeout=15)
    return out.stdout.decode("utf-8", errors="replace")


def _haversine(a, b):
    R = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _parse_results(data):
    """Extract {name, lat, lon, place_id} from a search RPC response.
    Layout (observed 2026-11-23): data[64] is the result list; each
    result's [1] is the place block with [11]=name, [9]=[_,_,lat,lon],
    [78]=Place ID. Defensive fallbacks included in case indices drift.
    """
    results = []
    try:
        items = data[64]
    except (TypeError, IndexError, KeyError):
        return results
    if not isinstance(items, list):
        return results
    for item in items:
        try:
            block = item[1]
            name = block[11]
            lat = block[9][2]
            lon = block[9][3]
            place_id = block[78]
            if not (isinstance(name, str) and isinstance(lat, (int, float))
                    and isinstance(lon, (int, float))
                    and isinstance(place_id, str)
                    and _PLACE_ID_RE.match(place_id)):
                raise ValueError("layout mismatch")
        except (TypeError, IndexError, KeyError, ValueError):
            continue
        results.append({"name": name, "lat": float(lat), "lon": float(lon),
                        "place_id": place_id})
    return results


def _load_body(body):
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[1]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _parse_rpc_body(body):
    data = _load_body(body)
    if data is None:
        return []
    return _parse_results(data)


def _parse_single(data):
    """Single-place response shape: an exact-name search returns one
    place block at data[0][1][0][14] (same [11]/[9]/[78] layout as a
    list result block) instead of the data[64] list. Observed
    2026-11-23 with multi-word facility-name queries."""
    try:
        block = data[0][1][0][14]
        name = block[11]
        lat = block[9][2]
        lon = block[9][3]
        place_id = block[78]
        if not (isinstance(name, str) and isinstance(lat, (int, float))
                and isinstance(lon, (int, float))
                and isinstance(place_id, str)
                and _PLACE_ID_RE.match(place_id)):
            return None
    except (TypeError, IndexError, KeyError):
        return None
    return {"name": name, "lat": float(lat), "lon": float(lon),
            "place_id": place_id}


def search_nearby(lat, lon, category, zoom=14):
    """One category search biased at (lat, lon), genuine two-step
    flow: the search page (to obtain the signed RPC URL) and the RPC
    itself. Kept as the reference implementation / fallback.

    Zoom matters: at 14z the results are an area search ranked by
    prominence and the place AT the point is routinely missing; at
    18z the search hugs the point and that place appears at ~0 m
    (measured 2026-11-23 in central Tokyo)."""
    q = urllib.parse.quote(category)
    page = _curl(f"https://www.google.com/maps/search/{q}/@{lat},{lon},{zoom}z?hl=ja")
    m = _RPC_URL_RE.search(page)
    if not m:
        return []
    qs = m.group(1).replace("&amp;", "&")
    return _parse_rpc_body(_curl("https://www.google.com/search?tbm=map&" + qs))


def harvest_template(lat, lon):
    """Fetch one genuine search page and extract its RPC `pb` as a
    fresh template for synthesized searches. None on failure."""
    q = urllib.parse.quote("コンビニ")
    page = _curl(f"https://www.google.com/maps/search/{q}/@{lat},{lon},14z?hl=ja")
    m = _RPC_URL_RE.search(page)
    if not m:
        return None
    qs = m.group(1).replace("&amp;", "&")
    pb = urllib.parse.parse_qs(qs).get("pb", [None])[0]
    return pb or None


def search_rpc_body(lat, lon, query, template):
    """Raw body of one synthesized RPC search for an arbitrary query
    string (category word or place name)."""
    pb = re.sub(r"!2d[0-9.]+", f"!2d{lon}", template)
    pb = re.sub(r"!3d[0-9.]+", f"!3d{lat}", pb)
    z = base64.b64encode(query.encode("utf-8")).decode("ascii")
    pb = re.sub(r"!1z[^!]+", "!1z" + z, pb)
    qs = urllib.parse.urlencode(
        {"authuser": "0", "hl": "ja", "gl": "us", "q": query, "pb": pb})
    return _curl("https://www.google.com/search?tbm=map&" + qs)


def search_rpc(lat, lon, category, template):
    """One category search as a single synthesized RPC request: the
    template's query (!1z, base64) and bias center (!2d / !3d) are
    substituted for this pin and category."""
    return _parse_rpc_body(search_rpc_body(lat, lon, category, template))


# --- Place-name resolution -------------------------------------------
# Links built by hand or by an AI travel plan carry stop NAMES with no
# Place ID (the api=1 dir format has no per-waypoint ID field). Resolve
# each name through the same search RPC: an exact-name query returns
# either the data[64] list or a single-place block. A result is only
# accepted on a confident name match — a wrong guess must never be
# sent to the car silently.

import difflib
import unicodedata


def _norm_name(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s)


def _name_score(query, candidate):
    q, c = _norm_name(query), _norm_name(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c or c in q:
        return 0.85
    return difflib.SequenceMatcher(None, q, c).ratio()


def _query_variants(query):
    """Alternative spellings tried when a name search fails as-is.
    Hand-written and AI-written links use separators Google does not
    match the same way (a name joined with ・ may only resolve
    with the separator replaced by a space),
    and a compound name may only match by one of its segments. The
    tool must cope with the link as given — the sender of the link
    cannot be asked to retype it (distribution requirement,
    2026-11-23)."""
    variants = []
    spaced = re.sub(r"\s+", " ",
                    re.sub(r"[・／/　]", " ", query)).strip()
    if spaced != query:
        variants.append(spaced)
    joined = re.sub(r"[・／/　\s]", "", query)
    if joined != query and joined not in variants:
        variants.append(joined)
    parts = [p for p in re.split(r"[・／/　\s]+", query) if p]
    for p in reversed(parts):
        if p != query and p not in variants:
            variants.append(p)
    return variants


def _resolve_once(query, lat, lon, template):
    data = _load_body(search_rpc_body(lat, lon, query, template))
    if data is None:
        return None
    candidates = _parse_results(data)
    single = _parse_single(data)
    if single:
        candidates.append(single)
    scored = [(_name_score(query, c["name"]), c) for c in candidates]
    scored = [s for s in scored if s[0] >= 0.7]
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    top_score, top = scored[0]
    if len(scored) > 1 and scored[1][0] == top_score \
            and scored[1][1]["place_id"] != top["place_id"] \
            and _haversine((top["lat"], top["lon"]),
                           (scored[1][1]["lat"],
                            scored[1][1]["lon"])) > 200:
        return None  # ambiguous: same name, different far-apart places
    out = dict(top)
    out["score"] = top_score
    out["query"] = query
    return out


def resolve_name(query, lat, lon, template):
    """Resolve one place name biased at (lat, lon). Returns
    {name, place_id, lat, lon, score, query} or None when no
    candidate matches confidently (or the top match is ambiguous
    between distant places). When the name as written finds
    nothing, separator/segment variants are tried before giving
    up; each variant is scored against its own spelling."""
    r = _resolve_once(query, lat, lon, template)
    if r:
        return r
    for variant in _query_variants(query):
        r = _resolve_once(variant, lat, lon, template)
        if r:
            r["query"] = query
            r["matched_query"] = variant
            return r
    return None


def resolve_stops(queries):
    """Resolve stop names in route order (last = destination).
    The destination is resolved first as the regional anchor; each
    following search is biased at the previous resolved stop so
    same-named places are picked along the route. Returns a list
    parallel to `queries` with result dicts or None per stop."""
    results = [None] * len(queries)
    if not queries:
        return results
    template = harvest_template(36.2, 138.25) or PB_TEMPLATE
    center = (36.2, 138.25)
    order = [len(queries) - 1] + list(range(len(queries) - 1))
    prev = None
    for idx in order:
        bias = prev or center
        r = resolve_name(queries[idx], bias[0], bias[1], template)
        results[idx] = r
        if r:
            here = (r["lat"], r["lon"])
            if idx == len(queries) - 1:
                center = here  # destination anchors the region
            prev = here if idx != len(queries) - 1 else prev
    return results


def _nearest_in_categories(lat, lon, categories, max_distance_m,
                           zoom=14):
    """Nearest place across the given categories. The category
    searches are independent RPCs and run in parallel: sequentially
    they cost ~1 s each and dominated send latency (13 categories
    ≈ 13 s to snap one coordinate destination, 2026-11-23).
    pool.map preserves category order and the selection is a pure
    minimum over the full candidate set, so the winner matches
    the sequential scan (which only differed by stopping early at
    the first <=30 m category instead of the nearest one)."""
    def scan(category):
        best = None
        for place in search_nearby(lat, lon, category, zoom=zoom):
            if any(bad in place["name"] for bad in _NAME_DENYLIST):
                continue
            d = _haversine((lat, lon), (place["lat"], place["lon"]))
            if d <= max_distance_m and (
                    best is None or d < best["distance_m"]):
                best = {**place, "distance_m": d, "category": category}
        return best

    with ThreadPoolExecutor(max_workers=min(8, len(categories))) as pool:
        results = list(pool.map(scan, categories))
    best = None
    for cand in results:
        if cand is not None and (
                best is None or cand["distance_m"] < best["distance_m"]):
            best = cand
    return best


def snap_stop(lat, lon, max_distance_m=500.0):
    """Snap a user-intended coordinate stop to a real place.

    Searches run at zoom 18, where the place AT the point appears
    among the results (at 14z it usually does not, and no distance
    rule can pick a candidate that was never returned — central
    Tokyo verification, 2026-11-23). Selection: among hits within
    30 m a facility-type place wins (a tower over its in-house
    restaurant), else the nearest hit within 30 m (the place's own
    point). Beyond that, a facility within 200 m is the intent;
    failing all, the nearest hit within max_distance_m."""
    facility = _nearest_in_categories(lat, lon, FACILITY_CATEGORIES,
                                      max_distance_m, zoom=18)
    if facility is not None and facility["distance_m"] <= 30.0:
        return facility
    any_place = _nearest_in_categories(lat, lon, CATEGORIES,
                                       max_distance_m, zoom=18)
    if any_place is not None and any_place["distance_m"] <= 30.0:
        return any_place
    if facility is not None and facility["distance_m"] <= 200.0:
        return facility
    cands = [c for c in (facility, any_place) if c is not None]
    if not cands:
        return None
    return min(cands, key=lambda c: c["distance_m"])


def snap_stops(pins, max_distance_m=500.0):
    """Snap user-intended coordinate stops (a handful at most);
    returns a list aligned with `pins`. Pins run sequentially on
    purpose: each snap_stop already parallelizes its category
    searches, and stacking both levels would oversubscribe the
    search RPCs (throttling was observed around 12 concurrent)."""
    return [snap_stop(p[0], p[1], max_distance_m) for p in pins]
