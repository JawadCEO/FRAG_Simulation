"""
evaluate.py  —  v2
Evaluates FRAG detector and baseline against ground-truth race INSTANCES.

Key fix (v2): GT denominator is race instances, not labeled events.
  Each injected race spans 2-3 events (e.g. TAV = READ + WRITE + WRITE).
  A detection is a TP if its resource_id matches the resource of a GT race
  instance within timestamp tolerance. One detection matches one race instance.

Matching rule:
  TP: detected resource_id == GT race resource_id
      AND detection_timestamp within ±MATCH_TOLERANCE_MS of the earliest
      GT event timestamp for that race instance
      AND class matches (FRAG) or class-agnostic (baseline)
"""

import json, os, glob
import numpy as np
from pathlib import Path
from collections import defaultdict


MATCH_TOLERANCE_MS = 50.0


# ── GROUND TRUTH: race INSTANCES (not events) ─────────────────────────────────

def extract_gt_instances(log_data):
    """
    Return per-class list of race instances.
    Each instance = { resource_id, anchor_timestamp (earliest event in that race) }.
    Multiple events on the same resource with the same class = one instance.
    """
    # Group events by (resource_id, race_class)
    buckets = defaultdict(list)
    for ev in log_data["events"]:
        if ev["is_race"] and ev["race_class"]:
            buckets[(ev["resource_id"], ev["race_class"])].append(ev["timestamp_ms"])

    gt = defaultdict(list)
    for (rid, cls), timestamps in buckets.items():
        gt[cls].append({
            "resource_id": rid,
            "timestamp":   min(timestamps),   # anchor = earliest event
        })
    return gt


# ── MATCHING ──────────────────────────────────────────────────────────────────

def match_detections_to_gt(detections, gt_instances):
    """
    Match detections to GT race instances.
    Each GT instance matched at most once.
    Returns (tp, fp, fn).
    """
    matched = set()
    tp = fp = 0

    for det in detections:
        rid   = det["resource_id"]
        t_det = det["detection_timestamp"]
        found = False
        for idx, inst in enumerate(gt_instances):
            if idx in matched:
                continue
            if inst["resource_id"] != rid:
                continue
            if abs(inst["timestamp"] - t_det) <= MATCH_TOLERANCE_MS:
                tp += 1
                matched.add(idx)
                found = True
                break
        if not found:
            fp += 1

    fn = len(gt_instances) - len(matched)
    return tp, fp, fn


def safe_metrics(tp, fp, fn):
    p  = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    r  = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2*p*r / (p+r)  if (p  + r)  > 0 else 0.0
    return p, r, f1


# ── EVALUATE ONE LOG ──────────────────────────────────────────────────────────

def evaluate_log(log_data, frag_results, baseline_results):
    gt = extract_gt_instances(log_data)

    frag_counts = {}
    base_counts = {}

    for cls in ["SWC", "TAV", "CUR"]:
        gt_cls = gt.get(cls, [])

        # FRAG: class-specific
        tp, fp, fn = match_detections_to_gt(frag_results.get(cls, []), gt_cls)
        frag_counts[cls] = (tp, fp, fn)

        # Baseline: class-agnostic — evaluate against each class's GT
        tp_b, fp_b, fn_b = match_detections_to_gt(baseline_results.get("RACE", []), gt_cls)
        base_counts[cls] = (tp_b, fp_b, fn_b)

    return frag_counts, base_counts


# ── AGGREGATE ─────────────────────────────────────────────────────────────────

def aggregate_metrics(all_frag, all_base):
    classes = ["SWC", "TAV", "CUR"]
    frag_m = {}
    base_m = {}
    tot_f = [0,0,0]; tot_b = [0,0,0]

    for cls in classes:
        ftp = sum(c[cls][0] for c in all_frag)
        ffp = sum(c[cls][1] for c in all_frag)
        ffn = sum(c[cls][2] for c in all_frag)
        frag_m[cls] = safe_metrics(ftp, ffp, ffn)
        tot_f[0]+=ftp; tot_f[1]+=ffp; tot_f[2]+=ffn

        btp = sum(c[cls][0] for c in all_base)
        bfp = sum(c[cls][1] for c in all_base)
        bfn = sum(c[cls][2] for c in all_base)
        base_m[cls] = safe_metrics(btp, bfp, bfn)
        tot_b[0]+=btp; tot_b[1]+=bfp; tot_b[2]+=bfn

    frag_m["Overall"] = safe_metrics(*tot_f)
    base_m["Overall"] = safe_metrics(*tot_b)
    return frag_m, base_m


