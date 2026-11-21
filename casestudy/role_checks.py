"""Step 2 of the Snowdon Towers case study: define the alternatives as IFC deltas and run the
deterministic role checks.

Usage:
    python role_checks.py                 # uses out/solar_wall_inventory.json and config.json

Outputs (folder out/):
    deltas/A0..A3.json          IFC delta of each alternative (GlobalIds and property changes)
    evidence/<role>_<alt>.json  evidence report of every role check (inputs, formula, result)
    case_scores.json            criterion scores (0..1000), SHA-256 hashes, weighted ranking
    sensitivity.json            ranking stability when each parameter changes by +/-20 %

Alternatives (change request: "upgrade the Solar Wall facade")
    A0  Baseline: keep the current envelope.
    A1  BIPV infill: the glazed curtain-wall system panels become semi-transparent BIPV glass.
    A2  Triple glazing: the glazed curtain-wall system panels become triple-glazed units.
    A3  PV repowering: the CP_Solar Panel modules of the Solar Panels curtain walls are replaced
        with high-efficiency modules.
    A4  PV extension: the opaque "Solar Wall Edges" panels of the same curtain walls become PV
        modules.
    (In the Snowdon Towers model the glazed panels belong to the stair glazing walls and the
    storefronts, not to the Solar Panels curtain walls, so the envelope-wide scope is used.)

Criteria (one per role; scores 0..1000, higher is better)
    architect       daylight/view proxy: area-weighted visible transmittance relative to baseline
                    (judgement-based in practice, so treated as NOT verifiable on-chain)
    structural      added facade dead load against an allowance (verifiable)
    mep             annual operational energy saving, PV yield + reduced heat loss (verifiable)
    cost            capital cost against the budget (verifiable)
    sustainability  net life-cycle carbon saving against the owner's carbon target (verifiable)
    client          undiscounted life-cycle financial balance (savings minus capital cost);
                    the owner's valuation is preference-based, so treated as NOT verifiable
"""
import copy, hashlib, json, math, os, sys

ROLES = ["architect", "structural", "mep", "cost", "sustainability", "client"]
VERIFIABLE = {"structural", "mep", "cost", "sustainability"}
ALTS = {
    "A0": "Baseline (no change)",
    "A1": "BIPV glass infill on curtain-wall glazed panels",
    "A2": "Triple glazing on curtain-wall glazed panels",
    "A3": "High-efficiency repowering of existing PV panels",
    "A4": "PV modules on the opaque Solar Wall edge panels",
}


