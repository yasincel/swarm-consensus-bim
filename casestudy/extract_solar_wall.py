"""Step 1 of the Snowdon Towers case study: extract the Solar Wall inventory from the IFC model.

Usage:
    python extract_solar_wall.py "C:/path/Snowdonia.ifc"

Output: out/solar_wall_inventory.json (one record per facade plate with area, orientation,
storey and host curtain wall). The model is only read, never changed.

What is extracted
  * IfcPlate elements whose type name contains "System Panel:Glazed", "CP_Solar Panel",
    "Solar Wall Edges" or "System Panel:Solid".
  * For each plate: GlobalId, type name, host curtain wall (via IfcRelAggregates), storey,
    face area (m2) and outward normal from the triangulated geometry (world coordinates, metres).
  * Plates are flagged in_solar_wall when the host curtain wall name, or the plate's own type,
    contains "Solar". Glazed panels in other curtain walls are kept but not changed by the
    alternatives, so the change scope stays limited to the Solar Wall.
"""
import json, math, os, sys, time
from collections import Counter

try:
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.element as uel
except ImportError:
    sys.exit("ifcopenshell is missing. Install it with:  py -m pip install ifcopenshell")

TYPE_KEYS = {
    "System Panel:Glazed": "glazed",
    "CP_Solar Panel": "pv",
    "Solar Wall Edges": "edge",
    "System Panel:Solid": "solid",
}


def type_name(el):
    t = uel.get_type(el)
    tn = (t.Name if t is not None and t.Name else "") or ""
    ot = getattr(el, "ObjectType", None) or ""
    nm = el.Name or ""
    # Revit writes "Family:Type:Id" into Name and "Family:Type" into ObjectType
    return " | ".join(x for x in (nm, ot, tn) if x)


def classify(tn):
    for key, cat in TYPE_KEYS.items():
        if key.lower() in tn.lower():
            return cat
    return None


def host_and_storey(el):
    host = uel.get_aggregate(el)
    container = uel.get_container(el) or (uel.get_container(host) if host is not None else None)
    return host, container


def area_and_normal(shape):
    """Area of the largest planar face direction and its unit normal (thin plate: one side)."""
    g = shape.geometry
    v, f = g.verts, g.faces
    buckets = {}
    for i in range(0, len(f), 3):
        a, b, c = f[i] * 3, f[i + 1] * 3, f[i + 2] * 3
        ux, uy, uz = v[b] - v[a], v[b + 1] - v[a + 1], v[b + 2] - v[a + 2]
        wx, wy, wz = v[c] - v[a], v[c + 1] - v[a + 1], v[c + 2] - v[a + 2]
        nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
        n = math.sqrt(nx * nx + ny * ny + nz * nz)
        if n < 1e-12:
            continue
        key = (round(nx / n, 2), round(ny / n, 2), round(nz / n, 2))
        buckets[key] = buckets.get(key, 0.0) + n / 2.0
    if not buckets:
        return None, None
    key = max(buckets, key=buckets.get)
    return buckets[key], key


def orientation(normal, building_centroid, el_centroid):
    nx, ny, nz = normal
    # make the normal point away from the building centroid (outward)
    dx, dy = el_centroid[0] - building_centroid[0], el_centroid[1] - building_centroid[1]
    if nx * dx + ny * dy < 0:
        nx, ny, nz = -nx, -ny, -nz
    tilt = math.degrees(math.acos(max(-1, min(1, abs(nz)))))     # 90 = vertical facade
    az = (math.degrees(math.atan2(nx, ny)) + 360) % 360           # 0 = +Y (model north), 180 = south
    return round(az, 1), round(tilt, 1)


def main():
    if len(sys.argv) < 2:
        sys.exit('Usage: python extract_solar_wall.py "path/to/model.ifc"')
    t0 = time.time()
    model = ifcopenshell.open(sys.argv[1])
    print(f"Model opened ({model.schema}) in {time.time()-t0:.0f}s")

    plates = []
    for el in model.by_type("IfcPlate"):
        tn = type_name(el)
        cat = classify(tn)
        if cat:
            plates.append((el, tn, cat))
    print("Facade plates found:", Counter(c for _, _, c in plates))
    if not plates:
        sys.exit("No matching plates. Send me the console output and model_summary.json.")

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    records, failed = [], 0
    for el, tn, cat in plates:
        host, storey = host_and_storey(el)
        host_name = " | ".join(x for x in ((host.Name if host else ""), (getattr(host, "ObjectType", "") if host else "")) if x)
        rec = dict(guid=el.GlobalId, category=cat, type_name=tn, host=host_name,
                   host_guid=host.GlobalId if host else None,
                   storey=storey.Name if storey is not None and hasattr(storey, "Name") else None)
        try:
            shape = ifcopenshell.geom.create_shape(settings, el)
            area, normal = area_and_normal(shape)
            v = shape.geometry.verts
            n = len(v) // 3
            rec["centroid"] = [round(sum(v[i::3]) / n, 3) for i in range(3)]
            rec["area_m2"] = round(area, 4) if area else None
            rec["normal"] = list(normal) if normal else None
        except Exception as ex:                                       # noqa: BLE001
            failed += 1
            rec.update(area_m2=None, normal=None, centroid=None, geom_error=str(ex)[:120])
        rec["in_solar_wall"] = ("solar" in host_name.lower()) or ("solar" in tn.lower())
        records.append(rec)

    # true north: rotate model azimuths so that 0 = true north
    tn_deg = 0.0
    for ctx in model.by_type("IfcGeometricRepresentationContext"):
        tn = getattr(ctx, "TrueNorth", None)
        if tn is not None and tn.DirectionRatios:
            x, y = tn.DirectionRatios[0], tn.DirectionRatios[1]
            tn_deg = math.degrees(math.atan2(x, y))
            break
    print(f"True north offset: {tn_deg:.1f} deg")

    pts = [r["centroid"] for r in records if r.get("centroid")]
    bc = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
    for r in records:
        if r.get("normal") and r.get("centroid"):
            az, tilt = orientation(r["normal"], bc, r["centroid"])
            r["azimuth_deg"], r["tilt_deg"] = round((az - tn_deg) % 360, 1), tilt

    os.makedirs("out", exist_ok=True)
    summary = {
        "model": os.path.basename(sys.argv[1]),
        "schema": model.schema,
        "plates": len(records),
        "geometry_failures": failed,
        "true_north_offset_deg": round(tn_deg, 2),
        "by_category": dict(Counter(r["category"] for r in records)),
        "in_solar_wall_by_category": dict(Counter(r["category"] for r in records if r["in_solar_wall"])),
        "area_m2_by_category_in_solar_wall": {
            c: round(sum(r["area_m2"] or 0 for r in records if r["in_solar_wall"] and r["category"] == c), 2)
            for c in TYPE_KEYS.values()},
        "hosts": dict(Counter(r["host"] for r in records).most_common(15)),
    }
    json.dump({"summary": summary, "plates": records}, open("out/solar_wall_inventory.json", "w"), indent=1)
    print(json.dumps(summary, indent=2))
    print(f"Written out/solar_wall_inventory.json in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
