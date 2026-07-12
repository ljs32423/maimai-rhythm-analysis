# maimai Rhythm Analysis

> Parse `maidata.txt` → render rhythm charts, record preview videos, generate interactive HTML analysis pages with auto audio sync.

## Quick Start

```powershell
pip install -r requirements.txt
python -m mra.run_all -d "QZKago Requiem"
# → songs/QZKago Requiem/outputs/MASTER/html/analysis.html
```

## Stack

| Layer | Tech |
|-------|------|
| Parsing | Python `re` — custom [simai](https://w.atwiki.jp/simai/) format parser |
| Signal processing | `numpy`, `scipy.signal` (FFT cross-correlation, STFT) |
| Rendering | `matplotlib` → SVG + PNG rhythm charts |
| Recording | `subprocess` → MajdataView HTTP API → FFmpeg screen capture |
| Frontend | **Zero-dependency vanilla JS** — `requestAnimationFrame`, CSS `translate3d` GPU scroll |
| Orchestration | `subprocess` pipeline — each stage runs as independent `python -m` process |
| Runtime | MajdataView 4.3.1 (.NET 9), MajdataBridge (.NET 8), FFmpeg |

## Architecture

```
mra/
├── simai_parser.py      # Lexer/parser: maidata.txt → SongData (BPM timeline, Note[])
├── difficulty.py        # Difficulty ID ↔ name mapping, file path conventions
├── song_library.py      # Song discovery, folder naming, DX detection
├── visualize.py         # compute_rhythm_events() → matplotlib SVG/PNG render
├── render_preview.py    # MajdataView automation → FFmpeg → preview.mp4
├── align_audio.py       # Energy envelope cross-correlation + waveform refinement → offset.txt
├── make_html.py         # Template engine: injects JS constants + SVG segments → analysis.html
└── run_all.py           # Orchestrator: subprocess pipeline (visualize → render → align → html)
```

### Pipeline

```
maidata.txt ──[simai_parser]──▶ SongData
                                   │
     ┌─────────────────────────────┤
     ▼                             ▼
[visualize]                   [render_preview]
rhythm.{svg,png}               MajdataView → FFmpeg → preview.mp4
     │                             │
     ▼                             ▼
[align_audio] ◀── track.mp3 ──▶ energy envelope FFT cross-correlation
     │                             │
     ▼                             ▼
offset.txt ──────────────────▶ [make_html]
                                   │
                                   ▼
                            analysis.html
                      (vanilla JS, GPU-scrolled,
                       BPM-aware seek, ±1ms delay tuning)
```

### Audio Alignment Strategy

```
Tier 1 (primary)   Energy envelope (8ms frames, ±0.50 clip) →
                   FFT cross-correlation [-15s, +45s] →
                   waveform refinement (150ms radius, full-length)
                   │
                   ├─ confidence ≥ 0.15 → accept
                   └─ confidence < 0.15
                       │
                       ▼
Tier 2 (fallback)  RMS energy onset detection (5ms frames, P40 threshold)
                   → first sustained high-energy segment after 1s
```

## Project Layout

```
maimai-rhythm-analysis/
├── mra/                     # Core package
├── tests/                   # pytest (parser, alignment, workflow, generation)
├── tools/
│   ├── build_release.ps1    # Release packager
│   └── src/majdata_bridge/  # .NET 8 bridge (C#)
├── .tools/                  # MajdataView 4.3.1 + bridge binaries (gitignored)
├── songs/                   # Per-song input (gitignored)
│   └── <song>/
│       ├── maidata.txt
│       ├── track.mp3
│       ├── bg.png
│       └── outputs/<diff>/
└── release/                 # Build output (gitignored)
```

## Usage

```powershell
# Full pipeline (default: MASTER + Re:MASTER)
python -m mra.run_all                        # batch all songs
python -m mra.run_all -d "曲名"              # single song
python -m mra.run_all -d "曲名" -diff 5 -f   # specific diff, force overwrite

# Individual stages
python -m mra.visualize        -d "曲名" -diff 5
python -m mra.render_preview   -d "曲名" -diff 5 -f
python -m mra.align_audio      -d "曲名" -diff 5
python -m mra.make_html        -d "曲名" -diff 5 -f

# Test
python -m pytest -q
```

`-diff`: `1`=EASY `2`=BASIC `3`=ADVANCED `4`=EXPERT `5`=MASTER `6`=Re:MASTER `7`=UTOPIA

## Output

```
outputs/MASTER/
├── html/analysis.html      # Interactive player (play/pause/seek/±1s/倍速)
├── video/preview.mp4       # 2560×1440 60fps, PV off by default
├── sync/offset.txt         # Single-line float offset in seconds
├── rhythm/rhythm.{png,svg} # Full rhythm chart
└── strip/strip.svg         # Scroll bar base + segment SVGs (lazy-loaded)
```

Re:MASTER → `outputs/ReMASTER/`.

## Requirements

- Windows
- Python ≥ 3.10
- `pip install -r requirements.txt` (matplotlib, numpy, scipy)
- FFmpeg in PATH (or use release package which reuses MajdataView's bundled ffmpeg)

Input per song: `maidata.txt` + `track.mp3`. Optional: `bg.png`, `pv.mp4`.
Songs with touch/hold/firework notes auto-tagged as `曲名 [DX]`.

## Release

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_release.ps1
```

Produces `release/` — self-contained, no Python install required. Ship `app/` + `required-programs/` as siblings, run `run_all.bat`.
