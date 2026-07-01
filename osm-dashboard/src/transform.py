# OSM Changeset Dashboard – Serverless Transform (run(input)).
#
# Pipeline:
#   1. Changeset bestimmen: custom field changeset_id (Override) oder neuester
#      Changeset von osm_user (Seed liegt in input["changesets"]).
#   2. Metadaten (bbox, user, comment, counts) via OSM-API.
#   3. osmChange-Download -> bearbeitete Weg-IDs + getaggte Knoten.
#   4. Overpass (POST!): Geometrie der Edits + Hintergrund in der bbox.
#   5. Karte als SVG rendern (Port des Editor-Renderers, 1-bit-Stile).
#   6. HDYC-Statistiken (Streak) scrapen.
#
# Nur Standardbibliothek für HTTP (urllib) -> läuft ohne externe Pakete.
import json, math, base64, io, time, os, urllib.request, urllib.parse
from xml.dom import minidom
try:
    from PIL import Image, ImageDraw
    _HAVE_PIL = True
except Exception:
    _HAVE_PIL = False

UA = "trmnl-osm-dashboard/1.0 (hackathon)"
OSM = "https://api.openstreetmap.org/api/0.6"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HDYC = "https://hdyc.neis-one.org/user/%s"
W, H = 800, 480

# ---- HTTP -----------------------------------------------------------------
def _truthy(v, default=True):
    if v is None or v == "": return default
    if isinstance(v, bool): return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "ja")

def _rem(deadline): return max(0.0, deadline - time.monotonic())

def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        raise RuntimeError("%s: %s" % (url.split("/")[2], e))

def get_json(url, deadline): return json.loads(_get(url, min(10.0, _rem(deadline))))

def overpass(query, deadline):
    body = urllib.parse.urlencode({"data": query}).encode()
    last = "?"
    for url in OVERPASS:
        rem = _rem(deadline)
        if rem < 2.5:
            last = "Zeitbudget erschöpft"; break
        host = url.split("/")[2]
        try:
            req = urllib.request.Request(url, data=body,
                headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=min(9.0, rem)) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = "%s (%s)" % (host, e)
    raise RuntimeError("Overpass fehlgeschlagen; zuletzt %s" % last)

# ---- Projektion -----------------------------------------------------------
def projx(lon): return (lon + 180.0) / 360.0
def projy(lat):
    s = math.sin(lat * math.pi / 180.0)
    return 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
def unprojx(x): return x * 360.0 - 180.0
def unprojy(y): return math.atan(math.sinh(math.pi * (1 - 2 * y))) * 180.0 / math.pi

def categorize(t):
    if not t: return None
    if t.get("building"): return "building"
    if t.get("natural") == "water" or t.get("water") or t.get("waterway"): return "water"
    if t.get("highway"): return "highway:" + t["highway"]
    if t.get("railway"): return "railway"
    if t.get("landuse") or t.get("leisure") == "park" or t.get("natural") == "wood": return "landuse"
    return "other"

def is_closed(pts):
    return len(pts) > 3 and pts[0][0] == pts[-1][0] and pts[0][1] == pts[-1][1]

def road_width(cat, base):
    t = cat.split(":")[1] if ":" in cat else ""
    if any(k in t for k in ("motorway", "trunk")): return base * 3.0
    if "primary" in t: return base * 2.4
    if "secondary" in t: return base * 2.0
    if "tertiary" in t: return base * 1.6
    if any(k in t for k in ("residential", "unclassified", "living")): return base * 1.2
    if any(k in t for k in ("service", "track", "path", "footway", "cycleway", "pedestrian", "steps")): return base * 0.7
    return base

# ---- Daten holen ----------------------------------------------------------
def get_changeset(osm_user, override, deadline):
    # Liefert das komplette Changeset-Objekt (bbox, user, tags, counts) in EINEM Call.
    if override:
        cs = get_json("%s/changeset/%s.json" % (OSM, override), deadline).get("changeset")
        if not cs:
            raise RuntimeError("Changeset #%s nicht gefunden" % override)
        return cs
    lst = get_json("%s/changesets.json?display_name=%s&limit=1"
                   % (OSM, urllib.parse.quote(osm_user)), deadline).get("changesets") or []
    if not lst:
        raise RuntimeError("kein Changeset für User '%s' gefunden" % osm_user)
    return lst[0]

def parse_download(cid, deadline):
    dom = minidom.parseString(_get("%s/changeset/%s/download" % (OSM, cid), min(12.0, _rem(deadline))))
    way_ids = []
    for sec in ("create", "modify"):
        for s in dom.getElementsByTagName(sec):
            for w in s.getElementsByTagName("way"):
                way_ids.append(w.getAttribute("id"))
    nodes = []
    for nd in dom.getElementsByTagName("node"):
        if nd.getElementsByTagName("tag") and nd.getAttribute("lat"):
            nodes.append((float(nd.getAttribute("lon")), float(nd.getAttribute("lat"))))
    return way_ids, nodes

