"""Optional: write the committed alternative back into the IFC model.

Usage:
    python apply_delta.py "C:/path/Snowdonia.ifc" A1

Reads out/deltas/A1.json, checks its SHA-256 against out/case_scores.json (the hash that was
put on-chain), adds Pset_HQC_Alternative to every changed element and saves
out/Snowdonia_A1.ifc. Geometry is not changed; the delta records the product change only.
"""
import hashlib, json, os, sys

try:
    import ifcopenshell
    import ifcopenshell.api
except ImportError:
    sys.exit("ifcopenshell is missing. Install it with:  py -m pip install ifcopenshell")


def main():
    if len(sys.argv) < 3:
        sys.exit('Usage: python apply_delta.py "path/to/model.ifc" A1')
    ifc_path, alt = sys.argv[1], sys.argv[2]
    delta = json.load(open(f"out/deltas/{alt}.json"))
    onchain = json.load(open("out/case_scores.json"))["alt_hash"][alt]
    digest = hashlib.sha256(json.dumps(delta, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != onchain:
        sys.exit("Hash mismatch: the delta file differs from the alternative that was approved.")
    model = ifcopenshell.open(ifc_path)
    done = 0
    for ch in delta["changes"]:
        el = model.by_guid(ch["GlobalId"])
        pset = ifcopenshell.api.run("pset.add_pset", model, product=el, name="Pset_HQC_Alternative")
        props = {k.split(".", 1)[1]: v for k, v in ch["set"].items()}
        props.update({"Alternative": alt, "DeltaSHA256": digest})
        ifcopenshell.api.run("pset.edit_pset", model, pset=pset, properties=props)
        done += 1
    out = os.path.join("out", f"{os.path.splitext(os.path.basename(ifc_path))[0]}_{alt}.ifc")
    model.write(out)
    print(f"{done} elements updated; written {out}")


if __name__ == "__main__":
    main()
