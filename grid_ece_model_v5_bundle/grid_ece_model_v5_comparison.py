"""Grid ECE v4 stress-test wrapper for grid_ece_model_v3.py.

Runs reproducible synthetic tests for arrival surges, tight deadlines, varied vehicle
power acceptance, higher utilization, and a conservative cloud-loss fallback proxy.
It does not emulate ABB firmware or certify electrical safety. All workloads are synthetic.
Python 3.8+.
"""
from pathlib import Path
import csv
import importlib.util
import copy

HERE = Path(__file__).resolve().parent
V3_PATH = HERE / 'grid_ece_model_v3.py'
spec = importlib.util.spec_from_file_location('grid_ece_model_v3', str(V3_PATH))
v3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3)


def write_csv(path, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for row in rows for k in row.keys()))
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def run_case(name, arrivals=None, session_energy=None, session_max_power=None,
             dwell_override=None, mode_override=None, cloud_loss=False):
    # Restore the model's original inputs before every independent test.
    v3.SYNTHETIC_ARRIVAL_BLOCKS = copy.deepcopy(arrivals if arrivals is not None else ORIGINAL_ARRIVALS)
    v3.SESSION_ENERGY_KWH = ORIGINAL_ENERGY if session_energy is None else session_energy
    v3.SESSION_MAX_DC_KW = ORIGINAL_MAX_POWER if session_max_power is None else session_max_power
    v3.STATIC_CAP_PER_CHARGER_DC_KW = ORIGINAL_STATIC_CAP
    # Override session dwell in the session generator when a deadline stress test is requested.
    if dwell_override is not None:
        original_make_sessions = v3.make_sessions
        def make_short_dwell_sessions():
            rows = original_make_sessions()
            dwell_intervals = max(1, int(round(dwell_override * 60 / v3.INTERVAL_MINUTES)))
            for row in rows:
                row['departure_interval'] = min(v3.SIMULATION_DAY_HOURS * 60 // v3.INTERVAL_MINUTES, row['arrival_interval'] + dwell_intervals)
            return rows
        v3.make_sessions = make_short_dwell_sessions
    else:
        v3.make_sessions = ORIGINAL_MAKE_SESSIONS
    budget, intervals, sessions, summary = v3.simulate()
    v3.make_sessions = ORIGINAL_MAKE_SESSIONS
    # A conservative cloud-loss proxy: local control continues with a static cap.
    # This is a test of a fallback assumption, NOT evidence that ABB supports it.
    if cloud_loss:
        intervals = [dict(r, scenario='cloud_loss_static_fallback_proxy') for r in intervals if r['scenario'] == 'static_cap']
        sessions = [dict(r, scenario='cloud_loss_static_fallback_proxy') for r in sessions if r['scenario'] == 'static_cap']
        s = next(r for r in summary if r['scenario'] == 'static_cap').copy()
        s['scenario'] = 'cloud_loss_static_fallback_proxy'
        s['status'] = 'SYNTHETIC FALLBACK PROXY ONLY; vendor confirmation and fault testing required'
        return {'case':name, 'scenario':s['scenario'], **{k:v for k,v in s.items() if k!='scenario'}}, intervals, sessions
    chosen = mode_override or 'dynamic_site_cap'
    s = next(r for r in summary if r['scenario'] == chosen).copy()
    return {'case':name, 'scenario':s['scenario'], **{k:v for k,v in s.items() if k!='scenario'}}, [r for r in intervals if r['scenario']==chosen], [r for r in sessions if r['scenario']==chosen]

ORIGINAL_MAKE_SESSIONS = v3.make_sessions
ORIGINAL_ARRIVALS = copy.deepcopy(v3.SYNTHETIC_ARRIVAL_BLOCKS)
ORIGINAL_ENERGY = v3.SESSION_ENERGY_KWH
ORIGINAL_MAX_POWER = v3.SESSION_MAX_DC_KW
ORIGINAL_STATIC_CAP = v3.STATIC_CAP_PER_CHARGER_DC_KW


def main():
    cases = []
    interval_rows = []
    session_rows = []
    # Baseline: same synthetic workload as v3.
    tests = [
        ('baseline_workload', None, None, None, None, None, False),
        ('arrival_surge', [(7.0, 12, 1.0), (11.0, 2, 1.0), (16.0, 12, 1.0), (18.0, 2, .75)], None, None, None, None, False),
        ('tight_deadlines', None, None, None, .50, None, False),
        ('lower_vehicle_acceptance', None, None, 150.0, None, None, False),
        ('higher_energy_need', None, 150.0, None, None, None, False),
        ('cloud_loss_static_fallback_proxy', None, None, None, None, 'static_cap', True),
    ]
    for name, arrivals, energy, max_power, dwell, mode, cloud_loss in tests:
        result, intervals, sessions = run_case(name, arrivals, energy, max_power, dwell, mode, cloud_loss)
        cases.append(result)
        interval_rows.extend(dict(test_case=name, **r) for r in intervals)
        session_rows.extend(dict(test_case=name, **r) for r in sessions)

    # Compare static vs dynamic on identical inputs for each workload.
    # Apply the tight-deadline dwell override here too. The earlier V4 comparison
    # omitted that override, so its tight_deadlines comparison was not valid.
    comparison_rows = []
    for name, arrivals, energy, max_power, dwell, mode, cloud_loss in tests[:5]:
        for strategy in ('static_cap', 'dynamic_site_cap'):
            v3.SYNTHETIC_ARRIVAL_BLOCKS = copy.deepcopy(arrivals if arrivals is not None else ORIGINAL_ARRIVALS)
            v3.SESSION_ENERGY_KWH = ORIGINAL_ENERGY if energy is None else energy
            v3.SESSION_MAX_DC_KW = ORIGINAL_MAX_POWER if max_power is None else max_power
            v3.STATIC_CAP_PER_CHARGER_DC_KW = ORIGINAL_STATIC_CAP
            if dwell is not None:
                original_make_sessions = v3.make_sessions
                def make_short_dwell_sessions():
                    rows = original_make_sessions()
                    dwell_intervals = max(1, int(round(dwell * 60 / v3.INTERVAL_MINUTES)))
                    for row in rows:
                        row['departure_interval'] = min(
                            v3.SIMULATION_DAY_HOURS * 60 // v3.INTERVAL_MINUTES,
                            row['arrival_interval'] + dwell_intervals
                        )
                    return rows
                v3.make_sessions = make_short_dwell_sessions
            else:
                v3.make_sessions = ORIGINAL_MAKE_SESSIONS
            _, _, _, summary = v3.simulate()
            v3.make_sessions = ORIGINAL_MAKE_SESSIONS
            s = next(r for r in summary if r['scenario'] == strategy)
            comparison_rows.append({
                'test_case': name,
                **s,
                'status': 'SYNTHETIC SIMULATION ONLY; not field results or ABB controller behavior',
                'interpretation': 'Same workload inputs used for static and dynamic strategies.'
            })

    write_csv(HERE/'grid_ece_v5_stress_summary.csv', cases)
    write_csv(HERE/'grid_ece_v5_stress_intervals.csv', interval_rows)
    write_csv(HERE/'grid_ece_v5_stress_sessions.csv', session_rows)
    write_csv(HERE/'grid_ece_v5_strategy_comparison.csv', comparison_rows)
    print('GRID ECE V5 STRESS TESTS')
    print('All workloads are synthetic. Cloud-loss row is a conservative static-fallback proxy, not verified hardware behavior.')
    for r in cases:
        print('{}: strategy={}, peak={:.1f} kW AC, delivered={:.1f} kWh, completion={:.1f}%, over-ceiling intervals={}'.format(
            r['case'], r.get('scenario',''), float(r.get('peak_total_site_ac_kw_est',0) or 0),
            float(r.get('total_delivered_charging_kwh',0) or 0), float(r.get('session_completion_rate_pct',0) or 0),
            r.get('intervals_over_operating_ceiling','')))
    print('Created grid_ece_v5_stress_summary.csv, grid_ece_v5_stress_intervals.csv, grid_ece_v5_stress_sessions.csv, grid_ece_v5_strategy_comparison.csv')

if __name__ == '__main__':
    main()