def fetch_edited_ways(way_ids, deadline):
    if not way_ids: return []
    q = "[out:json][timeout:60];way(id:%s);out geom;" % ",".join(way_ids)
    out = []
    for el in overpass(q, deadline).get("elements", []):
        if el.get("type") == "way" and el.get("geometry"):
            out.append({"pts": [(g["lon"], g["lat"]) for g in el["geometry"]], "tags": el.get("tags", {})})
    return out

def fetch_bg(bbox, edited_ids, deadline):
    q = ('[out:json][timeout:60];('
         'way["building"](%s);way["highway"](%s);way["railway"](%s);'
         'way["natural"="water"](%s);way["water"](%s);way["waterway"](%s);'
         'way["landuse"](%s);way["leisure"="park"](%s);'
         ');out geom;' % ((bbox,) * 8))
    out = []
    for el in overpass(q, deadline).get("elements", []):
        if el.get("type") == "way" and el.get("geometry") and str(el.get("id")) not in edited_ids:
            t = el.get("tags", {})
            out.append({"pts": [(g["lon"], g["lat"]) for g in el["geometry"]], "tags": t, "cat": categorize(t)})
    return out

def get_hdyc(user, deadline):
    base = {"streak": 0, "longest_streak": 0, "mapping_days": 0, "changesets": 0, "changes": 0, "rank_de": 0, "since": ""}
    try:
        html = _get(HDYC % urllib.parse.quote(user), min(12.0, _rem(deadline)))
        i = html.index("var contributor")
        st = html.index("{", i)
        depth = 0; instr = False; esc = False; end = None
        for j in range(st, len(html)):
            c = html[j]
            if instr:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': instr = False
                continue
            if c == '"': instr = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0: end = j + 1; break
        obj = json.loads(html[st:end])
        cs = obj.get("changesets", {})
        rank = 0
        for r in (obj.get("ranks") or []):
            if (r.get("code") or "").lower() == "de": rank = int(r.get("rank") or 0)
        base.update(
            streak=int(cs.get("current_streak") or 0),
            longest_streak=int(cs.get("longest_streak") or 0),
            mapping_days=int(cs.get("mapping_days") or 0),
            changesets=int(cs.get("no") or 0),
            changes=int(cs.get("changes") or 0),
            rank_de=rank,
            since=(obj.get("contributor", {}).get("since") or "")[:10],
        )
    except Exception as e:
        base["hdyc_error"] = str(e)
    return base

# ---- Bounds ---------------------------------------------------------------
def compute_bounds(meta, pad_pct=25):
    minX, maxX = projx(meta["min_lon"]), projx(meta["max_lon"])
    minY, maxY = projy(meta["max_lat"]), projy(meta["min_lat"])
    w = (maxX - minX) or 1e-5; h = (maxY - minY) or 1e-5
    minX -= w * pad_pct / 100; maxX += w * pad_pct / 100
    minY -= h * pad_pct / 100; maxY += h * pad_pct / 100
    w = maxX - minX; h = maxY - minY
    ar = W / H
    if w / h < ar:
        d = (h * ar - w) / 2; minX -= d; maxX += d
    else:
        d = (w / ar - h) / 2; minY -= d; maxY += d
    return (minX, minY, maxX, maxY)

# ---- SVG-Renderer (Port aus dem Editor) -----------------------------------
STYLE_DEFAULTS = {
    "bg": "white", "landuse": "none", "water": "stipple", "roads": "casing",
    "road_width": 2.0, "buildings": "3d", "b3d": 2.4, "building_gray": 196,
    "water_gray": 184, "landuse_gray": 237, "road_gray": 138,
    "edited": {"fill": True, "color": "black", "halo": "white", "width": 3.0, "node": 4.5},
    "pattern_scale": 1.0, "line_scale": 1.0, "dither": "threshold",
}
# Mappt eine Editor-Export-Config (camelCase) auf die interne Renderer-Config.
def cfg_from_editor(o):
    c = json.loads(json.dumps(STYLE_DEFAULTS))
    m = {"bg": "bg", "landuse": "landuse", "landuseGray": "landuse_gray",
         "water": "water", "waterGray": "water_gray", "roads": "roads",
         "roadGray": "road_gray", "roadWidth": "road_width", "buildings": "buildings",
         "buildingGray": "building_gray", "building3dHeight": "b3d",
         "patternScale": "pattern_scale", "lineScale": "line_scale", "dither": "dither"}
    for ek, tk in m.items():
        if ek in o: c[tk] = o[ek]
    if isinstance(o.get("edited"), dict): c["edited"].update(o["edited"])
    return c
