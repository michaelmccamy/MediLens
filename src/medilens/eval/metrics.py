"""Pure metric functions for the evaluation harness (CLAUDE.md section 8).

No pipeline, model, or database here: these take predictions and gold labels
and compute numbers, so they are fully unit-testable and the metric
definitions are auditable in one place.

Metric definitions:
- Code accuracy: micro-averaged precision, recall, and F1 over the set of
  recommended codes versus the set of expected codes, summed across cases.
- Denial prediction: binary precision and recall with "denied" as the positive
  class, after binarizing the risk score at a threshold.
- Citation correctness: two distinct numbers, because they mean different
  things (see CitationSummary).

Undefined ratios (zero denominator) return 0.0 by convention; callers that
need to distinguish "0.0 because wrong" from "0.0 because nothing to measure"
can read the raw counts.
"""

from dataclasses import dataclass


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


@dataclass(frozen=True)
class SetMetrics:
    """Micro-averaged set-overlap metrics (used for code accuracy)."""

    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        return _ratio(self.true_positives, self.true_positives + self.false_positives)

    @property
    def recall(self) -> float:
        return _ratio(self.true_positives, self.true_positives + self.false_negatives)

    @property
    def f1(self) -> float:
        precision = self.precision
        recall = self.recall
        if precision + recall == 0.0:
            return 0.0
        return 2 * precision * recall / (precision + recall)


def code_set_counts(
    predicted: frozenset[str] | set[str], expected: frozenset[str] | set[str]
) -> tuple[int, int, int]:
    """Return (true_positives, false_positives, false_negatives) for one case."""
    predicted_set = set(predicted)
    expected_set = set(expected)
    true_positives = len(predicted_set & expected_set)
    false_positives = len(predicted_set - expected_set)
    false_negatives = len(expected_set - predicted_set)
    return true_positives, false_positives, false_negatives


def aggregate_code_metrics(
    pairs: list[tuple[frozenset[str] | set[str], frozenset[str] | set[str]]]
) -> SetMetrics:
    """Micro-average code metrics across cases by summing the raw counts."""
    total_tp = 0
    total_fp = 0
    total_fn = 0
    for predicted, expected in pairs:
        true_positives, false_positives, false_negatives = code_set_counts(
            predicted, expected
        )
        total_tp = total_tp + true_positives
        total_fp = total_fp + false_positives
        total_fn = total_fn + false_negatives
    return SetMetrics(
        true_positives=total_tp,
        false_positives=total_fp,
        false_negatives=total_fn,
    )


@dataclass(frozen=True)
class BinaryMetrics:
    """Binary classification metrics with a named positive class."""

    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def precision(self) -> float:
        return _ratio(self.true_positives, self.true_positives + self.false_positives)

    @property
    def recall(self) -> float:
        return _ratio(self.true_positives, self.true_positives + self.false_negatives)

    @property
    def f1(self) -> float:
        precision = self.precision
        recall = self.recall
        if precision + recall == 0.0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    @property
    def total(self) -> int:
        return (
            self.true_positives
            + self.false_positives
            + self.false_negatives
            + self.true_negatives
        )


def binary_metrics(pairs: list[tuple[bool, bool]]) -> BinaryMetrics:
    """Compute binary metrics from (predicted, gold) pairs. Positive == True."""
    true_positives = 0
    false_positives = 0
    false_negatives = 0
    true_negatives = 0
    for predicted, gold in pairs:
        if predicted and gold:
            true_positives = true_positives + 1
        elif predicted and not gold:
            false_positives = false_positives + 1
        elif not predicted and gold:
            false_negatives = false_negatives + 1
        else:
            true_negatives = true_negatives + 1
    return BinaryMetrics(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        true_negatives=true_negatives,
    )


def denial_metrics_at_threshold(
    scored: list[tuple[float, bool]], threshold: float
) -> BinaryMetrics:
    """Binarize risk scores at threshold and compute denial metrics.

    scored is a list of (denial_risk_score, expected_denied). A score at or
    above the threshold predicts denial.
    """
    pairs: list[tuple[bool, bool]] = []
    for score, expected_denied in scored:
        pairs.append((score >= threshold, expected_denied))
    return binary_metrics(pairs)


def sweep_denial_thresholds(
    scored: list[tuple[float, bool]], thresholds: list[float]
) -> list[tuple[float, BinaryMetrics]]:
    """Denial metrics across thresholds, for tuning the false-neg/pos tradeoff.

    Uses stored scores, so a sweep costs no additional model calls. This is the
    surface CLAUDE.md section 8 calls for: decide and document the target
    tradeoff, then set the threshold against it.
    """
    sweep: list[tuple[float, BinaryMetrics]] = []
    for threshold in thresholds:
        sweep.append((threshold, denial_metrics_at_threshold(scored, threshold)))
    return sweep


@dataclass(frozen=True)
class CitationSummary:
    """Two distinct citation-correctness numbers.

    grounding_guarantee_held: whether every emitted recommendation carried only
    located spans. Verification enforces this, so it must always be True; the
    harness checks it as an invariant, and a False here is a serious bug, not a
    quality metric to optimize.

    model_clean_rate: the fraction of scored cases where the model produced
    nothing that verification had to drop (no rejections). This is the real
    quality signal: how often the model grounded everything by itself, before
    the safety net caught anything.
    """

    grounding_guarantee_held: bool
    model_clean_rate: float
    clean_cases: int
    scored_cases: int


def citation_summary(
    per_case: list[tuple[bool, int]]
) -> CitationSummary:
    """Summarize citation correctness from (grounding_ok, rejection_count) pairs."""
    scored_cases = len(per_case)
    clean_cases = 0
    guarantee_held = True
    for grounding_ok, rejection_count in per_case:
        if not grounding_ok:
            guarantee_held = False
        if rejection_count == 0:
            clean_cases = clean_cases + 1
    return CitationSummary(
        grounding_guarantee_held=guarantee_held,
        model_clean_rate=_ratio(clean_cases, scored_cases),
        clean_cases=clean_cases,
        scored_cases=scored_cases,
    )
