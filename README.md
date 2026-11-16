# FRAG Simulation — Run Instructions

Formal Modeling of Race Condition Vulnerabilities in FHIR-Based Healthcare Interoperability Systems.

---

## Project Structure

```
frag_simulation/
├── log_generator.py       # Step 1 — generates synthetic FHIR transaction logs
├── frag_builder.py        # Step 2 — builds the FRAG directed graph from a log
├── race_detector_v2.py    # Step 3 — three-pass TAV/SWC/CUR detector (FRAG) — used for the paper
├── baseline_scanner.py    # Step 3b — time-window baseline detector (comparison)
├── evaluate_v2.py         # Step 4 — race-instance P/R/F1 evaluation + chart — used for the paper
├── scaling_benchmark.py   # Step 5 — runtime / memory / F1 vs. log size
├── race_detector.py       # original v1 detector (kept for reference)
├── evaluate.py            # original v1 evaluator (kept for reference, see note below)
├── requirements.txt       # Python dependencies
└── README.md              # This file
```

---

## Requirements

- Python 3.10 or higher
- pip

---

## Installation

```bash
pip install -r requirements.txt
```

Or install manually:

```bash
pip install networkx numpy matplotlib
```

---

## Running the Full Pipeline

### Step 1 — Generate Logs

Generates 500 synthetic FHIR transaction logs for each of three conditions:
- **C1** — Sequential (no concurrency, used as false-positive control)
- **C2** — Concurrent, unsynchronized (primary evaluation condition)
- **C3** — Concurrent, partially synchronized with ETag (70% of writes)

```bash
python log_generator.py --n_logs 500 --outdir logs --seed 42
```

This creates:
```
logs/
├── C1/   500 JSON logs  (sequential, no races)
├── C2/   500 JSON logs  (concurrent, races injected)
└── C3/   500 JSON logs  (partial ETag sync, residual races)
```

**Options:**
```
--n_logs      Number of logs per condition  (default: 500)
--n_patients  Patient pool size             (default: 50)
--seed        Random seed                   (default: 42)
--outdir      Output directory              (default: logs)
```

---

### Step 2 — Run Evaluation (FRAG vs Baseline)

Evaluate each condition separately. Results are saved to the output directory.

```bash
# Primary condition — concurrent, unsynchronized
python evaluate_v2.py --logdir logs/C2 --outdir results/C2

# False-positive control — sequential
python evaluate_v2.py --logdir logs/C1 --outdir results/C1

# Partial synchronization
python evaluate_v2.py --logdir logs/C3 --outdir results/C3
```

> **Use `evaluate_v2.py`.** It runs the v2 detector (`race_detector_v2.py`) and
> scores at the race-instance level, which is the evaluation reported in the
> paper. The original `evaluate.py` (v1 detector, event-level matching) is kept
> only for reference: on the same C2 logs it reports 44.4% overall F1 for FRAG
> vs. 36.7% for the baseline, and its numbers should not be cited.

Each run produces:
- **Console output** — precision/recall/F1 table (FRAG vs baseline)
- `results/<condition>/metrics.json` — raw metrics for all classes
- `results/<condition>/results_chart.png` — F1 bar chart

**Options:**
```
--logdir   Directory of JSON logs to evaluate
--outdir   Where to write results
--max      Limit number of logs (useful for quick tests, e.g. --max 10)
```

---

### Quick Test (10 logs, ~5 seconds)

```bash
python log_generator.py --n_logs 10 --outdir logs_test --seed 42
python evaluate_v2.py --logdir logs_test/C2 --outdir results_test --max 10
```

---

## Running the Detector on a Single Log

```bash
python race_detector_v2.py logs/C2/log_0000.json
```

Output:
```
Log: C2_0000  Condition: C2
  GT  : 29 (SWC:11 TAV:8 CUR:10)
  Det : 28 (SWC:10 TAV:8 CUR:10)
```

---

## Understanding the Output Table

```
================================================================
Detection Performance: FRAG vs. Baseline  [C2]
================================================================
Class      P(FRAG)   R(FRAG)  F1(FRAG)  F1(Base)   ΔF1(pp)
----------------------------------------------------------------
SWC          88.6%     89.7%     89.1%     28.8%    +60.3pp
TAV          93.1%     97.7%     95.4%     21.6%    +73.7pp
CUR          88.0%     85.2%     86.6%     25.9%    +60.6pp
Overall      89.7%     90.4%     90.0%     25.5%    +64.5pp
================================================================
```