STYLE_PRESETS = {
    "threed":    {"buildings": "3d", "landuse": "dots", "water": "stipple", "roads": "casing", "road_width": 2.0},
    "toner":     {"buildings": "solid", "landuse": "none", "water": "stipple", "roads": "casing", "road_width": 2.2},
    "hachure":   {"buildings": "hatch", "landuse": "dots", "water": "wave", "roads": "casing", "road_width": 2.0,
                  "edited": {"fill": False, "color": "black", "halo": "white", "width": 3.0, "node": 4.5}},
    "blueprint": {"bg": "black", "buildings": "outline", "landuse": "dots", "water": "hatch", "roads": "solid", "road_width": 1.6,
                  "edited": {"fill": False, "color": "white", "halo": "black", "width": 3.2, "node": 4.5}},
}
def style_cfg(name):
    c = json.loads(json.dumps(STYLE_DEFAULTS))
    p = STYLE_PRESETS.get(name, STYLE_PRESETS["threed"])
    for k, v in p.items():
        if k == "edited": c["edited"].update(v)
        else: c[k] = v
    return c

def _gray(v): h = "%02x" % max(0, min(255, int(round(v)))); return "#" + h + h + h
def _f(v): return ("%.1f" % v).rstrip("0").rstrip(".")   # Linienbreiten
def _n(v): return str(int(round(v)))                      # Koordinaten ganzzahlig

VIEW = (-12.0, -12.0, W + 12.0, H + 12.0)                 # Clip-Box inkl. kleinem Rand

def _simplify(pts, eps):                                   # Douglas-Peucker
    if len(pts) < 3: return pts
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]; e2 = eps * eps
    while stack:
        a, b = stack.pop(); ax, ay = pts[a]; bx, by = pts[b]
        dx = bx - ax; dy = by - ay; dd = dx * dx + dy * dy
        idx = -1; far = e2
        for i in range(a + 1, b):
            px, py = pts[i]
            if dd == 0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / dd
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                cx = ax + t * dx; cy = ay + t * dy
                d2 = (px - cx) ** 2 + (py - cy) ** 2
            if d2 > far: far = d2; idx = i
        if idx != -1:
            keep[idx] = True; stack.append((a, idx)); stack.append((idx, b))
    return [pts[i] for i in range(len(pts)) if keep[i]]

def _clip_seg(x0, y0, x1, y1, box):                        # Liang-Barsky
    xmin, ymin, xmax, ymax = box
    dx = x1 - x0; dy = y1 - y0
    p = (-dx, dx, -dy, dy); q = (x0 - xmin, xmax - x0, y0 - ymin, ymax - y0)
    u0, u1 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0:
            if qi < 0: return None
        else:
            t = qi / pi
            if pi < 0:
                if t > u1: return None
                if t > u0: u0 = t
            else:
                if t < u0: return None
                if t < u1: u1 = t
    return (x0 + u0 * dx, y0 + u0 * dy, x0 + u1 * dx, y0 + u1 * dy)

def _clip_polyline(pts, box):                              # -> Liste zusammenhängender Runs
    runs = []; cur = []
    for i in range(len(pts) - 1):
        seg = _clip_seg(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], box)
        if seg is None:
            if cur: runs.append(cur); cur = []
            continue
        a = (seg[0], seg[1]); b = (seg[2], seg[3])
        if cur and abs(cur[-1][0] - a[0]) < 1e-6 and abs(cur[-1][1] - a[1]) < 1e-6:
            cur.append(b)
        else:
            if cur: runs.append(cur)
            cur = [a, b]
    if cur: runs.append(cur)
    return runs

def _runs_d(runs):
    out = []
    for run in runs:
        out.append("M%s %s" % (_n(run[0][0]), _n(run[0][1])))
        for (x, y) in run[1:]: out.append("L%s %s" % (_n(x), _n(y)))
    return "".join(out)

def _poly_d(pts):
    out = ["M%s %s" % (_n(pts[0][0]), _n(pts[0][1]))]
    for (x, y) in pts[1:]: out.append("L%s %s" % (_n(x), _n(y)))
    out.append("Z"); return "".join(out)

def _visible(pts, box):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return not (max(xs) < box[0] or min(xs) > box[2] or max(ys) < box[1] or min(ys) > box[3])

def _defs(ink, page):
    # Muster: Schraffur, Stipple (fein), Park-Punkte (grob), Wellen
    return (
        '<defs>'
        '<pattern id="pHatch" width="5" height="5" patternUnits="userSpaceOnUse">'
        '<rect width="5" height="5" fill="%s"/>'
        '<path d="M0 5 L5 0 M-1 1 L1 -1 M4 6 L6 4" stroke="%s" stroke-width="0.9"/></pattern>'
        '<pattern id="pStip" width="5" height="5" patternUnits="userSpaceOnUse">'
        '<rect width="5" height="5" fill="%s"/><circle cx="2.5" cy="2.5" r="1" fill="%s"/></pattern>'
        '<pattern id="pPark" width="8" height="8" patternUnits="userSpaceOnUse">'
        '<rect width="8" height="8" fill="%s"/><circle cx="4" cy="4" r="0.9" fill="%s"/></pattern>'
        '<pattern id="pWave" width="14" height="7" patternUnits="userSpaceOnUse">'
        '<rect width="14" height="7" fill="%s"/>'
        '<path d="M0 3.5 Q3.5 1.5 7 3.5 T14 3.5" stroke="%s" stroke-width="1" fill="none"/></pattern>'
        '</defs>'
    ) % (page, ink, page, ink, page, ink, page, ink)