# ── OUTPUT ────────────────────────────────────────────────────────────────────

def print_table(frag_m, base_m, condition=""):
    hdr = f"{'Class':<10} {'P(FRAG)':>9} {'R(FRAG)':>9} {'F1(FRAG)':>9} {'F1(Base)':>9} {'ΔF1(pp)':>9}"
    tag = f"  [{condition}]" if condition else ""
    print("\n" + "="*62)
    print(f"Detection Performance: FRAG vs. Baseline{tag}")
    print("="*62)
    print(hdr)
    print("-"*62)
    for cls in ["SWC", "TAV", "CUR", "Overall"]:
        fp,fr,ff = frag_m[cls]
        _,_,bf   = base_m[cls]
        d = (ff-bf)*100
        print(f"{cls:<10} {fp*100:>8.1f}% {fr*100:>8.1f}% {ff*100:>8.1f}% {bf*100:>8.1f}% {d:>+8.1f}pp")
    print("="*62 + "\n")


def save_chart(frag_m, base_m, outdir, condition=""):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        classes = ["SWC", "TAV", "CUR", "Overall"]
        frag_f1 = [frag_m[c][2]*100 for c in classes]
        base_f1 = [base_m[c][2]*100 for c in classes]

        x = np.arange(len(classes)); w = 0.35
        fig, ax = plt.subplots(figsize=(8, 5))
        b1 = ax.bar(x-w/2, frag_f1, w, label="FRAG (ours)", color="#1f4e79")
        b2 = ax.bar(x+w/2, base_f1, w, label="Baseline",    color="#a9c4e0")

        ax.set_ylabel("F1 Score (%)", fontsize=11)
        title = f"FRAG vs. Baseline — F1 Detection Performance ({condition})" if condition else "FRAG vs. Baseline — F1 Detection Performance"
        ax.set_title(title, fontsize=11)
        ax.set_xticks(x); ax.set_xticklabels(classes, fontsize=11)
        ax.set_ylim(0, 110); ax.legend(fontsize=10)
        ax.grid(axis="y", linestyle="--", alpha=0.4)

        for bar in list(b1)+list(b2):
            ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+1,
                    f"{bar.get_height():.1f}", ha="center", va="bottom", fontsize=9)

        plt.tight_layout()
        path = os.path.join(outdir, "results_chart.png")
        plt.savefig(path, dpi=150); plt.close()
        print(f"Chart saved → {path}")
    except ImportError:
        print("matplotlib not available — skipping chart")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    from frag_builder import load_log
    from race_detector_v2 import detect_all
    from baseline_scanner import scan

    parser = argparse.ArgumentParser()
    parser.add_argument("--logdir",  default="logs/C2")
    parser.add_argument("--outdir",  default="results")
    parser.add_argument("--max",     type=int, default=None)
    args = parser.parse_args()

    condition = Path(args.logdir).name
    Path(args.outdir).mkdir(parents=True, exist_ok=True)

    log_files = sorted(glob.glob(os.path.join(args.logdir, "*.json")))
    if args.max:
        log_files = log_files[:args.max]

    print(f"Evaluating {len(log_files)} logs from {args.logdir}...")

    all_frag = []; all_base = []
    for i, fpath in enumerate(log_files):
        if i % 50 == 0:
            print(f"  Processing log {i+1}/{len(log_files)}...")
        log      = load_log(fpath)
        frag_res = detect_all(log)
        base_res = scan(log)
        fc, bc   = evaluate_log(log, frag_res, base_res)
        all_frag.append(fc); all_base.append(bc)

    frag_m, base_m = aggregate_metrics(all_frag, all_base)
    print_table(frag_m, base_m, condition)
    save_chart(frag_m, base_m, args.outdir, condition)

    results = {
        "n_logs":       len(log_files),
        "condition":    condition,
        "logdir":       args.logdir,
        "frag_metrics": {k: list(v) for k,v in frag_m.items()},
        "base_metrics": {k: list(v) for k,v in base_m.items()},
    }
    out = os.path.join(args.outdir, "metrics.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Metrics saved → {out}")


if __name__ == "__main__":
    main()