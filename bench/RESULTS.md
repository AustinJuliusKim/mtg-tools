# Results — 2026-07-30, Apple M3 Max

CPython 3.9.6 (the repo venv) · Node v24.18.0 · Chromium 151 (headless, Playwright).
Protocol: 2 warmups, median of 7 in-process runs, per-stage timing. Every lane
emitted an identical oracle (rows/quantity/valueCents, pre- and post-merge) on
every dataset before its numbers counted.

## The tables

### real-922 — the actual Desktop exports (925 rows, 1,304 cards)

| lane | read | fetch | parse | merge | summarize | total ms | rows/s (parse) |
|---|---|---|---|---|---|---|---|
| browser | — | 3.5 | 2.4 | 0.1 | 0.1 | 2.6 | 385,417 |
| node | 0.2 | — | 2.6 | 0.2 | 0.1 | 3.1 | 357,143 |
| python-fast | 0.2 | — | 4.6 | 0.3 | 0.2 | 5.2 | 202,407 |
| python-app | 0.2 | — | 12.6 | 0.2 | 2.1 | 15.1 | 73,413 |

### synth-20k (20,000 rows, ~30% duplicate identities, 3.3 MB)

| lane | read | fetch | parse | merge | summarize | total ms | rows/s (parse) |
|---|---|---|---|---|---|---|---|
| browser | — | 6.4 | 38.7 | 3.0 | 2.7 | 44.4 | 516,796 |
| node | 1.1 | — | 47.0 | 5.6 | 2.6 | 56.3 | 425,080 |
| python-fast | 1.8 | — | 98.0 | 5.6 | 4.0 | 109.4 | 204,061 |
| python-app | 1.8 | — | 277.4 | 27.0 | 31.9 | 338.0 | 72,111 |
| python-stage | 1.8 | — | 585.5 | — | — | 587.3 | 34,161 |

### synth-100k (100,000 rows, 16.4 MB)

| lane | read | fetch | parse | merge | summarize | total ms | rows/s (parse) |
|---|---|---|---|---|---|---|---|
| browser | — | 20.7 | 174.7 | 21.3 | 20.1 | 216.1 | 572,410 |
| node | 4.6 | — | 218.8 | 29.1 | 18.2 | 270.6 | 457,122 |
| python-fast | 8.3 | — | 530.3 | 46.8 | 26.2 | 611.6 | 188,569 |
| python-app | 8.4 | — | 1618.5 | 327.4 | 325.4 | 2279.7 | 61,787 |
| python-stage | 8.4 | — | 3122.6 | — | — | 3131.1 | 32,024 |

### sealed-20k (20,000 rows, sealed dialect)

| lane | read | fetch | parse | total ms | rows/s (parse) |
|---|---|---|---|---|---|
| browser | — | 4.4 | 19.2 | 19.2 | 1,041,667 |
| node | 0.6 | — | 23.6 | 24.1 | 847,817 |
| python-app | 1.0 | — | 147.7 | 148.7 | 135,382 |

Memory at 100k rows: Python max RSS 437 MB, Node heap 463 MB — a wash.
Browser cold first run (before V8 warms up): 55 ms parse on 20k rows vs 39 ms
warm — the JIT tax exists but is small.

## Reading the numbers

1. **The hypothesis doesn't hold on speed.** V8 beats CPython 3.9 on this
   workload by 4–8× with a faithful port — real quoted-field parsing, integer
   cents, identical output to the cent. The gap is the runtime, not the
   algorithm: both sides run the same shape of code.
2. **The browser is not a penalty box.** In-page Chromium matched or slightly
   beat Node (same V8; the page even skips Node's module/startup overhead in
   the timed region). "Server-side handling vs in-browser handling" is a tie
   on the JS side — the meaningful gap is CPython vs V8.
3. **Half of Python's cost is exactness machinery.** python-fast (integer
   cents + dicts, same semantics) is 2–3× faster than python-app, so `Decimal`
   + dataclass construction account for roughly half the time. Still 2–4×
   slower than JS after that.
4. **Absolute numbers rescue Python in practice.** The real collection parses
   in 13 ms; a 100k-row import runs the full production staging path in 3.1 s.
   At this app's scale, Python's speed is irrelevant — its value is the
   already-tested invariant-rich codebase (exact money, undo log, dialect
   quirks), not throughput.
5. **For the PWA question:** parse performance is *not* a reason to keep the
   server. If the browser-storage app is ever built, CSV ingestion would be
   faster there, not slower. The real costs remain the ones identified before:
   porting ~3.3k lines of tested Python semantics, and browser-storage
   durability.

## Caveats

- CPython 3.9 is the floor this repo supports and what the venv runs; 3.12+
  would narrow the gap somewhat (typically 1.3–1.7× on parse-heavy code), not
  close it.
- PapaParse is a heavily-optimized parser; Python's `csv` is a C module too —
  both sides got their standard best tool.
- python-stage has no JS equivalent (it includes SQLite + 3× json.dumps per
  row); it's context for "what an import actually costs the app today".
- Single machine, single run day; medians of 7, but no cross-machine claims.