def render_svg(cfg, bounds, edited_ways, edited_nodes, bg_ways):
    minX, minY, maxX, maxY = bounds
    sx = W / (maxX - minX); sy = H / (maxY - minY)
    def pt(lo, la): return ((projx(lo) - minX) * sx, (projy(la) - minY) * sy)
    def px(w): return [pt(lo, la) for (lo, la) in w["pts"]]
    ink = "#fff" if cfg["bg"] == "black" else "#000"
    page = "#000" if cfg["bg"] == "black" else "#fff"
    P = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" shape-rendering="crispEdges">' % (W, H, W, H)]
    P.append(_defs(ink, page))
    P.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, page))

    def areas(cat, eps=0.8):          # sichtbare, vereinfachte Polygone
        for w in bg_ways:
            if w["cat"] != cat: continue
            pts = px(w)
            if _visible(pts, VIEW): yield w, _simplify(pts, eps)
    def polylines(ways, eps=0.8):     # geclippte, vereinfachte Linien-Runs
        for w in ways:
            runs = _clip_polyline(_simplify(px(w), eps), VIEW)
            if runs: yield w, runs

    # Flächen
    if cfg["landuse"] in ("gray", "dots"):
        f = _gray(cfg["landuse_gray"]) if cfg["landuse"] == "gray" else "url(#pPark)"
        for w, ring in areas("landuse"): P.append('<path d="%s" fill="%s"/>' % (_poly_d(ring), f))
    # Wasser (Flächen)
    if cfg["water"] != "none":
        wf = {"gray": _gray(cfg["water_gray"]), "stipple": "url(#pStip)", "wave": "url(#pWave)", "hatch": "url(#pHatch)"}.get(cfg["water"], "url(#pStip)")
        st = "" if cfg["water"] == "gray" else ' stroke="%s" stroke-width="0.7"' % ink
        for w, ring in areas("water"):
            if w["tags"].get("waterway"): continue
            P.append('<path d="%s" fill="%s"%s/>' % (_poly_d(ring), wf, st))
        for w, runs in polylines([x for x in bg_ways if x["cat"] == "water" and x["tags"].get("waterway")]):
            P.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.4"/>' % (_runs_d(runs), ink))
    # Straßen + Bahn
    if cfg["roads"] != "none":
        rd = list(polylines([w for w in bg_ways if w["cat"] and w["cat"].startswith("highway")]))
        if cfg["roads"] == "casing":
            for w, runs in rd:
                P.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round"/>' % (_runs_d(runs), ink, _f(road_width(w["cat"], cfg["road_width"]) + 2.2)))
            for w, runs in rd:
                P.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round"/>' % (_runs_d(runs), page, _f(road_width(w["cat"], cfg["road_width"]))))
        else:
            col = _gray(cfg["road_gray"]) if cfg["roads"] == "gray" else ink
            for w, runs in rd:
                P.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round"/>' % (_runs_d(runs), col, _f(road_width(w["cat"], cfg["road_width"]))))
        rail = _runs_d([r for _, runs in polylines([w for w in bg_ways if w["cat"] == "railway"]) for r in runs])
        if rail: P.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.2" stroke-dasharray="3 3"/>' % (rail, ink))

    # Gebäude
    bm = cfg["buildings"]
    blds = [w for w in bg_ways if w["cat"] == "building"]
    if bm == "3d":
        _extrude(P, blds, px, cfg["b3d"], roof=page, wall="url(#pHatch)", edge=ink, roof_edge=ink)
    elif bm != "none":
        fill = {"gray": _gray(cfg["building_gray"]), "solid": ink, "hatch": "url(#pHatch)"}.get(bm)
        for w in blds:
            pts = px(w)
            if not _visible(pts, VIEW): continue
            P.append('<path d="%s" fill="%s" stroke="%s" stroke-width="0.85"/>' % (_poly_d(_simplify(pts, 0.8)), fill or "none", ink))

    # Bearbeitungen
    e = cfg["edited"]
    ecol = "#fff" if e["color"] == "white" else "#000"
    ehalo = None if e["halo"] == "none" else ("#fff" if e["halo"] == "white" else "#000")
    if bm == "3d":
        _extrude(P, [w for w in edited_ways if is_closed(w["pts"])], px, cfg["b3d"],
                 roof=ecol, wall=ecol, edge=(ehalo or page), roof_edge=(ehalo or page))
        _draw_edited(P, px, pt, edited_ways, edited_nodes, e, ecol, ehalo, only_open=True)
    else:
        _draw_edited(P, px, pt, edited_ways, edited_nodes, e, ecol, ehalo, only_open=False)

    P.append('</svg>')
    return "".join(P)

