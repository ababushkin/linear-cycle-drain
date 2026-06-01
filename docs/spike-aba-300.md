# Spike ABA-300 — Graphite PR-stacking sequence

This file is the throwaway payload for the ABA-300 spike. It exists to give
the two stacked spike PRs real, reviewable content.

## Step A (base layer, base = main)

Branch `spike/aba-300-step-a` is the bottom of the stack. Its PR targets `main`.

## Step B (stacked on A, base = spike/aba-300-step-a)

Branch `spike/aba-300-step-b` builds directly on Step A. Its PR must target
`spike/aba-300-step-a`, not `main` — that is what makes this a real stack.
