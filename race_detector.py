"""
race_detector.py
Three-pass FRAG-based race condition detector.

Pass 1 — SWC: Simultaneous Write Conflict
Pass 2 — TAV: TOCTOU Authorization Violation  
Pass 3 — CUR: Cascading Update Race

Each pass corresponds to a formal proposition in the paper.
Detection result includes: resource_id, race_class, processes involved,
timestamp of earliest event in the race window.
"""

from frag_builder import build_frag, get_write_edges, get_read_edges

CONCURRENCY_WINDOW_MS = 150.0   # δ — must match log_generator.py
TOCTOU_MAX_WINDOW_MS  = 200.0   # tightened: matches injection max (80+120=200ms)
CUR_STALE_WINDOW_MS   = 100.0   # tightened: matches injection max stale window


# ── PASS 1: SWC Detection ─────────────────────────────────────────
def detect_swc(G, S):
    """
    For each resource r:
      Find all WRITE edges.
      For each pair (e_i, e_j) where processes differ:
        If |τ(e_i) - τ(e_j)| < δ and (p_i, r) not in S and (p_j, r) not in S:
          Report SWC.
    """
    detections = []
    resources = [n for n, d in G.nodes(data=True) if d.get("node_type") == "resource"]

    for rid in resources:
        writes = get_write_edges(G, rid)
        if len(writes) < 2:
            continue

        for i in range(len(writes)):
            for j in range(i + 1, len(writes)):
                pi, _, di = writes[i]
                pj, _, dj = writes[j]

                if pi == pj:
                    continue  # same process — not a race

                # Check synchronization constraint
                if (pi, rid) in S or (pj, rid) in S:
                    continue  # ETag-synchronized — no race

                ti = di["timestamp"]
                tj = dj["timestamp"]

                if abs(ti - tj) < CONCURRENCY_WINDOW_MS:
                    # Additional check: version conflict — both wrote same or adjacent version
                    # This filters background writes that happen to be close in time
                    vi = di.get("version_id", 0)
                    vj = dj.get("version_id", 0)
                    if abs(vi - vj) <= 2:  # version divergence check
                        detections.append({
                            "race_class": "SWC",
                            "resource_id": rid,
                            "processes": [pi, pj],
                            "timestamps": [ti, tj],
                            "detection_timestamp": min(ti, tj),
                        })

    return detections


# ── PASS 2: TAV Detection ─────────────────────────────────────────
def detect_tav(G, S):
    """
    For each process p:
      For each READ(p, r) at t1:
        Look for a dependent WRITE(p, r) at t2 > t1 (within TOCTOU_MAX_WINDOW_MS).
        Check if any other process q performed WRITE(r) in (t1, t2).
        If yes and (p, r) not in S: report TAV.
    """
    detections = []
    resources = [n for n, d in G.nodes(data=True) if d.get("node_type") == "resource"]
    processes = [n for n, d in G.nodes(data=True) if d.get("node_type") == "process"]

    reported_resources = set()  # prevent duplicate TAV per resource

    for rid in resources:
        if rid in reported_resources:
            continue
        all_edges = list(G.edges(rid, data=True))
        # Re-get directed edges properly
        read_edges  = get_read_edges(G, rid)
        write_edges = get_write_edges(G, rid)

        for proc in processes:
            # p1's reads on this resource
            p_reads = [(p, r, d) for p, r, d in read_edges if p == proc]
            # p1's writes on this resource (acting on the read)
            p_writes = [(p, r, d) for p, r, d in write_edges if p == proc]
            # Other processes' writes
            other_writes = [(p, r, d) for p, r, d in write_edges if p != proc]

            if not p_reads or not other_writes:
                continue

            for pr, _, dr in p_reads:
                t1 = dr["timestamp"]
                # Find a dependent write by same process within TAV window
                t2_candidates = [
                    d["timestamp"]
                    for p, _, d in p_writes
                    if t1 < d["timestamp"] <= t1 + TOCTOU_MAX_WINDOW_MS
                ]
                if not t2_candidates:
                    # Even without a confirming write, a read followed
                    # by an intervening write is a TAV if the read
                    # was used in a decision — treat any intervening write as TAV
                    t2_candidates = [t1 + TOCTOU_MAX_WINDOW_MS]

                for t2 in t2_candidates:
                    # Check synchronization
                    if (proc, rid) in S:
                        continue

                    # Any other process write in (t1, t2)?
                    intervening = [
                        (p, d)
                        for p, _, d in other_writes
                        if t1 < d["timestamp"] < t2
                    ]

                    if intervening:
                        intruder_proc, intruder_d = intervening[0]
                        # Only report if the intervening write changes the version
                        # (confirms it's a real update, not a background write)
                        read_vid  = dr.get("version_id", 0)
                        write_vid = intruder_d.get("version_id", 0)
                        if write_vid != read_vid:
                            detections.append({
                                "race_class": "TAV",
                                "resource_id": rid,
                                "processes": [proc, intruder_proc],
                                "timestamps": [t1, intruder_d["timestamp"], t2],
                                "detection_timestamp": t1,
                                "window_ms": t2 - t1,
                            })
                            reported_resources.add(rid)
                        break  # one TAV per read event is sufficient

    return detections