def _draw_edited(P, px, pt, ways, nodes, e, color, halo, only_open):
    lw = e["width"]; nr = e["node"]; halo_w = 2.2
    prims = []
    for w in ways:
        closed = is_closed(w["pts"])
        if only_open and closed: continue
        pts = px(w)
        if closed:
            if _visible(pts, VIEW): prims.append(("poly", _simplify(pts, 0.4)))
        else:
            runs = _clip_polyline(_simplify(pts, 0.4), VIEW)
            if runs: prims.append(("runs", runs))
    vnodes = []
    for (lon, lat) in nodes:
        x, y = pt(lon, lat)
        if VIEW[0] <= x <= VIEW[2] and VIEW[1] <= y <= VIEW[3]: vnodes.append((x, y))

    def stroke_pass(style, extra):
        for kind, g in prims:
            if kind == "poly":
                fill = style if e["fill"] else "none"
                P.append('<path d="%s" fill="%s" stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round"/>'
                         % (_poly_d(g), fill, style, _f(lw + extra)))
            else:
                P.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round"/>'
                         % (_runs_d(g), style, _f(lw + extra)))
        for (x, y) in vnodes:
            if e["fill"]:
                P.append('<circle cx="%s" cy="%s" r="%s" fill="%s"/>' % (_n(x), _n(y), _f(max(0.5, nr + extra / 2)), style))
            else:
                P.append('<circle cx="%s" cy="%s" r="%s" fill="none" stroke="%s" stroke-width="%s"/>' % (_n(x), _n(y), _f(nr), style, _f(lw + extra)))
    if halo: stroke_pass(halo, halo_w * 2)
    stroke_pass(color, 0)

def _extrude(P, ways, px, hf, roof, wall, edge, roof_edge):
    items = []
    for w in ways:
        if len(w["pts"]) <= 3: continue
        pts = px(w)
        if not _visible(pts, VIEW): continue
        sp = _simplify(pts, 0.6)
        if len(sp) < 3: continue
        lv = 3.0
        try: lv = float(w["tags"].get("building:levels") or 3)
        except Exception: pass
        items.append((max(p[1] for p in sp), sp, lv))
    items.sort(key=lambda t: t[0])  # hinten (oben) zuerst
    for _, sp, lv in items:
        hpx = min(28.0, max(6.0, lv * hf)); ox = hpx * 0.42; oy = -hpx * 0.9
        rp = [(x + ox, y + oy) for (x, y) in sp]; n = len(sp)
        for i in range(n - 1):
            a, b, ar, br = sp[i], sp[i + 1], rp[i], rp[i + 1]
            P.append('<polygon points="%s,%s %s,%s %s,%s %s,%s" fill="%s"/>'
                     % (_n(a[0]), _n(a[1]), _n(b[0]), _n(b[1]), _n(br[0]), _n(br[1]), _n(ar[0]), _n(ar[1]), wall))
        edges = "".join('<line x1="%s" y1="%s" x2="%s" y2="%s"/>' % (_n(sp[i][0]), _n(sp[i][1]), _n(rp[i][0]), _n(rp[i][1])) for i in range(n))
        P.append('<g stroke="%s" stroke-width="0.8">%s</g>' % (edge, edges))
        roof_pts = " ".join("%s,%s" % (_n(x), _n(y)) for (x, y) in rp)
        P.append('<polygon points="%s" fill="%s" stroke="%s" stroke-width="0.9"/>' % (roof_pts, roof, roof_edge))

# ---- PNG-Renderer (Pillow, 1-bit, kleine Payload) -------------------------
def _pat_hatch(size, sp, ink, page):
    W_, H_ = size; img = Image.new("L", size, page); d = ImageDraw.Draw(img)
    c = -H_
    while c < W_:
        d.line([(c, H_), (c + H_, 0)], fill=ink, width=1); c += sp
    return img
def _pat_dots(size, sp, r, ink, page):
    W_, H_ = size; img = Image.new("L", size, page); d = ImageDraw.Draw(img)
    y = sp // 2
    while y < H_:
        x = sp // 2
        while x < W_:
            d.ellipse([x - r, y - r, x + r, y + r], fill=ink); x += sp
        y += sp
    return img
def _pat_wave(size, sp, ink, page):
    W_, H_ = size; img = Image.new("L", size, page); d = ImageDraw.Draw(img)
    y = sp
    while y < H_:
        pts = [(x, y + math.sin(x / sp * 2 * math.pi) * sp * 0.28) for x in range(0, W_ + 2, 2)]
        d.line(pts, fill=ink, width=1); y += sp
    return img

