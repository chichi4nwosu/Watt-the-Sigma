"""Grid ECE model v3: site sizing, charging allocation, validation, costs, emissions.

Purpose: transparent screening model for a proposed Québec public DC fast-charging site.
This is NOT an electrical design, ABB controller emulator, tariff calculator, or measured
pilot result. Default scenario inputs are illustrative and are explicitly exported as assumptions.

Python 3.8+. matplotlib is optional; CSV outputs are created without it.
"""
from pathlib import Path
import csv
import math
from collections import defaultdict

try:
    import matplotlib.pyplot as plt
except ImportError:
    plt = None

OUT = Path(__file__).resolve().parent

# --------------------------- CONFIGURABLE INPUTS ---------------------------
# Source-informed hardware assumptions from the team's research notes. Confirm against
# the current North American ABB datasheet and exact installed configuration before use.
CHARGER_COUNT = 4
OUTLETS_PER_CHARGER = 2
CHARGER_MAX_DC_KW = 400.0
PORT_MIN_DC_KW = 100.0
POWER_STEP_KW = 50.0
CHARGER_EFFICIENCY = 0.965  # total-system efficiency in project notes, verify exact datasheet

# Illustrative site assumptions. Replace with a selected site's accepted connection limit,
# 15-minute interval meter data, other loads, and electrical engineer/utility limits.
SITE_LIMIT_AC_KW = 1000.0
SAFETY_MARGIN_KW = 50.0
OTHER_LOAD_DEFAULT_KW = 100.0

# Synthetic workload used only to compare control strategies. It is NOT observed site data.
SIMULATION_DAY_HOURS = 24
INTERVAL_MINUTES = 15
SESSION_ENERGY_KWH = 100.0
SESSION_MAX_DC_KW = 400.0
# [arrival hour, number of arriving vehicles, dwell hours]
SYNTHETIC_ARRIVAL_BLOCKS = [
    # Deliberate bursts stress the site connection. Synthetic, not observed arrivals.
    (7.00, 8, 1.00), (11.00, 2, 1.00), (12.00, 1, 0.75),
    (16.00, 8, 1.00), (18.00, 2, 0.75),
]
STATIC_CAP_PER_CHARGER_DC_KW = 200.0  # illustrative fixed cap per dispenser, not a vendor setting

# Economics. Leave tariff demand-charge at zero until the selected operator's actual tariff
# and bills establish whether/how demand is billed. Rate BR eligibility and bill structure
# need checking for the actual public charging installation.
ELECTRICITY_PRICE_CAD_PER_KWH = 0.12  # placeholder only, NOT a verified Hydro-Québec tariff
DEMAND_CHARGE_CAD_PER_KW_MONTH = 0.0  # zero until confirmed by actual tariff/bill
BILLING_MONTHS = 12

# Emissions factor: identify factor year and method. Use the Canada.ca 2026 Québec factor
# cited in project notes as an annual-consumption factor, not a marginal hourly factor.
QUEBEC_GRID_G_CO2E_PER_KWH = 1.9
EMISSIONS_FACTOR_YEAR = 2026
EMISSIONS_FACTOR_METHOD = 'annual electricity-consumption factor, not hourly marginal'

# ------------------------------ UTILITIES ----------------------------------
def write_csv(path, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for row in rows for k in row.keys()))
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def roundn(x, n=3):
    return round(float(x), n)


def site_power_budget(site_limit_ac_kw, other_load_kw, margin_kw, efficiency):
    allowed_total_site_kw = max(0.0, site_limit_ac_kw - margin_kw)
    charger_ac_budget = max(0.0, allowed_total_site_kw - other_load_kw)
    return {
        'site_connection_limit_ac_kw': site_limit_ac_kw,
        'safety_margin_kw_assumed': margin_kw,
        'other_site_load_kw_assumed': other_load_kw,
        'operating_ceiling_site_ac_kw': allowed_total_site_kw,
        'charger_input_budget_ac_kw': charger_ac_budget,
        'estimated_dc_charging_budget_kw': charger_ac_budget * efficiency,
        'status': 'ILLUSTRATIVE, replace with site-specific electrical data',
    }


