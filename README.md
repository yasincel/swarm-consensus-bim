# When does swarm consensus help?

Code and data for the paper *When does swarm consensus help? Honeybee quorum consensus for multi-stakeholder BIM change approval on blockchain*.

The repository contains three parts:

| Folder | Contents |
| --- | --- |
| `simulation/` | Agent-based simulation (v3) with role-specific criteria, budget-matched and audit-matched baselines, analysis scripts, results and figures |
| `contract/` | `HQC2.sol` (Solidity 0.8.24): quality-weighted endorsements, re-dancing, abandonment, stop signals, decay, role-aware quorum, veto, stakes, evidence challenges and slashing; Hardhat tests; case-study driver |
| `casestudy/` | Snowdon Towers Solar Wall case study: IFC extraction, IFC deltas, deterministic role checks, evidence hashes, sensitivity analysis |

No human participants are involved; all stakeholder roles are deterministic checks or simulated agents.

## Requirements

- Python 3.9–3.12 with `numpy`, `pandas`, `scipy`, `matplotlib` (simulation) and `ifcopenshell` (case study)
- Node.js 18 or later (contract)

## Reproduce

### 1. Simulation (about 5 minutes on 2 cores)

```
cd simulation
pip install -r requirements.txt
python hqc_sim_v3.py 300        # calibration on disjoint seeds, then the full grid -> results/sim_runs_v3.csv.gz
python pareto_v3.py             # accuracy-effort, no-carry-over and audit-matched runs -> results/pareto_v3.csv.gz
python analyse_v3.py            # tables, McNemar tests, Fig. 3
python make_pareto_figure.py    # Fig. 2
python fig1_state_machine.py    # Fig. 1
```

### 2. Smart contract

```
cd contract
npm install
npx hardhat test                               # 9 tests, writes results/gas_results_v2.json
```

### 3. Case study

The Snowdon Towers model is an Autodesk sample file and is not included. Export it to IFC2X3 and run:

```
cd casestudy
pip install -r requirements.txt
python extract_solar_wall.py path/to/Snowdonia.ifc   # -> out/solar_wall_inventory.json
python role_checks.py                                # -> out/case_scores.json, deltas, evidence, sensitivity
cd ../contract
npx hardhat run scripts/casestudy.js                 # theta = 0.5
THETA=0.4 npx hardhat run scripts/casestudy.js       # sensitivity (PowerShell: $env:THETA="0.4")
```

Optionally, `python casestudy/apply_delta.py path/to/Snowdonia.ifc A4` checks a delta against its on-chain hash and writes the change into a copy of the model.

The parameters in `casestudy/config.json` are illustrative assumptions (see Appendix A of the paper).

## Results included

- `simulation/results/`: raw runs, calibration, frozen parameters, main table, McNemar tests
- `casestudy/out/`: inventory, deltas, evidence reports, scores, sensitivity
- `contract/results/`: gas reports and case-study outcomes for theta = 0.5 and 0.4

## License

MIT. See `LICENSE`.
