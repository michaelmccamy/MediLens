"""Evaluation harness for the reasoning pipeline (CLAUDE.md section 8).

Loads a labeled set of synthetic cases, runs each through the real pipeline,
and computes the three metrics section 8 names: code recommendation accuracy,
denial-prediction precision and recall, and citation correctness. A threshold
sweep supports tuning the denial threshold against the documented tradeoff.

The gold labels shipped here are synthetic placeholders. They scaffold the
metrics; they are not a substitute for certified-coder review, and no number
computed against them is a real accuracy claim until the labels are reviewed.
"""