def make_sessions():
    """Create deterministic synthetic sessions in 15-minute time steps."""
    sessions = []
    idx = 1
    for arrival_hour, count, dwell_hours in SYNTHETIC_ARRIVAL_BLOCKS:
        for j in range(count):
            # Small offset avoids every arrival in a block being identical, while remaining deterministic.
            arrival = int(round(arrival_hour * 60 / INTERVAL_MINUTES)) + (j % 2)
            dwell = max(1, int(round(dwell_hours * 60 / INTERVAL_MINUTES)))
            sessions.append({
                'session_id': 'S{:03d}'.format(idx),
                'arrival_interval': arrival,
                'departure_interval': min(SIMULATION_DAY_HOURS * 60 // INTERVAL_MINUTES, arrival + dwell),
                'energy_requested_kwh': SESSION_ENERGY_KWH,
                'energy_delivered_kwh': 0.0,
                'max_dc_kw': SESSION_MAX_DC_KW,
                'charger_id': (idx - 1) % CHARGER_COUNT,
                'port_id': (idx - 1) % OUTLETS_PER_CHARGER,
                'completed_by_departure': False,
                'first_power_interval': None,
            })
            idx += 1
    return sessions


def allocate_unmanaged(active, charger_caps):
    """Unmanaged charging: share each A400's 400 kW across its active outlets,
    but do not apply a site-wide limit. Power is split equally and rounded down
    to the documented 50 kW increment, subject to the 100 kW minimum.
    """
    allocations = {s['session_id']: 0.0 for s in active}
    by_charger = defaultdict(list)
    for s in active:
        by_charger[s['charger_id']].append(s)
    for cid, sessions in by_charger.items():
        share = charger_caps[cid] / max(1, len(sessions))
        for s in sessions:
            energy_left = max(0.0, s['energy_requested_kwh'] - s['energy_delivered_kwh'])
            desired = min(s['max_dc_kw'], share, energy_left * 60.0 / INTERVAL_MINUTES)
            desired = math.floor(desired / POWER_STEP_KW) * POWER_STEP_KW
            if desired >= PORT_MIN_DC_KW:
                allocations[s['session_id']] = desired
    return allocations


def allocate_capped(active, charger_caps, site_dc_budget_kw, mode):
    """Simplified safe allocation with 50 kW increments and a minimum 100 kW.

    First give each feasible active session its minimum, then distribute additional
    increments by earliest departure. Static mode has a fixed per-charger cap;
    dynamic mode can use the full assumed site DC budget. This is NOT ABB firmware.
    """
    allocations = {s['session_id']: 0.0 for s in active}
    by_charger = defaultdict(list)
    for s in active:
        by_charger[s['charger_id']].append(s)
    if mode == 'static':
        charger_remaining = {cid: min(charger_caps[cid], STATIC_CAP_PER_CHARGER_DC_KW) for cid in range(CHARGER_COUNT)}
    else:
        charger_remaining = dict(charger_caps)
    remaining_site = site_dc_budget_kw
    ranked = sorted(active, key=lambda s: (s['departure_interval'] - s['current_interval'], s['departure_interval'], s['session_id']))
    # Pass 1: allocate minimum 100 kW to each feasible session, if hardware and site budget allow.
    for s in ranked:
        cid = s['charger_id']
        energy_left = max(0.0, s['energy_requested_kwh'] - s['energy_delivered_kwh'])
        desired = min(s['max_dc_kw'], energy_left * 60.0 / INTERVAL_MINUTES)
        if desired >= PORT_MIN_DC_KW and charger_remaining[cid] >= PORT_MIN_DC_KW and remaining_site >= PORT_MIN_DC_KW:
            allocations[s['session_id']] = PORT_MIN_DC_KW
            charger_remaining[cid] -= PORT_MIN_DC_KW
            remaining_site -= PORT_MIN_DC_KW
    # Pass 2: add 50 kW increments by urgency, while honoring charger and site limits.
    progress = True
    while progress and remaining_site >= POWER_STEP_KW:
        progress = False
        for s in ranked:
            sid, cid = s['session_id'], s['charger_id']
            current = allocations[sid]
            energy_left = max(0.0, s['energy_requested_kwh'] - s['energy_delivered_kwh'])
            max_useful = min(s['max_dc_kw'], energy_left * 60.0 / INTERVAL_MINUTES)
            if current <= 0 or current + POWER_STEP_KW > max_useful + 1e-9:
                continue
            if charger_remaining[cid] >= POWER_STEP_KW and remaining_site >= POWER_STEP_KW:
                allocations[sid] += POWER_STEP_KW
                charger_remaining[cid] -= POWER_STEP_KW
                remaining_site -= POWER_STEP_KW
                progress = True
    return allocations


def simulate():
    sessions = make_sessions()
    n_intervals = SIMULATION_DAY_HOURS * 60 // INTERVAL_MINUTES
    budget = site_power_budget(SITE_LIMIT_AC_KW, OTHER_LOAD_DEFAULT_KW, SAFETY_MARGIN_KW, CHARGER_EFFICIENCY)
    site_dc_budget = budget['estimated_dc_charging_budget_kw']
    charger_caps = {i: CHARGER_MAX_DC_KW for i in range(CHARGER_COUNT)}
    scenarios = ['unmanaged', 'static_cap', 'dynamic_site_cap']
    all_intervals, all_sessions, summary = [], [], []
    for scenario in scenarios:
        # independent copies for each scenario
        import copy
        sc_sessions = copy.deepcopy(sessions)
        for t in range(n_intervals):
            active = []
            for s in sc_sessions:
                if s['arrival_interval'] <= t < s['departure_interval'] and s['energy_delivered_kwh'] < s['energy_requested_kwh'] - 1e-9:
                    s['current_interval'] = t
                    active.append(s)
            if scenario == 'unmanaged':
                alloc = allocate_unmanaged(active, charger_caps)
            elif scenario == 'static_cap':
                alloc = allocate_capped(active, charger_caps, site_dc_budget, 'static')
            else:
                alloc = allocate_capped(active, charger_caps, site_dc_budget, 'dynamic')
            # Aggregate by charger and site
            charger_kw = defaultdict(float)
            delivered_this_interval = 0.0
            for s in active:
                kw = alloc.get(s['session_id'], 0.0)
                if kw > 0 and s['first_power_interval'] is None:
                    s['first_power_interval'] = t
                # kWh for this interval, capped by energy remaining
                kwh = min(s['energy_requested_kwh'] - s['energy_delivered_kwh'], kw * INTERVAL_MINUTES / 60.0)
                s['energy_delivered_kwh'] += kwh
                delivered_this_interval += kwh
                charger_kw[s['charger_id']] += kw
            dc_kw = sum(alloc.values())
            ac_charger_kw = dc_kw / CHARGER_EFFICIENCY
            # Use a fixed other-load value for the synthetic simulation; actual analysis should use meter data.
            other_kw = OTHER_LOAD_DEFAULT_KW
            site_kw = ac_charger_kw + other_kw
            # count active ports and sessions whose energy requirement has completed by departure
            all_intervals.append({
                'scenario': scenario,
                'timestamp_interval_start_hour': roundn(t * INTERVAL_MINUTES / 60.0, 2),
                'active_sessions': len(active),
                'charger_dc_kw': roundn(dc_kw),
                'charger_ac_input_kw_est': roundn(ac_charger_kw),
                'other_site_load_kw_assumed': other_kw,
                'total_site_ac_kw_est': roundn(site_kw),
                'site_limit_ac_kw_assumed': SITE_LIMIT_AC_KW,
                'operating_ceiling_ac_kw_assumed': SITE_LIMIT_AC_KW - SAFETY_MARGIN_KW,
                'interval_energy_delivered_kwh': roundn(delivered_this_interval),
                'site_limit_exceeded': site_kw > SITE_LIMIT_AC_KW - SAFETY_MARGIN_KW + 1e-9,
            })
        for s in sc_sessions:
            s['energy_unserved_kwh'] = max(0.0, s['energy_requested_kwh'] - s['energy_delivered_kwh'])
            s['completed_by_departure'] = s['energy_unserved_kwh'] < 1e-6
            s['scenario'] = scenario
            s['arrival_hour'] = roundn(s['arrival_interval'] * INTERVAL_MINUTES / 60.0, 2)
            s['departure_hour'] = roundn(s['departure_interval'] * INTERVAL_MINUTES / 60.0, 2)
            s['first_power_hour'] = '' if s['first_power_interval'] is None else roundn(s['first_power_interval'] * INTERVAL_MINUTES / 60.0, 2)
            s.pop('current_interval', None)
            all_sessions.append({k: s[k] for k in ['scenario','session_id','arrival_hour','departure_hour','energy_requested_kwh','energy_delivered_kwh','energy_unserved_kwh','max_dc_kw','charger_id','completed_by_departure','first_power_hour']})
        intervals = [r for r in all_intervals if r['scenario'] == scenario]
        site_peaks = [r['total_site_ac_kw_est'] for r in intervals]
        charging_peaks = [r['charger_dc_kw'] for r in intervals]
        delivered = sum(r['interval_energy_delivered_kwh'] for r in intervals)
        energy_requested = sum(s['energy_requested_kwh'] for s in sc_sessions)
        completed = sum(1 for s in sc_sessions if s['completed_by_departure'])
        active_session_energy_kwh = delivered
        summary.append({
            'scenario': scenario,
            'sessions': len(sc_sessions),
            'total_requested_charging_kwh': roundn(energy_requested),
            'total_delivered_charging_kwh': roundn(delivered),
            'total_unserved_charging_kwh': roundn(max(0.0, energy_requested-delivered)),
            'sessions_fully_served_by_departure': completed,
            'session_completion_rate_pct': roundn(100.0*completed/len(sc_sessions) if sc_sessions else 0),
            'peak_charger_dc_kw': roundn(max(charging_peaks, default=0)),
            'peak_total_site_ac_kw_est': roundn(max(site_peaks, default=0)),
            'intervals_over_operating_ceiling': sum(1 for r in intervals if r['site_limit_exceeded']),
            'site_operating_ceiling_ac_kw_assumed': SITE_LIMIT_AC_KW-SAFETY_MARGIN_KW,
            'status': 'SYNTHETIC SIMULATION ONLY, not field results or ABB controller behavior',
        })
    return budget, all_intervals, all_sessions, summary


def analyze_real_interval_data(path):
    """Summarize user-supplied interval data if a populated CSV exists.

    Expected columns: timestamp_local, mode (baseline/managed), site_meter_kw,
    site_energy_kwh_interval. Optional columns: charger_dc_kw_total, active_sessions,
    delivered_charging_kwh_interval, completed_sessions, failed_sessions.
    """
    p = Path(path)
    if not p.exists():
        return [{'status':'NOT RUN: no real interval-data CSV supplied', 'file_expected':str(p)}]
    with open(p, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    valid = []
    for r in rows:
        mode = (r.get('mode') or '').strip().lower()
        try:
            kw = float(r['site_meter_kw'])
        except (ValueError, KeyError, TypeError):
            continue
        if mode not in ('baseline','managed'):
            continue
        energy = None
        try: energy = float(r.get('site_energy_kwh_interval',''))
        except (ValueError, TypeError): pass
        charger_energy = None
        try: charger_energy = float(r.get('delivered_charging_kwh_interval',''))
        except (ValueError, TypeError): pass
        bill = None
        try: bill = float(r.get('actual_total_bill_cad',''))
        except (ValueError, TypeError): pass
        valid.append({'mode':mode,'kw':kw,'energy':energy,'charger_energy':charger_energy,'bill':bill,'timestamp':r.get('timestamp_local','')})
    out=[]
    for mode in ('baseline','managed'):
        subset=[r for r in valid if r['mode']==mode]
        if not subset:
            out.append({'mode':mode,'interval_count':0,'status':'MISSING DATA'})
            continue
        energies=[r['energy'] for r in subset if r['energy'] is not None]
        charger_energies=[r['charger_energy'] for r in subset if r['charger_energy'] is not None]
        bills=[r['bill'] for r in subset if r['bill'] is not None]
        stamps=[r['timestamp'] for r in subset if r['timestamp']]
        out.append({
            'mode':mode,
            'interval_count':len(subset),
            'peak_site_meter_kw':roundn(max(r['kw'] for r in subset)),
            'mean_site_meter_kw':roundn(sum(r['kw'] for r in subset)/len(subset)),
            'recorded_site_energy_kwh_sum':roundn(sum(energies)) if energies else '',
            'recorded_charging_energy_kwh_sum':roundn(sum(charger_energies)) if charger_energies else '',
            'actual_total_bill_cad':roundn(sum(bills),2) if bills else '',
            'first_timestamp':min(stamps) if stamps else '',
            'last_timestamp':max(stamps) if stamps else '',
            'estimated_charging_emissions_kg_co2e':roundn(sum(charger_energies)*QUEBEC_GRID_G_CO2E_PER_KWH/1000.0) if charger_energies else '',
            'status':'OBSERVED INPUT, check that comparison periods and weather/traffic are comparable',
        })
    baseline=next((r for r in out if r.get('mode')=='baseline' and r.get('interval_count',0)>0),None)
    managed=next((r for r in out if r.get('mode')=='managed' and r.get('interval_count',0)>0),None)
    if baseline and managed:
        row={
            'comparison':'managed_minus_baseline',
            'peak_reduction_kw':roundn(baseline['peak_site_meter_kw']-managed['peak_site_meter_kw']),
            'peak_reduction_pct':roundn(100*(baseline['peak_site_meter_kw']-managed['peak_site_meter_kw'])/baseline['peak_site_meter_kw'] if baseline['peak_site_meter_kw'] else 0),
            'caution':'Descriptive comparison only unless periods are matched and confounders are controlled.'
        }
        if baseline.get('recorded_site_energy_kwh_sum') != '' and managed.get('recorded_site_energy_kwh_sum') != '':
            row['site_energy_change_kwh_managed_minus_baseline']=roundn(managed['recorded_site_energy_kwh_sum']-baseline['recorded_site_energy_kwh_sum'])
        if baseline.get('recorded_charging_energy_kwh_sum') != '' and managed.get('recorded_charging_energy_kwh_sum') != '':
            row['charging_energy_change_kwh_managed_minus_baseline']=roundn(managed['recorded_charging_energy_kwh_sum']-baseline['recorded_charging_energy_kwh_sum'])
            row['charging_emissions_change_kg_co2e_managed_minus_baseline']=roundn((managed['recorded_charging_energy_kwh_sum']-baseline['recorded_charging_energy_kwh_sum'])*QUEBEC_GRID_G_CO2E_PER_KWH/1000.0)
        if baseline.get('actual_total_bill_cad') != '' and managed.get('actual_total_bill_cad') != '':
            row['actual_bill_savings_cad_baseline_minus_managed']=roundn(baseline['actual_total_bill_cad']-managed['actual_total_bill_cad'],2)
        out.append(row)
    return out


def build_cost_emissions(summary):
    rows=[]
    for s in summary:
        # This is a scenario screening estimate, not a tariff bill. Energy cost applies only to
        # simulated charging energy delivered. Do not interpret demand charge without a real tariff.
        kwh=s['total_delivered_charging_kwh']
        peak_reduction_kw=0.0
        annual_cost=kwh*ELECTRICITY_PRICE_CAD_PER_KWH
        emissions_kg=kwh*QUEBEC_GRID_G_CO2E_PER_KWH/1000.0
        rows.append({
            'scenario':s['scenario'],
            'simulated_charging_energy_delivered_kwh_day':kwh,
            'annualized_energy_cost_cad_placeholder':roundn(annual_cost*365),
            'electricity_price_cad_per_kwh_placeholder':ELECTRICITY_PRICE_CAD_PER_KWH,
            'demand_charge_cad_per_kw_month_input':DEMAND_CHARGE_CAD_PER_KW_MONTH,
            'annualized_grid_emissions_kg_co2e_placeholder':roundn(emissions_kg*365),
            'emissions_factor_g_co2e_per_kwh':QUEBEC_GRID_G_CO2E_PER_KWH,
            'emissions_factor_year':EMISSIONS_FACTOR_YEAR,
            'emissions_factor_method':EMISSIONS_FACTOR_METHOD,
            'interpretation':'Synthetic one-day workload annualized by x365. Not a forecast. Same kWh means same location-based emissions under a fixed annual factor.'
        })
    return rows


def create_templates():
    interval_rows=[{
        'timestamp_local':'2026-01-01 00:00:00',
        'mode':'baseline_or_managed',
        'site_meter_kw':'',
        'site_energy_kwh_interval':'',
        'charger_dc_kw_total':'',
        'delivered_charging_kwh_interval':'',
        'actual_total_bill_cad':'',
        'active_sessions':'',
        'completed_sessions':'',
        'failed_sessions':'',
        'ambient_temperature_c':'',
        'notes_fault_or_offline_event':'',
    }]
    write_csv(OUT/'grid_ece_interval_data_template.csv', interval_rows)
    session_rows=[{
        'session_id_anonymized':'S001',
        'mode':'baseline_or_managed',
        'arrival_timestamp_local':'',
        'departure_timestamp_local':'',
        'energy_requested_kwh':'',
        'energy_delivered_kwh':'',
        'vehicle_max_dc_kw_if_known':'',
        'charge_complete_by_departure':'',
        'wait_minutes':'',
        'abandoned_or_failed':'',
        'notes':'Do not include names, license plates, or other direct identifiers.'
    }]
    write_csv(OUT/'grid_ece_session_data_template.csv', session_rows)
    assumptions=[
        {'input':'Site connection limit','value':SITE_LIMIT_AC_KW,'unit':'kW AC','classification':'ASSUMPTION','source_or_evidence_needed':'Utility-approved connection / site electrical drawings'},
        {'input':'Other site load','value':OTHER_LOAD_DEFAULT_KW,'unit':'kW AC','classification':'ASSUMPTION','source_or_evidence_needed':'Interval meter data excluding charger load, or submeter'},
        {'input':'Safety margin','value':SAFETY_MARGIN_KW,'unit':'kW','classification':'ASSUMPTION','source_or_evidence_needed':'Electrical engineer / utility approved operating limit'},
        {'input':'A400 charger max','value':CHARGER_MAX_DC_KW,'unit':'kW DC per charger','classification':'SOURCE-BASED, verify exact configuration','source_or_evidence_needed':'Current ABB North American datasheet and purchase configuration'},
        {'input':'A400 minimum output','value':PORT_MIN_DC_KW,'unit':'kW per active port/session','classification':'SOURCE-BASED, verify exact configuration','source_or_evidence_needed':'Current ABB datasheet / controller documentation'},
        {'input':'Power allocation increment','value':POWER_STEP_KW,'unit':'kW','classification':'SOURCE-BASED, verify exact configuration','source_or_evidence_needed':'ABB datasheet and controller documentation'},
        {'input':'Charger efficiency','value':CHARGER_EFFICIENCY,'unit':'fraction','classification':'SOURCE-BASED, verify exact datasheet meaning','source_or_evidence_needed':'ABB datasheet, confirm whether full-load/total-system value'},
        {'input':'Electricity price','value':ELECTRICITY_PRICE_CAD_PER_KWH,'unit':'CAD/kWh','classification':'PLACEHOLDER','source_or_evidence_needed':'Actual Hydro-Québec bill and applicable Rate BR terms'},
        {'input':'Demand charge','value':DEMAND_CHARGE_CAD_PER_KW_MONTH,'unit':'CAD/kW-month','classification':'UNCONFIRMED, intentionally zero','source_or_evidence_needed':'Applicable tariff and actual bill; do not assume Rate BR has this charge'},
        {'input':'Québec emissions factor','value':QUEBEC_GRID_G_CO2E_PER_KWH,'unit':'g CO2e/kWh','classification':'SOURCE-BASED annual factor, year specified','source_or_evidence_needed':'Government of Canada factor table; use hourly marginal factor for causal time-shifting claims'},
        {'input':'Session energy','value':SESSION_ENERGY_KWH,'unit':'kWh/session','classification':'SYNTHETIC WORKLOAD ASSUMPTION','source_or_evidence_needed':'Actual session records'},
        {'input':'Session max power','value':SESSION_MAX_DC_KW,'unit':'kW DC/session','classification':'SYNTHETIC WORKLOAD ASSUMPTION','source_or_evidence_needed':'Vehicle acceptance / charger session records'},
    ]
    write_csv(OUT/'grid_ece_assumptions_register.csv', assumptions)
    checklist=[
        {'item':'Identify actual Québec site and operator','status':'OPEN','evidence_needed':'Site address, operator, utility account/service ID (keep sensitive IDs private)'},
        {'item':'Verify service/transformer/switchgear capacity','status':'OPEN','evidence_needed':'Approved drawings, nameplate ratings, utility accepted load limit'},
        {'item':'Verify ABB site-wide controller behavior','status':'OPEN','evidence_needed':'Written vendor/integrator confirmation, architecture, tests with four chargers'},
        {'item':'Verify safe offline/failure behavior','status':'OPEN','evidence_needed':'Documented local fallback limits and test results for cloud, meter, controller failure'},
        {'item':'Confirm Rate BR eligibility and bill formula','status':'OPEN','evidence_needed':'Actual bill and Hydro-Québec tariff confirmation for this public charging site'},
        {'item':'Collect matched baseline/managed interval data','status':'OPEN','evidence_needed':'Prefer 15-minute meter data plus charger/session data across comparable days'},
        {'item':'Compare service quality','status':'OPEN','evidence_needed':'Completed by departure, delivered/requested kWh, wait, failed/abandoned sessions'},
        {'item':'Validate emissions method','status':'OPEN','evidence_needed':'Factor year, average vs marginal method, same-energy baseline comparison'},
        {'item':'Test rebound peak','status':'OPEN','evidence_needed':'Meter/control logs during recovery and resumption after peak period'},
        {'item':'Validate model against pilot data','status':'OPEN','evidence_needed':'Compare predicted interval kW/kWh with held-out observed data'},
    ]
    write_csv(OUT/'grid_ece_validation_checklist.csv', checklist)


def create_charts(intervals, summary, budget):
    if plt is None:
        return
    # Plot site load across the day for the three scenarios, and the operating ceiling.
    fig, ax = plt.subplots(figsize=(11,5.5))
    for scenario in ['unmanaged','static_cap','dynamic_site_cap']:
        rows=[r for r in intervals if r['scenario']==scenario]
        x=[r['timestamp_interval_start_hour'] for r in rows]
        y=[r['total_site_ac_kw_est'] for r in rows]
        ax.step(x,y,where='post',label=scenario.replace('_',' ').title(),linewidth=1.8)
    ax.axhline(SITE_LIMIT_AC_KW-SAFETY_MARGIN_KW,linestyle='--',label='Assumed operating ceiling')
    ax.set(xlabel='Hour of day',ylabel='Estimated site demand (kW AC)',title='Synthetic 24-hour site demand by charging strategy')
    ax.grid(alpha=.25); ax.legend(); fig.tight_layout()
    fig.savefig(OUT/'grid_ece_v3_peak_vs_energy.png',dpi=180); plt.close(fig)

    fig, axes=plt.subplots(1,2,figsize=(10,4.8))
    labels=[r['scenario'].replace('_',' ').title() for r in summary]
    axes[0].bar(labels,[r['peak_total_site_ac_kw_est'] for r in summary])
    axes[0].axhline(SITE_LIMIT_AC_KW-SAFETY_MARGIN_KW,linestyle='--')
    axes[0].set_title('Estimated peak site demand'); axes[0].set_ylabel('kW AC')
    axes[1].bar(labels,[r['session_completion_rate_pct'] for r in summary])
    axes[1].set_title('Sessions fully served by departure'); axes[1].set_ylabel('Percent')
    for ax in axes: ax.tick_params(axis='x',rotation=18); ax.grid(axis='y',alpha=.25)
    fig.suptitle('Synthetic scenario trade-offs, not field results')
    fig.tight_layout(); fig.savefig(OUT/'grid_ece_v3_peak_service_tradeoff.png',dpi=180); plt.close(fig)


def main():
    budget, intervals, sessions, summary = simulate()
    write_csv(OUT/'grid_ece_v3_power_budget.csv',[budget])
    write_csv(OUT/'grid_ece_v3_interval_results.csv',intervals)
    write_csv(OUT/'grid_ece_v3_session_results.csv',sessions)
    write_csv(OUT/'grid_ece_v3_scenario_summary.csv',summary)
    write_csv(OUT/'grid_ece_v3_cost_emissions.csv',build_cost_emissions(summary))
    # Real data analyzer reads a user-filled copy named exactly this; blank template is separate.
    real_results=analyze_real_interval_data(OUT/'grid_ece_real_interval_data.csv')
    write_csv(OUT/'grid_ece_v3_real_data_analysis.csv',real_results)
    create_templates()
    create_charts(intervals,summary,budget)
    print('GRID ECE MODEL V3')
    print('IMPORTANT: synthetic workload only. No actual Québec site has been identified in the team notes.')
    print('Assumed DC charging budget: {:.1f} kW'.format(budget['estimated_dc_charging_budget_kw']))
    print('Site operating ceiling: {:.1f} kW AC'.format(SITE_LIMIT_AC_KW-SAFETY_MARGIN_KW))
    for r in summary:
        print('{}: peak site {:.1f} kW AC, delivered {:.1f} kWh, completion {:.1f}%, over-ceiling intervals {}'.format(
            r['scenario'],r['peak_total_site_ac_kw_est'],r['total_delivered_charging_kwh'],r['session_completion_rate_pct'],r['intervals_over_operating_ceiling']))
    print('Created model outputs, real-data templates, assumptions register, and validation checklist in: {}'.format(OUT))
    print('To analyze real data, save populated 15-minute data as grid_ece_real_interval_data.csv and rerun.')

if __name__ == '__main__':
    main()