# --- Dithering (auf dem 8-bit-Graustufen-Bild) -----------------------------
def _gen_bayer(n):
    m = [[0]]
    while len(m) < n:
        s = len(m); nm = [[0] * (s * 2) for _ in range(s * 2)]
        for y in range(s * 2):
            for x in range(s * 2):
                q = (0 if y < s and x < s else 2 if y < s else 3 if x < s else 1)
                nm[y][x] = 4 * m[y % s][x % s] + q
        m = nm
    return m
_BAYER4, _BAYER8 = _gen_bayer(4), _gen_bayer(8)
_FS = [(1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16)]
_ATK = [(1, 0, .125), (2, 0, .125), (-1, 1, .125), (0, 1, .125), (1, 1, .125), (0, 2, .125)]
def _nearest(v, levels):
    best = levels[0]
    for l in levels:
        if abs(v - l) < abs(v - best): best = l
    return best
def _errdiff(buf, levels, kernel):
    for y in range(H):
        for x in range(W):
            i = y * W + x; old = buf[i]; nv = _nearest(old, levels); buf[i] = nv; err = old - nv
            for dx, dy, f in kernel:
                nx, ny = x + dx, y + dy
                if 0 <= nx < W and 0 <= ny < H: buf[ny * W + nx] += err * f

def _apply_dither(img, name):
    if name == "none": return img
    if name == "threshold": return img.point(lambda v: 255 if v >= 128 else 0).convert("1")
    if name == "floyd1": return img.convert("1")
    buf = [float(v) for v in img.getdata()]
    if name == "floyd2":
        _errdiff(buf, (0, 85, 170, 255), _FS)
        out = Image.new("L", (W, H)); out.putdata([int(v) for v in buf]); return out
    if name == "atkinson":
        _errdiff(buf, (0, 255), _ATK)
    elif name in ("bayer4", "bayer8"):
        mat, n = (_BAYER4, 4) if name == "bayer4" else (_BAYER8, 8)
        for y in range(H):
            row = mat[y % n]
            for x in range(W):
                i = y * W + x; t = (row[x % n] + 0.5) / (n * n) * 255
                buf[i] = 255.0 if buf[i] >= t else 0.0
    else:
        return img.convert("1")
    out = Image.new("L", (W, H)); out.putdata([int(v) for v in buf]); return out.convert("1")

def _encode_png(img):
    buf = io.BytesIO(); img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

def render_png(cfg, bounds, edited_ways, edited_nodes, bg_ways):
    minX, minY, maxX, maxY = bounds
    sx = W / (maxX - minX); sy = H / (maxY - minY)
    ps = float(cfg.get("pattern_scale", 1.0)); ls = float(cfg.get("line_scale", 1.0))
    def pt(lo, la): return ((projx(lo) - minX) * sx, (projy(la) - minY) * sy)
    def px(w): return [pt(lo, la) for (lo, la) in w["pts"]]
    def ip(pts): return [(int(round(x)), int(round(y))) for (x, y) in pts]
    def sp_(v): return max(2, int(round(v * ps)))          # Musterabstand skaliert
    def wd(v): return max(1, int(round(v * ls)))            # Linienbreite skaliert
    ink = 255 if cfg["bg"] == "black" else 0
    page = 0 if cfg["bg"] == "black" else 255
    base = Image.new("L", (W, H), page); dr = ImageDraw.Draw(base)
    PATS = {"hatch": _pat_hatch((W, H), sp_(5), ink, page), "stipple": _pat_dots((W, H), sp_(5), 1, ink, page),
            "park": _pat_dots((W, H), sp_(8), 1, ink, page), "wave": _pat_wave((W, H), sp_(7), ink, page)}
    def fillpat(pts, key):
        mask = Image.new("L", (W, H), 0); ImageDraw.Draw(mask).polygon(pts, fill=255)
        base.paste(PATS[key], (0, 0), mask)

    # Flächen
    for w in bg_ways:
        if w["cat"] != "landuse" or cfg["landuse"] == "none": continue
        p = px(w)
        if not _visible(p, VIEW): continue
        poly = ip(_simplify(p, 0.8))
        if cfg["landuse"] == "gray": dr.polygon(poly, fill=int(cfg["landuse_gray"]))
        else: fillpat(poly, "park")
    # Wasser
    if cfg["water"] != "none":
        for w in bg_ways:
            if w["cat"] != "water": continue
            p = px(w)
            if not _visible(p, VIEW): continue
            if w["tags"].get("waterway"):
                dr.line(ip(_simplify(p, 0.8)), fill=ink, width=wd(1.4), joint="curve"); continue
            poly = ip(_simplify(p, 0.8))
            if cfg["water"] == "gray": dr.polygon(poly, fill=int(cfg["water_gray"]))
            else: fillpat(poly, cfg["water"])
    # Straßen + Bahn
    if cfg["roads"] != "none":
        roads = [w for w in bg_ways if w["cat"] and w["cat"].startswith("highway")]
        rp = [(w, ip(_simplify(px(w), 0.8))) for w in roads]
        if cfg["roads"] == "casing":
            for w, p in rp: dr.line(p, fill=ink, width=wd(road_width(w["cat"], cfg["road_width"]) + 2.2), joint="curve")
            for w, p in rp: dr.line(p, fill=page, width=wd(road_width(w["cat"], cfg["road_width"])), joint="curve")
        else:
            col = int(cfg["road_gray"]) if cfg["roads"] == "gray" else ink
            for w, p in rp: dr.line(p, fill=col, width=wd(road_width(w["cat"], cfg["road_width"])), joint="curve")
        for w in bg_ways:
            if w["cat"] == "railway": dr.line(ip(_simplify(px(w), 0.8)), fill=ink, width=wd(1.2), joint="curve")

    # Gebäude
    bm = cfg["buildings"]
    blds = [w for w in bg_ways if w["cat"] == "building"]
    if bm == "3d":
        _extrude_png(dr, base, PATS, blds, px, ip, cfg["b3d"], roof=page, wall="hatch", edge=ink, roof_edge=ink)
    elif bm != "none":
        for w in blds:
            p = px(w)
            if not _visible(p, VIEW): continue
            poly = ip(_simplify(p, 0.8))
            if bm == "gray": dr.polygon(poly, fill=int(cfg["building_gray"]), outline=ink)
            elif bm == "solid": dr.polygon(poly, fill=ink)
            elif bm == "hatch": fillpat(poly, "hatch"); dr.line(poly + [poly[0]], fill=ink, width=wd(0.85))
            else: dr.line(poly + [poly[0]], fill=ink, width=wd(0.85))

    # Bearbeitungen
    e = cfg["edited"]
    ecol = 255 if e["color"] == "white" else 0
    ehalo = None if e["halo"] == "none" else (255 if e["halo"] == "white" else 0)
    if bm == "3d":
        _extrude_png(dr, base, PATS, [w for w in edited_ways if is_closed(w["pts"])], px, ip, cfg["b3d"],
                     roof=ecol, wall=ecol, edge=(page if ehalo is None else ehalo), roof_edge=(page if ehalo is None else ehalo))
        _edited_png(dr, px, pt, ip, edited_ways, edited_nodes, e, ecol, ehalo, ls, only_open=True)
    else:
        _edited_png(dr, px, pt, ip, edited_ways, edited_nodes, e, ecol, ehalo, ls, only_open=False)

    return _encode_png(_apply_dither(base, cfg.get("dither", "threshold")))

