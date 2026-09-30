# FRAG Simulation — Run Instructions

Formal Modeling of Race Condition Vulnerabilities in FHIR-Based Healthcare Interoperability Systems.

## Current IIETA Submission Documents

- [`ISI_43109_Revised_Manuscript_TwoColumn_10pg.docx`](ISI_43109_Revised_Manuscript_TwoColumn_10pg.docx) — revised manuscript (two-column), with **[NEW]** markers showing what changed per reviewer feedback.
- [`Response_to_Reviewers.docx`](Response_to_Reviewers.docx) — point-by-point response to both reviewers' comments.

---

## Project Structure

```
frag_simulation/
├── log_generator.py       # Step 1 — generates synthetic FHIR transaction logs
├── frag_builder.py        # Step 2 — builds the FRAG directed graph from a log
├── race_detector.py       # Step 3 — three-pass SWC/TAV/CUR detector (FRAG)
├── baseline_scanner.py    # Step 3b — time-window baseline detector (comparison)
├── evaluate.py            # Step 4 — precision/recall/F1 evaluation + chart
├── requirements.txt       # Python dependencies
├── ISI_43109_Revised_Manuscript_TwoColumn_10pg.docx  # IIETA revised manuscript
├── Response_to_Reviewers.docx                       # Response to reviewers
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
python evaluate.py --logdir logs/C2 --outdir results/C2

# False-positive control — sequential
python evaluate.py --logdir logs/C1 --outdir results/C1

# Partial synchronization
python evaluate.py --logdir logs/C3 --outdir results/C3
```

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
python evaluate.py --logdir logs_test/C2 --outdir results_test --max 10
```

---

## Running the Detector on a Single Log

```bash
python race_detector.py logs/C2/log_0000.json
```

Output:
```
Log: log_0000   Condition: C2
  GT  : 38 races  (SWC:15  TAV:11  CUR:12)
  Det : 32 races  (SWC:12  TAV:11  CUR:9)
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
python evaluate.py --logdir logs/C1 --outdir results/C1
python evaluate.py --logdir logs/C2 --outdir results/C2
python evaluate.py --logdir logs/C3 --outdir results/C3
```

Expected results (C2):

| Class | F1 (FRAG) | F1 (Baseline) | Δ |
|-------|-----------|---------------|---|
| SWC | 89.1% | 28.8% | +60.3pp |
| TAV | 95.4% | 21.6% | +73.7pp |
| CUR | 86.6% | 25.9% | +60.6pp |
| Overall | 90.0% | 25.5% | +64.5pp |
