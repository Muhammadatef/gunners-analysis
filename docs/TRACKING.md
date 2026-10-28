# Video tracking pipeline

Sibling to the Opta event warehouse. Extract player/ball tracks from a local
match video into parquet + Postgres aggregates — **do not** store the full
match file in the warehouse.

## Pilot: Arsenal 3–0 Fulham GW35

| | |
|---|---|
| `match_id` | 1903405 |
| Video kickoff | **7:33** → `--offset-s 453` |
| Event panels | `output/2025-2026/mw35_2026-05-02_arsenal_v_fulham/` |

```bash
# One-time host deps (not Airflow)
python3 -m venv .venv-tracking
source .venv-tracking/bin/activate
pip install -r requirements-tracking.txt
# Strongly recommended for fast 720p proxies:
#   sudo apt install ffmpeg

# Apply tables (stack up)
make tracking-schema

# 5 minutes of play from kickoff (MX350-safe defaults inside)
LOCAL=1 make tracking-run \
  VIDEO="/path/to/your/match_recording.mkv" \
  MATCH_ID=1903405 OFFSET_S=453 START_S=453 DURATION_S=300

# Overnight full match from kickoff (same offset; no duration cap)
LOCAL=1 make tracking-run \
  VIDEO="/path/to/your/match_recording.mkv" \
  MATCH_ID=1903405 OFFSET_S=453 FULL=1

# Equivalent CLI
.venv-tracking/bin/python -m tracking.cli \
  --match-id 1903405 --video "..." --offset-s 453 --full --device cpu

# Rebuild markdown only (reuse tracks.parquet)
LOCAL=1 make tracking-fuse MATCH_ID=1903405
```

Artifacts land in `data/tracking/1903405/` (`meta.json`, `tracks.parquet`,
`homography.json`, `phases.parquet`, optional `proxy_*.mp4`). Fused report:

`output/2025-2026/mw35_2026-05-02_arsenal_v_fulham/TRACKING_REPORT.md`

Time sync: `t_match_s = t_video_s - kickoff_offset_s` (453 for this file).

## FULL=1 overnight notes

- `FULL=1` sets start at kickoff (`OFFSET_S`) and runs to end of file — expect
  **many hours** on CPU / MX350.
- Prefer installing **ffmpeg** so the pipeline builds a 1280px H.264 proxy;
  without ffmpeg the CLI seeks the source MKV directly (works, slower I/O).
- Current PyTorch CUDA wheels often **lack sm_61** (MX350): pass `--device cpu`
  (or rely on the pipeline CUDA→CPU fallback).
- Do not leave the raw MKV under `output/` or load it into Postgres.

## Optional phase clips

After phases exist, cut 15–20s windows around interesting phase intervals.
Use **video** time = match time + `kickoff_offset_s`:

```bash
# Example: match minute 0:00–0:20 → video -ss 453
mkdir -p data/tracking/1903405/clips
ffmpeg -ss 453 -i "$VIDEO" -t 20 -c copy \
  data/tracking/1903405/clips/phase_kickoff.mp4

# Example: match t_start_s=120 → video -ss $((453+120))
ffmpeg -ss 573 -i "$VIDEO" -t 20 -c copy \
  data/tracking/1903405/clips/phase_demo.mp4
```

Clip paths can later be recorded in `tracking_clip_manifest` when that path is
wired; v1 keeps clips on disk only.

## GPU / model defaults

| Setting | Value |
|---|---|
| Proxy | 1280px wide H.264 (when ffmpeg present) |
| Model | `yolov8n.pt` |
| `imgsz` | 640 |
| Stride | every 5th frame (~5 Hz store) |
| Homography | default broadcast quad → `homography.json` (recalibrate per angle) |