def _extrude_png(dr, base, PATS, ways, px, ip, hf, roof, wall, edge, roof_edge):
    items = []
    for w in ways:
        if len(w["pts"]) <= 3: continue
        p = px(w)
        if not _visible(p, VIEW): continue
        sp = _simplify(p, 0.6)
        if len(sp) < 3: continue
        lv = 3.0
        try: lv = float(w["tags"].get("building:levels") or 3)
        except Exception: pass
        items.append((max(q[1] for q in sp), sp, lv))
    items.sort(key=lambda t: t[0])
    wall_is_pat = isinstance(wall, str)
    for _, sp, lv in items:
        hpx = min(28.0, max(6.0, lv * hf)); ox = hpx * 0.42; oy = -hpx * 0.9
        rp = [(x + ox, y + oy) for (x, y) in sp]
        for i in range(len(sp) - 1):
            quad = ip([sp[i], sp[i + 1], rp[i + 1], rp[i]])
            if wall_is_pat:
                mask = Image.new("L", base.size, 0); ImageDraw.Draw(mask).polygon(quad, fill=255); base.paste(PATS[wall], (0, 0), mask)
            else:
                dr.polygon(quad, fill=wall)
        for i in range(len(sp)):
            a = (int(round(sp[i][0])), int(round(sp[i][1]))); b = (int(round(rp[i][0])), int(round(rp[i][1])))
            dr.line([a, b], fill=edge, width=1)
        dr.polygon(ip(rp), fill=roof, outline=roof_edge)

def _edited_png(dr, px, pt, ip, ways, nodes, e, color, halo, ls, only_open):
    lw = max(1, int(round(e["width"] * ls))); nr = e["node"] * ls
    halo_extra = max(2, int(round(4 * ls)))
    prims = []
    for w in ways:
        closed = is_closed(w["pts"])
        if only_open and closed: continue
        prims.append((closed, ip(_simplify(px(w), 0.4))))
    vnodes = [(int(round(x)), int(round(y))) for (x, y) in (pt(lo, la) for (lo, la) in nodes)]
    def passf(col, extra):
        wdt = max(1, lw + extra)
        for closed, p in prims:
            if closed and e["fill"]:
                dr.polygon(p, fill=col)
                dr.line(p + [p[0]], fill=col, width=wdt, joint="curve")
            else:
                dr.line(p, fill=col, width=wdt, joint="curve")
        for (x, y) in vnodes:
            rr = nr + extra / 2.0
            if e["fill"]: dr.ellipse([x - rr, y - rr, x + rr, y + rr], fill=col)
            else: dr.ellipse([x - nr, y - nr, x + nr, y + nr], outline=col, width=wdt)
    if halo is not None: passf(halo, halo_extra)
    passf(color, 0)

