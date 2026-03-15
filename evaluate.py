"""
evaluate.py
Evaluates FRAG detector and baseline against ground-truth labels.

Matching rule (stated in paper):
  A detection is a True Positive if:
    - resource_id matches a ground-truth race event
    - race_class matches (FRAG only; baseline is class-agnostic)
    - detection_timestamp within ±MATCH_TOLERANCE_MS of GT event timestamp

Outputs:
  - Per-class precision, recall, F1 for FRAG and baseline
  - Overall metrics
  - Table II ready for insertion into the paper
  - Matplotlib bar chart saved as results_chart.png
"""

import json
import os
import glob
import numpy as np
from pathlib import Path
from collections import defaultdict

MATCH_TOLERANCE_MS = 50.0   # TP matching tolerance stated in paper

# ── GROUND TRUTH EXTRACTION ───────────────────────────────────────

def extract_ground_truth(log_data):
    """
    Extract injected ground-truth races from log.
    Returns dict: class → list of (resource_id, timestamp) tuples.
    """
    gt = defaultdict(list)
    for ev in log_data["events"]:
        if ev["is_race"] and ev["race_class"]:
            gt[ev["race_class"]].append({
                "resource_id": ev["resource_id"],
                "timestamp":   ev["timestamp_ms"],
            })
    return gt


# ── MATCHING ──────────────────────────────────────────────────────

def match_detections_to_gt(detections, gt_list):
    """
    Match a list of detections to ground-truth events.
    Each GT event can be matched at most once (prevent double-counting).
    Returns (tp, fp, fn).
    """
    matched_gt = set()
    tp = 0
    fp = 0

    for det in detections:
        rid = det["resource_id"]
        t_det = det["detection_timestamp"]
        matched = False

        for idx, gt_ev in enumerate(gt_list):
            if idx in matched_gt:
                continue
            if gt_ev["resource_id"] != rid:
                continue
            if abs(gt_ev["timestamp"] - t_det) <= MATCH_TOLERANCE_MS:
                tp += 1
                matched_gt.add(idx)
                matched = True
                break

        if not matched:
            fp += 1

    fn = len(gt_list) - len(matched_gt)
    return tp, fp, fn


def safe_metrics(tp, fp, fn):
    p  = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    r  = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
    return p, r, f1


# ── EVALUATE ONE LOG ──────────────────────────────────────────────

def evaluate_log(log_data, frag_results, baseline_results):
    """
    Evaluate FRAG and baseline on a single log.
    Returns per-class TP/FP/FN counts for both detectors.
    """
    gt = extract_ground_truth(log_data)
    all_gt = []
    for cls_events in gt.values():
        all_gt.extend(cls_events)

    frag_counts   = {}  # class → (tp, fp, fn)
    base_counts   = {}

    for cls in ["SWC", "TAV", "CUR"]:
        gt_cls = gt.get(cls, [])

        # FRAG: class-specific matching
        frag_dets = frag_results.get(cls, [])
        tp, fp, fn = match_detections_to_gt(frag_dets, gt_cls)
        frag_counts[cls] = (tp, fp, fn)

        # Baseline: class-agnostic — it flags any concurrent write pair.
        # For fair comparison, we check how many of its detections overlap
        # with GT events of this class specifically.
        # This reveals the baseline's structural weakness: it cannot distinguish
        # SWC/TAV/CUR, so its per-class FP rate is high.
        base_dets = baseline_results.get("RACE", [])
        tp_b, fp_b, fn_b = match_detections_to_gt(base_dets, gt_cls)
        base_counts[cls] = (tp_b, fp_b, fn_b)

    return frag_counts, base_counts


# ── AGGREGATE ACROSS ALL LOGS ─────────────────────────────────────

def aggregate_metrics(all_frag_counts, all_base_counts):
    """
    Sum TP/FP/FN across all logs, compute P/R/F1 per class and overall.
    """
    classes = ["SWC", "TAV", "CUR"]
    frag_metrics = {}
    base_metrics = {}

    total_frag_tp = total_frag_fp = total_frag_fn = 0
    total_base_tp = total_base_fp = total_base_fn = 0

    for cls in classes:
        ftp = sum(c[cls][0] for c in all_frag_counts)
        ffp = sum(c[cls][1] for c in all_frag_counts)
        ffn = sum(c[cls][2] for c in all_frag_counts)
        frag_metrics[cls] = safe_metrics(ftp, ffp, ffn)

        btp = sum(c[cls][0] for c in all_base_counts)
        bfp = sum(c[cls][1] for c in all_base_counts)
        bfn = sum(c[cls][2] for c in all_base_counts)
        base_metrics[cls] = safe_metrics(btp, bfp, bfn)

        total_frag_tp += ftp; total_frag_fp += ffp; total_frag_fn += ffn
        total_base_tp += btp; total_base_fp += bfp; total_base_fn += bfn

    frag_metrics["Overall"] = safe_metrics(total_frag_tp, total_frag_fp, total_frag_fn)
    base_metrics["Overall"] = safe_metrics(total_base_tp, total_base_fp, total_base_fn)

    return frag_metrics, base_metrics


