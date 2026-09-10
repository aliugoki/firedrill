"""Turning recorded drills into validated thresholds.

Invariant 7 says thresholds are configuration, validated against a calibration
set, and that no production number is invented. This package is how a number
earns the right to be called calibrated.

It is deliberately usable before any GPU work lands. The harness takes labelled
observations from anywhere — the simulator now, recorded drill footage in
Phase 2 — and the arithmetic is identical. Building it against simulated data
first means the day real footage exists, the only new work is labelling it.

Three stages:

    dataset.py   labelled observations, split into tune and validate
    sweep.py     threshold sweeps, error rates, operating-point selection
    report.py    the numbers, and the caveats that belong beside them
"""
