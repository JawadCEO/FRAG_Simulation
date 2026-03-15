"""
baseline_scanner.py
Time-window baseline detector — represents current state of practice.

Logic: flag any two events accessing the same resource within δ_base = 200ms
where at least one is a WRITE.

This is what a healthcare IT security team would implement without a
formal model. Its weakness: it cannot distinguish structural races
(which depend on access type and dependency chains) from benign
concurrent reads, and it has no concept of dependency chains
(cannot detect CUR).
"""

BASELINE_WINDOW_MS = 200.0   # δ_base — standard EHR audit log window


def scan(log_data):
    """
    Apply time-window baseline scan to a log.
    Returns list of flagged (resource_id, race_class_guess, timestamp) tuples.
    """
    events = log_data["events"]

    # Group events by resource_id
    by_resource = {}
    for ev in events:
        rid = ev["resource_id"]
        if rid not in by_resource:
            by_resource[rid] = []
        by_resource[rid].append(ev)

    detections = []

    for rid, evs in by_resource.items():
        # Sort by timestamp
        evs_sorted = sorted(evs, key=lambda e: e["timestamp_ms"])

        for i in range(len(evs_sorted)):
            for j in range(i + 1, len(evs_sorted)):
                ei = evs_sorted[i]
                ej = evs_sorted[j]

                # Outside time window — stop inner loop
                if ej["timestamp_ms"] - ei["timestamp_ms"] > BASELINE_WINDOW_MS:
                    break

                # At least one must be a WRITE
                if ei["operation"] == "READ" and ej["operation"] == "READ":
                    continue

                # Different processes
                if ei["process"] == ej["process"]:
                    continue

                # Baseline cannot distinguish SWC / TAV / CUR —
                # it labels everything generically as "RACE"
                detections.append({
                    "race_class":           "RACE",
                    "resource_id":          rid,
                    "processes":            [ei["process"], ej["process"]],
                    "timestamps":           [ei["timestamp_ms"], ej["timestamp_ms"]],
                    "detection_timestamp":  ei["timestamp_ms"],
                })

    return {
        "log_id":    log_data["log_id"],
        "condition": log_data["condition"],
        "RACE":      detections,
        "total":     len(detections),
    }


if __name__ == "__main__":
    import sys
    import json
    from frag_builder import load_log

    if len(sys.argv) > 1:
        log = load_log(sys.argv[1])
        results = scan(log)
        print(f"Log: {results['log_id']}")
        print(f"  GT races   : {log['n_races']}")
        print(f"  Baseline   : {results['total']} flagged")