# ---- Einstiegspunkt -------------------------------------------------------
def run(input):
    cf = {}
    if isinstance(input, dict):
        cf = (input.get("trmnl", {}).get("plugin_settings", {}) or {}).get("custom_fields_values", {}) or {}
    osm_user = (cf.get("osm_user") or "Till_btn").strip()
    override = (cf.get("changeset_id") or "").strip()
    style = (cf.get("map_style") or "threed").lower()
    mode = (cf.get("render_mode") or "png").lower()
    show_edits = _truthy(cf.get("show_edits"), True)
    show_streak = _truthy(cf.get("show_streak"), True)
    show_kpi = _truthy(cf.get("show_kpi"), True)
    show_header = _truthy(cf.get("show_header"), True)
    show_stats = show_streak or show_kpi   # HDYC nur laden, wenn irgendeine Box es braucht

    # Netzwerkbudget: Server 20s (< 30s-Limit); lokale Snapshot-Erzeugung setzt
    # OSM_DEADLINE_S höher (kein Limit), damit Overpass genug Zeit bekommt.
    deadline = time.monotonic() + float(os.environ.get("OSM_DEADLINE_S") or 20)
    warns = []
    result = {"user": osm_user, "error": "", "warn": "", "map_datauri": "",
              "show_streak": show_streak, "show_kpi": show_kpi,
              "show_edits": show_edits, "show_header": show_header}

    # FATAL: ohne Changeset (bbox) kann keine Karte gerendert werden. Ein Call.
    try:
        meta = get_changeset(osm_user, override, deadline)
        cid = meta.get("id")
        if meta.get("min_lat") is None:
            raise RuntimeError("Changeset #%s hat keine Bounding-Box (leer/gelöscht?)" % cid)
    except Exception as e:
        result["error"] = "Changeset/Metadaten – %s: %s" % (type(e).__name__, e)
        return result

    bounds = compute_bounds(meta)
    bbox = "%s,%s,%s,%s" % (unprojy(bounds[3]), unprojx(bounds[0]), unprojy(bounds[1]), unprojx(bounds[2]))

    # NICHT-FATAL: fehlende Teile -> Warnung, Karte wird trotzdem gerendert.
    way_ids, edited_nodes, edited_ways = [], [], []
    if show_edits:
        try:
            way_ids, edited_nodes = parse_download(cid, deadline)
            edited_ways = fetch_edited_ways(way_ids, deadline)
        except Exception as e:
            warns.append("Bearbeitungen: %s" % e)
    bg = []
    try:
        bg = fetch_bg(bbox, set(way_ids), deadline)
    except Exception as e:
        warns.append("Hintergrund: %s" % e)

    # Style-Config: volle Editor-JSON (map_config) hat Vorrang vor dem Preset.
    map_config_raw = (cf.get("map_config") or "").strip()
    if map_config_raw:
        try:
            cfg = cfg_from_editor(json.loads(map_config_raw))
        except Exception as e:
            warns.append("map_config ungültig, nutze Preset '%s': %s" % (style, e))
            cfg = style_cfg(style)
    else:
        cfg = style_cfg(style)

    # FATAL: Rendern selbst (sollte nicht scheitern).
    try:
        if mode == "png" and _HAVE_PIL:
            datauri = render_png(cfg, bounds, edited_ways, edited_nodes, bg); used = "png"
        else:
            svg = render_svg(cfg, bounds, edited_ways, edited_nodes, bg)
            datauri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
            used = "svg"
    except Exception as e:
        result["error"] = "Rendern – %s: %s" % (type(e).__name__, e)
        return result

    stats = {}
    if show_stats:
        try:
            stats = get_hdyc(osm_user, deadline)
            if stats.get("hdyc_error"): warns.append("HDYC: %s" % stats["hdyc_error"])
        except Exception as e:
            warns.append("HDYC: %s" % e)

    result.update(
        map_datauri=datauri, render_mode_used=used, warn="; ".join(warns),
        changeset_id=str(cid), changeset_user=meta.get("user", osm_user),
        comment=(meta.get("tags", {}) or {}).get("comment", "Bearbeitung"),
        created_at=meta.get("created_at", ""),
        n_created=meta.get("created_count", 0), n_modified=meta.get("modified_count", 0),
        n_bg=len(bg), n_edited=len(edited_ways), style=style,
        **stats,
    )
    c = stats.get("changes", 0)
    result["changes_disp"] = ("%dk" % round(c / 1000)) if c >= 1000 else str(c)
    return result
