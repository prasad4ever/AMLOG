"""Controlled investigation-outcome evaluation.

This package reports whether a comparison of productive and non-productive
reviews can be supported from stored investigation evidence. It does not
invent those outcomes from rule triggers, anomaly ranks, or scenario codes.
The constructed benchmark in ``amlog.benchmark`` is a separate database.
This command does not read it and does not treat its judgments as reviewed
outcomes. The SynthAML research benchmark in ``amlog.synthaml`` is also
separate. This command does not read it, and a SynthAML Report or Dismiss
value is not an eligible investigator label.
"""

from amlog.evaluate.outcomes import EvaluationBlocked, OutcomeEvaluation, assess_outcome_evidence

__all__ = ["EvaluationBlocked", "OutcomeEvaluation", "assess_outcome_evidence"]
