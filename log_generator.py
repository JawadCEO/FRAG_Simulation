"""
log_generator.py
Generates synthetic FHIR R4 transaction logs for FRAG simulation.

Each log = one simulated session of concurrent FHIR access.
Output = JSON file with access events + ground-truth race labels.

Three conditions:
  C1 - Sequential (no concurrency, no races) → baseline false-positive test
  C2 - Concurrent, unsynchronized → races injected
  C3 - Concurrent, partial ETag sync (70% writes use ETag) → residual races only
"""

import json
import random
import os
import argparse
import numpy as np
from pathlib import Path

# ── FHIR resource type distribution ───────────────────────────────
# Based on FHIR R4 standard clinical workflows.
# AllergyIntolerance and MedicationRequest accessed 2x more frequently
# because they sit at the intersection of pharmacy, lab, and EHR workflows.
RESOURCE_TYPES = [
    "AllergyIntolerance",
    "AllergyIntolerance",      # 2x weight
    "MedicationRequest",
    "MedicationRequest",       # 2x weight
    "Observation",
    "Patient",
]

# Processes that access FHIR resources concurrently
PROCESSES = ["EHR", "PHARMACY", "LAB"]

# READ:WRITE ratio = 3:1 (standard clinical read-heavy workload)
OPERATIONS = ["READ", "READ", "READ", "WRITE"]

# Concurrency window: two events within this window are "concurrent"
CONCURRENCY_WINDOW_MS = 150.0

# ── RESOURCE ID POOL ──────────────────────────────────────────────
def build_resource_pool(n_patients=50):
    """
    Build a pool of resource IDs per type.
    Each patient has one resource per type — realistic FHIR structure.
    """
    pool = {rt: [] for rt in set(RESOURCE_TYPES)}
    for pid in range(n_patients):
        pool["AllergyIntolerance"].append(f"AllergyIntolerance/patient-{pid:03d}")
        pool["MedicationRequest"].append(f"MedicationRequest/patient-{pid:03d}")
        pool["Observation"].append(f"Observation/patient-{pid:03d}")
        pool["Patient"].append(f"Patient/patient-{pid:03d}")
    return pool


# ── BASE EVENT GENERATOR ──────────────────────────────────────────
def make_event(event_id, process, resource_type, resource_id,
               operation, timestamp_ms, version_id,
               etag_used=False, is_race=False, race_class=None):
    return {
        "event_id":      event_id,
        "process":       process,
        "resource_type": resource_type,
        "resource_id":   resource_id,
        "operation":     operation,
        "timestamp_ms":  round(timestamp_ms, 3),
        "version_id":    version_id,
        "etag_used":     etag_used,
        "is_race":       is_race,
        "race_class":    race_class,
    }


# ── NON-RACE EVENT GENERATOR ──────────────────────────────────────
def generate_background_events(n_events, pool, condition, rng, version_tracker):
    """
    Generate n_events non-race background events.
    Timestamps are spread across a 10-second window (10,000 ms).
    """
    events = []
    timestamps = sorted(rng.uniform(0, 10000, n_events).tolist())

    for i, ts in enumerate(timestamps):
        rt = rng.choice(RESOURCE_TYPES)
        rid = rng.choice(pool[rt])
        proc = rng.choice(PROCESSES)
        op = rng.choice(OPERATIONS)

        # C1: force sequential — space events 200ms apart minimum
        if condition == "C1":
            ts = i * 250.0
            op = "READ"  # C1 has no writes = no races possible

        vid = version_tracker.get(rid, 1)
        if op == "WRITE":
            version_tracker[rid] = vid + 1

        # C3: 70% of writes use ETag
        etag = False
        if condition == "C3" and op == "WRITE":
            etag = rng.random() < 0.70

        events.append(make_event(
            event_id=i,
            process=proc,
            resource_type=rt,
            resource_id=rid,
            operation=op,
            timestamp_ms=ts,
            version_id=vid,
            etag_used=etag
        ))
    return events, version_tracker


# ── RACE INJECTORS ────────────────────────────────────────────────

def inject_swc(base_id, pool, rng, version_tracker, condition):
    """
    Simultaneous Write Conflict:
    Two different processes WRITE same resource within CONCURRENCY_WINDOW_MS.
    """
    rt = rng.choice(["AllergyIntolerance", "MedicationRequest"])
    rid = rng.choice(pool[rt])
    p1, p2 = rng.choice(PROCESSES, size=2, replace=False)

    t_base = rng.uniform(500, 9500)
    delta = rng.uniform(1, CONCURRENCY_WINDOW_MS - 1)

    vid = version_tracker.get(rid, 1)
    version_tracker[rid] = vid + 2

    # C3: neither writer uses ETag → race survives
    etag = False

    e1 = make_event(base_id,   p1, rt, rid, "WRITE", t_base,         vid,   etag, True, "SWC")
    e2 = make_event(base_id+1, p2, rt, rid, "WRITE", t_base + delta, vid+1, etag, True, "SWC")
    return [e1, e2], version_tracker