# ── TABLE II PRINTER ──────────────────────────────────────────────

def print_table(frag_metrics, base_metrics):
    header = f"{'Class':<10} {'P(FRAG)':>9} {'R(FRAG)':>9} {'F1(FRAG)':>9} {'F1(Base)':>9} {'ΔF1(pp)':>9}"
    print("\n" + "="*60)
    print("TABLE II  Detection Performance: FRAG vs. Baseline")
    print("="*60)
    print(header)
    print("-"*60)

    for cls in ["SWC", "TAV", "CUR", "Overall"]:
        fp, fr, ff = frag_metrics[cls]
        _, _, bf   = base_metrics[cls]
        delta = (ff - bf) * 100
        print(f"{cls:<10} {fp*100:>8.1f}% {fr*100:>8.1f}% {ff*100:>8.1f}% {bf*100:>8.1f}% {delta:>+8.1f}pp")

    print("="*60 + "\n")


# ── CHART GENERATOR ───────────────────────────────────────────────

def save_chart(frag_metrics, base_metrics, outdir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        classes = ["SWC", "TAV", "CUR", "Overall"]
        frag_f1 = [frag_metrics[c][2] * 100 for c in classes]
        base_f1 = [base_metrics[c][2] * 100 for c in classes]

        x = np.arange(len(classes))
        w = 0.35

        fig, ax = plt.subplots(figsize=(8, 5))
        bars1 = ax.bar(x - w/2, frag_f1, w, label="FRAG (ours)", color="#1f4e79")
        bars2 = ax.bar(x + w/2, base_f1, w, label="Baseline",    color="#a9c4e0")

        ax.set_ylabel("F1 Score (%)", fontsize=11)
        ax.set_title("FRAG vs. Baseline — F1 Detection Performance", fontsize=12)
        ax.set_xticks(x)
        ax.set_xticklabels(classes, fontsize=11)
        ax.set_ylim(0, 105)
        ax.legend(fontsize=10)
        ax.grid(axis="y", linestyle="--", alpha=0.4)

        # Label bars
        for bar in bars1:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                    f"{bar.get_height():.1f}", ha="center", va="bottom", fontsize=9)
        for bar in bars2:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                    f"{bar.get_height():.1f}", ha="center", va="bottom", fontsize=9)

        plt.tight_layout()
        chart_path = os.path.join(outdir, "results_chart.png")
        plt.savefig(chart_path, dpi=150)
        plt.close()
        print(f"Chart saved → {chart_path}")
    except ImportError:
        print("matplotlib not available — skipping chart")


# ── MAIN ──────────────────────────────────────────────────────────

def main():
    import argparse
    from frag_builder import load_log
    from race_detector import detect_all
    from baseline_scanner import scan

    parser = argparse.ArgumentParser(description="Evaluate FRAG detector")
    parser.add_argument("--logdir",  default="logs/C2", help="Log directory to evaluate")
    parser.add_argument("--outdir",  default="results",  help="Output directory")
    parser.add_argument("--max",     type=int, default=None, help="Max logs to process")
    args = parser.parse_args()

    Path(args.outdir).mkdir(parents=True, exist_ok=True)

    log_files = sorted(glob.glob(os.path.join(args.logdir, "*.json")))
    if args.max:
        log_files = log_files[:args.max]

    print(f"Evaluating {len(log_files)} logs from {args.logdir}...")

    all_frag_counts = []
    all_base_counts = []

    for i, fpath in enumerate(log_files):
        if i % 50 == 0:
            print(f"  Processing log {i+1}/{len(log_files)}...")

        log      = load_log(fpath)
        frag_res = detect_all(log)
        base_res = scan(log)

        frag_c, base_c = evaluate_log(log, frag_res, base_res)
        all_frag_counts.append(frag_c)
        all_base_counts.append(base_c)

    frag_metrics, base_metrics = aggregate_metrics(all_frag_counts, all_base_counts)

    print_table(frag_metrics, base_metrics)
    save_chart(frag_metrics, base_metrics, args.outdir)

    # Save raw results
    results = {
        "n_logs":       len(log_files),
        "logdir":       args.logdir,
        "frag_metrics": {k: list(v) for k, v in frag_metrics.items()},
        "base_metrics": {k: list(v) for k, v in base_metrics.items()},
    }
    out_path = os.path.join(args.outdir, "metrics.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Metrics saved → {out_path}")


if __name__ == "__main__":
    main()