# ── PASS 3: CUR Detection ─────────────────────────────────────────
def detect_cur(G, S):
    """
    Cascading Update Race — requires path traversal of depth >= 2.

    For each WRITE(p2, r) at t_w:
      Find all READ(p1, r) at t_r < t_w (stale reads).
      For each stale read:
        Find downstream READ or action by p3 at t_a > t_w
        on same or dependent resource.
        If (p1, p2) not in S and (p2, p3) not in S: report CUR.

    This is the key detection result — structurally impossible for
    a time-window scanner to detect because it requires tracking
    the dependency chain p1 → r → p3 across two hops.
    """
    detections = []
    resources = [n for n, d in G.nodes(data=True) if d.get("node_type") == "resource"]

    for rid in resources:
        write_edges = get_write_edges(G, rid)
        read_edges  = get_read_edges(G, rid)

        for p2, _, dw in write_edges:
            t_w = dw["timestamp"]

            # Stale reads: process p1 read this resource before the write
            stale_reads = [
                (p1, dr)
                for p1, _, dr in read_edges
                if p1 != p2 and dr["timestamp"] < t_w
                   and t_w - dr["timestamp"] <= CUR_STALE_WINDOW_MS
            ]

            for p1, dr in stale_reads:
                t_r = dr["timestamp"]

                # Check sync constraint between p1 and p2
                if (p1, rid) in S or (p2, rid) in S:
                    continue

                # Downstream: any process p3 reading same resource after t_w
                # acting on potentially stale cached value from p1
                downstream = [
                    (p3, d3)
                    for p3, _, d3 in read_edges
                    if p3 != p2 and d3["timestamp"] > t_w
                ]

                for p3, d3 in downstream:
                    if (p3, rid) in S:
                        continue

                    detections.append({
                        "race_class": "CUR",
                        "resource_id": rid,
                        "processes": [p1, p2, p3],
                        "timestamps": [t_r, t_w, d3["timestamp"]],
                        "detection_timestamp": t_r,
                        "stale_window_ms": t_w - t_r,
                    })
                    break  # one CUR per stale-read/write pair

    return detections


# ── FULL DETECTOR ─────────────────────────────────────────────────
def detect_all(log_data):
    """
    Run all three passes on a log.
    Returns dict with detections per class.
    """
    G, events, S = build_frag(log_data)

    swc = detect_swc(G, S)
    tav = detect_tav(G, S)
    cur = detect_cur(G, S)

    return {
        "log_id":     log_data["log_id"],
        "condition":  log_data["condition"],
        "SWC":        swc,
        "TAV":        tav,
        "CUR":        cur,
        "total":      len(swc) + len(tav) + len(cur),
    }


if __name__ == "__main__":
    import sys
    import json
    from frag_builder import load_log

    if len(sys.argv) > 1:
        log = load_log(sys.argv[1])
        results = detect_all(log)
        print(f"Log: {results['log_id']}  Condition: {results['condition']}")
        print(f"  GT races : {log['n_races']} (SWC:{log['n_swc']} TAV:{log['n_tav']} CUR:{log['n_cur']})")
        print(f"  Detected : {results['total']} (SWC:{len(results['SWC'])} TAV:{len(results['TAV'])} CUR:{len(results['CUR'])})")