- **P** = Precision — of all detections, how many are real races
- **R** = Recall — of all real races, how many were detected
- **F1** = Harmonic mean of precision and recall
- **ΔF1** = FRAG F1 minus Baseline F1 in percentage points

Metrics are computed at the **race-instance level**: one detection matches one injected race instance (identified by resource_id + timestamp). Each ground-truth race instance is matched at most once.

---

## Race Classes

| Class | Pattern | Clinical Risk |
|-------|---------|---------------|
| SWC | Two concurrent WRITEs, no sync | Lost prescription update |
| TAV | READ → intervening WRITE → dependent action | Authorization bypass |
| CUR | Stale READ, downstream action (path depth ≥ 2) | Contraindicated drug |

---

## Reproducibility

All results in the paper use `--seed 42` and `--n_logs 500`. To exactly reproduce Table III and Table IV from the paper:

```bash
python log_generator.py --n_logs 500 --outdir logs --seed 42
python evaluate_v2.py --logdir logs/C1 --outdir results/C1
python evaluate_v2.py --logdir logs/C2 --outdir results/C2
python evaluate_v2.py --logdir logs/C3 --outdir results/C3
```

Expected results (C2):

| Class | F1 (FRAG) | F1 (Baseline) | Δ |
|-------|-----------|---------------|---|
| SWC | 89.1% | 28.8% | +60.3pp |
| TAV | 95.4% | 21.6% | +73.7pp |
| CUR | 86.6% | 25.9% | +60.6pp |
| Overall | 90.0% | 25.5% | +64.5pp |

---

## Scaling Benchmark

`scaling_benchmark.py` measures how FRAG's runtime, memory, and accuracy change
as a single log grows from ~1,000 to ~100,000 events.

```bash
python scaling_benchmark.py                                   # sizes 1k → 100k, 3 reps each
python scaling_benchmark.py --sizes 1000 5000 10000 25000 --reps 3
```

Outputs in `results/scaling/`:
- `scaling_runs.csv`: one row per run (events, runtime, events/s, peak memory, P/R/F1)
- `scaling_summary.json`: per-size mean ± std, linear and log-log fits, machine info
- `scaling_chart.png`: runtime vs. size with linear fit; F1 vs. size

**How the logs are scaled (constant density).** A log of N events is built from
paper-sized segments, each produced by `log_generator.generate_log()` (200–800
events, 8–40 injected races, 50 patients, 10 s window). Each segment gets its
own time slot and patient namespace. Events per resource, events per second,
and race density therefore match the paper's logs. What grows is how much one
detector run must process, not contention on any single resource.

**Measured results** (C2, seed 42, 3 reps per size; Intel Xeon @ 2.30 GHz,
4 vCPU, 15 GB RAM, Python 3.11, networkx 3.6.1). Time covers graph construction
plus all three detection passes.

| Events per log | FRAG time (s) | Events/s | Peak heap (MB) | F1 FRAG | F1 Baseline |
|---:|---:|---:|---:|---:|---:|
| 1,330   | 0.013 ± 0.002 | 98,981 | 0.8  | 89.7% | 26.2% |
| 5,147   | 0.060 ± 0.009 | 86,900 | 3.2  | 90.2% | 24.3% |
| 10,279  | 0.125 ± 0.019 | 83,274 | 6.5  | 89.8% | 25.2% |
| 25,161  | 0.323 ± 0.013 | 77,999 | 15.4 | 90.4% | 25.4% |
| 50,398  | 0.685 ± 0.029 | 73,686 | 31.0 | 90.2% | 25.4% |
| 100,343 | 1.464 ± 0.052 | 68,622 | 62.0 | 90.2% | 25.2% |

- Linear fit: ~14.7 µs per event, R² = 0.997. Log-log fit: t ∝ n^1.08, close to linear.
- Accuracy holds: F1 stays at 89.7–90.4%, in line with the 90.0% on the paper's 500 C2 logs.
- Timings depend on the machine. Re-run the script to get numbers for yours.

**Note on `frag_builder.get_edges_for_resource`.** It previously scanned every
edge in the graph for each resource, which made detection quadratic in log size
(about 33 s at 10k events). It now uses the resource's in-edges, sorted into the
same order, so the result is unchanged. The detector output is byte-identical
on all 1,500 logs (C1/C2/C3, seed 42), and the paper's metrics are unaffected.