def sha256(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def irradiance_factor(rec, cfg):
    f = cfg["climate"]["facade_irradiance_factor"]
    tilt, az = rec.get("tilt_deg"), rec.get("azimuth_deg")
    if tilt is None or az is None:
        return f["east_west"]                      # unknown orientation -> middle value
    if tilt < 30:
        return f["roof"]
    d = min(abs(az - 180), 360 - abs(az - 180))   # angular distance from south
    if d <= 45:
        return f["south"]
    if d >= 135:
        return f["north"]
    return f["east_west"]


def scope(inv):
    """Glazed curtain-wall panels (whole envelope), PV modules and Solar Wall edge panels."""
    plates = [p for p in inv["plates"] if p.get("area_m2")]
    glazed = [p for p in plates if p["category"] == "glazed"]
    pv = [p for p in plates if p["category"] == "pv"]
    edge = [p for p in plates if p["category"] == "edge"]
    return glazed, pv, edge


def make_deltas(glazed, pv, edge):
    def change(ps, new_type):
        return [{"GlobalId": p["guid"], "set": {"Pset_HQC_Alternative.Product": new_type}} for p in ps]
    return {
        "A0": {"alternative": "A0", "description": ALTS["A0"], "changes": []},
        "A1": {"alternative": "A1", "description": ALTS["A1"], "changes": change(glazed, "bipv_glass")},
        "A2": {"alternative": "A2", "description": ALTS["A2"], "changes": change(glazed, "glazed_triple")},
        "A3": {"alternative": "A3", "description": ALTS["A3"], "changes": change(pv, "pv_high_eff")},
        "A4": {"alternative": "A4", "description": ALTS["A4"], "changes": change(edge, "pv_high_eff")},
    }


def quantities(alt, glazed, pv, edge, cfg):
    """Physical quantities of an alternative relative to the baseline."""
    P, E, C = cfg["products"], cfg["energy"], cfg["climate"]
    ghi, hdd = C["ghi_kwh_m2_yr"], C["heating_degree_days_Kd"]
    a_gl = sum(p["area_m2"] for p in glazed)
    a_pv = sum(p["area_m2"] for p in pv)
    irr_gl = sum(p["area_m2"] * irradiance_factor(p, cfg) for p in glazed) * ghi   # kWh/yr on glazed area
    irr_pv = sum(p["area_m2"] * irradiance_factor(p, cfg) for p in pv) * ghi
    a_ed = sum(p["area_m2"] for p in edge)
    irr_ed = sum(p["area_m2"] * irradiance_factor(p, cfg) for p in edge) * ghi
    q = dict(area_changed_m2=0.0, pv_kwh_yr=0.0, heat_kwh_yr=0.0, capex_eur=0.0, embodied_t=0.0,
             added_load_kg_m2=0.0, vlt_ratio=1.0, glazed_area_m2=round(a_gl, 2), pv_area_m2=round(a_pv, 2), edge_area_m2=round(a_ed, 2))
    base = P["glazed_double"]
    if alt in ("A1", "A2"):
        prod = P["bipv_glass" if alt == "A1" else "glazed_triple"]
        q["area_changed_m2"] = a_gl
        q["pv_kwh_yr"] = irr_gl * prod["pv_eff"] * E["pv_performance_ratio"]
        q["heat_kwh_yr"] = (base["u_w_m2k"] - prod["u_w_m2k"]) * a_gl * hdd * 24 / 1000 / E["heating_system_efficiency"]
        q["capex_eur"] = a_gl * (prod["cost_eur_m2"] + P["removal_cost_eur_m2"])
        q["embodied_t"] = a_gl * prod["ec_kg_m2"] / 1000
        q["added_load_kg_m2"] = prod["mass_kg_m2"] - base["mass_kg_m2"]
        q["vlt_ratio"] = prod["vlt"] / base["vlt"]
    elif alt == "A3":
        new, old = P["pv_high_eff"], P["pv_existing"]
        q["area_changed_m2"] = a_pv
        q["pv_kwh_yr"] = irr_pv * (new["pv_eff"] - old["pv_eff"]) * E["pv_performance_ratio"]
        q["capex_eur"] = a_pv * (new["cost_eur_m2"] + P["removal_cost_eur_m2"])
        q["embodied_t"] = a_pv * new["ec_kg_m2"] / 1000
        q["added_load_kg_m2"] = new["mass_kg_m2"] - old["mass_kg_m2"]
    elif alt == "A4":
        new, old = P["pv_high_eff"], P["edge_existing"]
        q["area_changed_m2"] = a_ed
        q["pv_kwh_yr"] = irr_ed * new["pv_eff"] * E["pv_performance_ratio"]
        q["capex_eur"] = a_ed * (new["cost_eur_m2"] + P["removal_cost_eur_m2"])
        q["embodied_t"] = a_ed * new["ec_kg_m2"] / 1000
        q["added_load_kg_m2"] = new["mass_kg_m2"] - old["mass_kg_m2"]
    q["savings_eur_yr"] = q["pv_kwh_yr"] * E["electricity_price_eur_kwh"] + q["heat_kwh_yr"] * E["heating_fuel_price_eur_kwh"]
    q["operational_t_yr"] = (q["pv_kwh_yr"] * E["grid_carbon_kg_kwh"] + q["heat_kwh_yr"] * E["heating_fuel_carbon_kg_kwh"]) / 1000
    q["net_carbon_saving_t"] = q["operational_t_yr"] * cfg["lifetime_years"] - q["embodied_t"]
    q["payback_yr"] = (q["capex_eur"] / q["savings_eur_yr"]) if q["savings_eur_yr"] > 0 else None
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in q.items()}


def clip01(x):
    return max(0.0, min(1.0, x))


def criterion_scores(q, cfg):
    S, L = cfg["scoring"], cfg["lifetime_years"]
    kwh = q["pv_kwh_yr"] + q["heat_kwh_yr"]
    s = {
        "architect": clip01(q["vlt_ratio"]),
        "structural": clip01(1 - max(0.0, q["added_load_kg_m2"]) / S["structural_allowance_kg_m2"]),
        "mep": kwh / (kwh + S["mep_reference_kwh_yr"]) if kwh > 0 else 0.0,
        "cost": clip01(1 - q["capex_eur"] / S["cost_budget_eur"]),
        "sustainability": clip01(q["net_carbon_saving_t"] / S["carbon_target_t"]),
        "client": clip01(0.5 + 0.5 * (q["savings_eur_yr"] * L - q["capex_eur"]) / S["client_reference_eur"]),
    }
    return {r: int(round(1000 * v)) for r, v in s.items()}


FORMULAS = {
    "architect": "min(1, VLT_new / VLT_baseline)",
    "structural": "1 - max(0, added_load_kg_m2) / structural_allowance_kg_m2",
    "mep": "kWh / (kWh + mep_reference_kwh_yr), kWh = PV yield + heating saving",
    "cost": "1 - capex_eur / cost_budget_eur",
    "sustainability": "min(1, max(0, net_carbon_saving_t / carbon_target_t))",
    "client": "0.5 + 0.5 * (savings_eur_yr * lifetime_years - capex_eur) / client_reference_eur (status quo = 0.5)",
}


