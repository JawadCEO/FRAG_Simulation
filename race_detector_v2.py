"""
race_detector.py  —  v2
Three-pass FRAG-based race condition detector.

Fix (v2): Cross-class contamination eliminated.
  TAV injection emits READ→WRITE(intruder)→WRITE(victim) which looks like
  SWC to Pass 1. CUR injection also shares resources with SWC/TAV patterns.
  Solution: passes run in priority order (TAV → SWC → CUR) and each pass
  claims the resources it fires on. Later passes skip claimed resources.
  This enforces mutual exclusivity of class assignment.

Pass order (by structural specificity, most specific first):
  1. TAV  — requires READ + intervening WRITE + dependent action (3-event pattern)
  2. SWC  — requires concurrent WRITE pair (2-event pattern)
  3. CUR  — requires stale READ + downstream action (path-traversal pattern)
"""

from frag_builder import build_frag, get_write_edges, get_read_edges

CONCURRENCY_WINDOW_MS = 150.0   # δ for SWC
TOCTOU_MAX_WINDOW_MS  = 200.0   # max READ→act window for TAV
CUR_STALE_WINDOW_MS   = 100.0   # max stale-read window for CUR


# ── PASS 1: TAV Detection (highest priority — most specific pattern) ──────────
def detect_tav(G, S, claimed=None):
    """
    TAV: process p reads authorization resource r at t1,
    another process writes r between t1 and t2,
    p then acts (writes or decision) at t2.

    Requires READ + intervening WRITE + dependent WRITE — 3-event pattern.
    Most structurally specific, so runs first.
    """
    if claimed is None:
        claimed = set()

    detections = []
    resources = [n for n, d in G.nodes(data=True) if d.get("node_type") == "resource"]
    processes = [n for n, d in G.nodes(data=True) if d.get("node_type") == "process"]

    for rid in resources:
        if rid in claimed:
            continue

        read_edges  = get_read_edges(G, rid)
        write_edges = get_write_edges(G, rid)

        for proc in processes:
            p_reads       = [(p, r, d) for p, r, d in read_edges  if p == proc]
            p_writes      = [(p, r, d) for p, r, d in write_edges if p == proc]
            other_writes  = [(p, r, d) for p, r, d in write_edges if p != proc]

            if not p_reads or not other_writes:
                continue

            for pr, _, dr in p_reads:
                t1 = dr["timestamp"]

                # Dependent write by same process within TAV window
                t2_candidates = [
                    d["timestamp"]
                    for p, _, d in p_writes
                    if t1 < d["timestamp"] <= t1 + TOCTOU_MAX_WINDOW_MS
                ]
                if not t2_candidates:
                    continue  # v2: require confirmed dependent write, not assumed

                for t2 in t2_candidates:
                    if (proc, rid) in S:
                        continue

                    # Intervening write by another process
                    intervening = [
                        (p, d) for p, _, d in other_writes
                        if t1 < d["timestamp"] < t2
                    ]

                    if intervening:
                        intruder_proc, intruder_d = intervening[0]
                        # Version must change to confirm real update
                        if intruder_d.get("version_id", 0) != dr.get("version_id", 0):
                            detections.append({
                                "race_class": "TAV",
                                "resource_id": rid,
                                "processes": [proc, intruder_proc],
                                "timestamps": [t1, intruder_d["timestamp"], t2],
                                "detection_timestamp": t1,
                                "window_ms": t2 - t1,
                            })
                            claimed.add(rid)
                            break  # one TAV per resource
                    if rid in claimed:
                        break
            if rid in claimed:
                break

    return detections