def inject_tav(base_id, pool, rng, version_tracker, condition):
    """
    TOCTOU Authorization Violation:
    Process p1 READs auth resource at t1.
    Process p2 WRITEs auth resource at t1 < t < t2.
    Process p1 acts (dependent WRITE) at t2 based on stale read.
    """
    rt = "AllergyIntolerance"   # auth-adjacent resource
    rid = rng.choice(pool[rt])
    p1, p2 = rng.choice(PROCESSES, size=2, replace=False)

    t1 = rng.uniform(500, 8000)
    t_write = t1 + rng.uniform(10, 80)    # intervening write
    t2 = t_write + rng.uniform(10, 120)   # p1 acts after write

    vid = version_tracker.get(rid, 1)
    version_tracker[rid] = vid + 1

    e1 = make_event(base_id,   p1, rt, rid, "READ",  t1,      vid,   False, True, "TAV")
    e2 = make_event(base_id+1, p2, rt, rid, "WRITE", t_write, vid+1, False, True, "TAV")
    e3 = make_event(base_id+2, p1, rt, rid, "WRITE", t2,      vid,   False, True, "TAV")
    return [e1, e2, e3], version_tracker


def inject_cur(base_id, pool, rng, version_tracker, condition):
    """
    Cascading Update Race:
    p2 WRITEs r1 at t_w.
    p1 had READ r1 at t_r < t_w (stale read).
    p3 acts on p1's stale value at t_a > t_w.
    Requires path traversal of depth >= 2 to detect.
    """
    rt = rng.choice(["AllergyIntolerance", "Observation"])
    rid = rng.choice(pool[rt])
    p1, p2, p3 = rng.choice(PROCESSES, size=3, replace=False) \
        if len(PROCESSES) >= 3 else (PROCESSES[0], PROCESSES[1], PROCESSES[0])

    t_w = rng.uniform(2000, 8000)
    t_r = t_w - rng.uniform(10, 100)     # stale read before the write
    t_a = t_w + rng.uniform(5, 200)      # downstream action after write

    vid = version_tracker.get(rid, 1)
    version_tracker[rid] = vid + 1

    e1 = make_event(base_id,   p1, rt, rid, "READ",  t_r, vid,   False, True, "CUR")
    e2 = make_event(base_id+1, p2, rt, rid, "WRITE", t_w, vid+1, False, True, "CUR")
    e3 = make_event(base_id+2, p3, rt, rid, "READ",  t_a, vid+1, False, True, "CUR")
    return [e1, e2, e3], version_tracker


# ── MAIN LOG GENERATOR ────────────────────────────────────────────

def generate_log(log_id, condition, rng, pool):
    """
    Generate one complete transaction log.
    Returns list of events with ground-truth labels.
    """
    n_events = int(rng.uniform(200, 800))
    n_races  = 0 if condition == "C1" else int(rng.uniform(8, 40))

    # Race class distribution: 40% SWC, 30% TAV, 30% CUR
    if n_races > 0:
        n_swc = max(1, int(n_races * 0.40))
        n_tav = max(1, int(n_races * 0.30))
        n_cur = n_races - n_swc - n_tav
    else:
        n_swc = n_tav = n_cur = 0

    version_tracker = {}
    all_events = []
    eid = 0

    # Background events
    bg_events, version_tracker = generate_background_events(
        n_events, pool, condition, rng, version_tracker
    )
    all_events.extend(bg_events)
    eid = len(bg_events)

    # Inject races (C2 and C3 only)
    if condition != "C1":
        for _ in range(n_swc):
            evs, version_tracker = inject_swc(eid, pool, rng, version_tracker, condition)
            all_events.extend(evs)
            eid += len(evs)

        for _ in range(n_tav):
            evs, version_tracker = inject_tav(eid, pool, rng, version_tracker, condition)
            all_events.extend(evs)
            eid += len(evs)

        for _ in range(n_cur):
            evs, version_tracker = inject_cur(eid, pool, rng, version_tracker, condition)
            all_events.extend(evs)
            eid += len(evs)

    # Sort by timestamp — realistic log ordering
    all_events.sort(key=lambda e: e["timestamp_ms"])

    # Re-assign sequential event IDs after sort
    for i, ev in enumerate(all_events):
        ev["event_id"] = i

    return {
        "log_id":    log_id,
        "condition": condition,
        "n_events":  len(all_events),
        "n_races":   n_swc + n_tav + n_cur,
        "n_swc":     n_swc,
        "n_tav":     n_tav,
        "n_cur":     n_cur,
        "events":    all_events,
    }


# ── CLI ENTRY POINT ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FRAG synthetic log generator")
    parser.add_argument("--n_logs",     type=int, default=500,
                        help="Logs to generate per condition (default 500)")
    parser.add_argument("--n_patients", type=int, default=50,
                        help="Patient pool size (default 50)")
    parser.add_argument("--seed",       type=int, default=42,
                        help="Random seed for reproducibility")
    parser.add_argument("--outdir",     type=str, default="logs",
                        help="Output directory")
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    pool = build_resource_pool(args.n_patients)

    for condition in ["C1", "C2", "C3"]:
        cdir = Path(args.outdir) / condition
        cdir.mkdir(parents=True, exist_ok=True)
        print(f"Generating {args.n_logs} logs for condition {condition}...")

        for i in range(args.n_logs):
            log = generate_log(
                log_id=f"{condition}_{i:04d}",
                condition=condition,
                rng=rng,
                pool=pool
            )
            fpath = cdir / f"log_{i:04d}.json"
            with open(fpath, "w") as f:
                json.dump(log, f)

        print(f"  → {args.n_logs} logs written to {cdir}/")

    print("Done.")


if __name__ == "__main__":
    main()