def evaluate(cfg, glazed, pv, edge):
    res = {}
    for a in ALTS:
        q = quantities(a, glazed, pv, edge, cfg)
        res[a] = {"quantities": q, "scores": criterion_scores(q, cfg)}
    w = cfg["scoring"]["weights"]
    for a in res:
        res[a]["weighted"] = round(sum(w[r] * res[a]["scores"][r] for r in ROLES) / sum(w.values()), 1)
    ranking = sorted(res, key=lambda a: -res[a]["weighted"])
    return res, ranking


def leaf_paths(d, prefix=()):
    for k, v in d.items():
        if k.startswith("_") or k == "weights":
            continue
        if isinstance(v, dict):
            yield from leaf_paths(v, prefix + (k,))
        elif isinstance(v, (int, float)) and v != 0:
            yield prefix + (k,)


def set_path(d, path, value):
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value


def get_path(d, path):
    for k in path:
        d = d[k]
    return d


def sensitivity(cfg, glazed, pv, edge, base_rank):
    out = []
    for path in leaf_paths(cfg):
        for f in (0.8, 1.2):
            c = copy.deepcopy(cfg)
            set_path(c, path, get_path(cfg, path) * f)
            _, rank = evaluate(c, glazed, pv, edge)
            out.append({"parameter": ".".join(path), "factor": f, "winner": rank[0],
                        "ranking": rank, "winner_changed": rank[0] != base_rank[0]})
    # weights: each role's weight doubled, one at a time
    for r in ROLES:
        c = copy.deepcopy(cfg)
        c["scoring"]["weights"][r] *= 2
        _, rank = evaluate(c, glazed, pv, edge)
        out.append({"parameter": f"weight.{r}", "factor": 2.0, "winner": rank[0], "ranking": rank,
                    "winner_changed": rank[0] != base_rank[0]})
    return out


def main():
    inv_path = sys.argv[1] if len(sys.argv) > 1 else "out/solar_wall_inventory.json"
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))
    inv = json.load(open(inv_path))
    glazed, pv, edge = scope(inv)
    if not glazed or not pv or not edge:
        sys.exit(f"Scope problem: {len(glazed)} glazed, {len(pv)} PV, {len(edge)} edge panels.")
    print(f"Scope: {len(glazed)} glazed panels ({sum(p['area_m2'] for p in glazed):.1f} m2), "
          f"{len(pv)} PV panels ({sum(p['area_m2'] for p in pv):.1f} m2), "
          f"{len(edge)} edge panels ({sum(p['area_m2'] for p in edge):.1f} m2)")

    os.makedirs("out/deltas", exist_ok=True)
    os.makedirs("out/evidence", exist_ok=True)
    deltas = make_deltas(glazed, pv, edge)
    alt_hash = {}
    for a, d in deltas.items():
        json.dump(d, open(f"out/deltas/{a}.json", "w"), indent=1)
        alt_hash[a] = sha256(d)

    res, ranking = evaluate(cfg, glazed, pv, edge)
    evidence_hash = {}
    for a in ALTS:
        for r in ROLES:
            ev = {"alternative": a, "alt_hash": alt_hash[a], "role": r, "verifiable": r in VERIFIABLE,
                  "formula": FORMULAS[r], "inputs": res[a]["quantities"], "parameters_hash": sha256(cfg),
                  "score": res[a]["scores"][r]}
            json.dump(ev, open(f"out/evidence/{r}_{a}.json", "w"), indent=1)
            evidence_hash.setdefault(a, {})[r] = sha256(ev)

    sens = sensitivity(cfg, glazed, pv, edge, ranking)
    stable = sum(1 for s in sens if not s["winner_changed"]) / len(sens)
    out = {"alternatives": ALTS, "alt_hash": alt_hash, "results": res, "ranking_weighted_sum": ranking,
           "evidence_hash": evidence_hash, "verifiable_roles": sorted(VERIFIABLE),
           "sensitivity_winner_stability": round(stable, 3)}
    json.dump(out, open("out/case_scores.json", "w"), indent=1)
    json.dump(sens, open("out/sensitivity.json", "w"), indent=1)

    print("\nCriterion scores (0..1000):")
    print("alt  " + " ".join(f"{r[:6]:>7}" for r in ROLES) + "  weighted")
    for a in ALTS:
        print(f"{a}   " + " ".join(f"{res[a]['scores'][r]:>7}" for r in ROLES) + f"  {res[a]['weighted']:>8}")
    print("Ranking (full-review weighted sum):", " > ".join(ranking))
    print(f"Winner unchanged in {stable:.0%} of {len(sens)} sensitivity cases")
    changed = [s for s in sens if s["winner_changed"]]
    for s in changed[:10]:
        print(f"  winner -> {s['winner']} when {s['parameter']} x {s['factor']}")


if __name__ == "__main__":
    main()