# ── PASS 2: SWC Detection ────────────────────────────────────────────────────
def detect_swc(G, S, claimed=None):
    """
    SWC: two processes concurrently WRITE the same resource within window δ.
    Skips resources already claimed by TAV.
    """
    if claimed is None:
        claimed = set()

    detections = []
    resources = [n for n, d in G.nodes(data=True) if d.get("node_type") == "resource"]

    for rid in resources:
        if rid in claimed:
            continue

        writes = get_write_edges(G, rid)
        if len(writes) < 2:
            continue

        found = False
        for i in range(len(writes)):
            if found:
                break
            for j in range(i + 1, len(writes)):
                pi, _, di = writes[i]
                pj, _, dj = writes[j]

                if pi == pj:
                    continue
                if (pi, rid) in S or (pj, rid) in S:
                    continue

                ti, tj = di["timestamp"], dj["timestamp"]
                if abs(ti - tj) < CONCURRENCY_WINDOW_MS:
                    vi, vj = di.get("version_id", 0), dj.get("version_id", 0)
                    if abs(vi - vj) <= 2:
                        detections.append({
                            "race_class": "SWC",
                            "resource_id": rid,
                            "processes": [pi, pj],
                            "timestamps": [ti, tj],
                            "detection_timestamp": min(ti, tj),
                        })
                        claimed.add(rid)
                        found = True
                        break  # one SWC per resource

    return detections


# ── PASS 3: CUR Detection ────────────────────────────────────────────────────
def detect_cur(G, S, claimed=None):
    """
    CUR: stale read chain. p1 reads r before p2 writes it; p3 acts on
    p1's stale value after the write. Requires path traversal depth >= 2.
    Skips resources already claimed by TAV or SWC.
    """
    if claimed is None:
        claimed = set()

    detections = []
    resources = [n for n, d in G.nodes(data=True) if d.get("node_type") == "resource"]

    for rid in resources:
        if rid in claimed:
            continue

        write_edges = get_write_edges(G, rid)
        read_edges  = get_read_edges(G, rid)

        found = False
        for p2, _, dw in write_edges:
            if found:
                break
            t_w = dw["timestamp"]

            stale_reads = [
                (p1, dr) for p1, _, dr in read_edges
                if p1 != p2
                   and dr["timestamp"] < t_w
                   and t_w - dr["timestamp"] <= CUR_STALE_WINDOW_MS
            ]

            for p1, dr in stale_reads:
                if (p1, rid) in S or (p2, rid) in S:
                    continue

                downstream = [
                    (p3, d3) for p3, _, d3 in read_edges
                    if p3 != p2 and d3["timestamp"] > t_w
                ]

                for p3, d3 in downstream:
                    if (p3, rid) in S:
                        continue
                    detections.append({
                        "race_class": "CUR",
                        "resource_id": rid,
                        "processes": [p1, p2, p3],
                        "timestamps": [dr["timestamp"], t_w, d3["timestamp"]],
                        "detection_timestamp": dr["timestamp"],
                        "stale_window_ms": t_w - dr["timestamp"],
                    })
                    claimed.add(rid)
                    found = True
                    break
                if found:
                    break

    return detections


# ── FULL DETECTOR ────────────────────────────────────────────────────────────
def detect_all(log_data):
    """
    Run all three passes with shared claimed set (mutual exclusivity).
    Pass order: TAV (most specific) → SWC → CUR.
    """
    G, events, S = build_frag(log_data)

    claimed = set()          # shared across passes — enforces class exclusivity
    tav = detect_tav(G, S, claimed)
    swc = detect_swc(G, S, claimed)
    cur = detect_cur(G, S, claimed)

    return {
        "log_id":    log_data["log_id"],
        "condition": log_data["condition"],
        "SWC": swc,
        "TAV": tav,
        "CUR": cur,
        "total": len(swc) + len(tav) + len(cur),
    }


if __name__ == "__main__":
    import sys
    from frag_builder import load_log

    if len(sys.argv) > 1:
        log = load_log(sys.argv[1])
        results = detect_all(log)
        print(f"Log: {results['log_id']}  Condition: {results['condition']}")
        print(f"  GT  : {log['n_races']} (SWC:{log['n_swc']} TAV:{log['n_tav']} CUR:{log['n_cur']})")
        print(f"  Det : {results['total']} (SWC:{len(results['SWC'])} TAV:{len(results['TAV'])} CUR:{len(results['CUR'])})")
