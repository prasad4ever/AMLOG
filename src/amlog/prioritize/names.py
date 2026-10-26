"""Names shared by scoring and the review page.

This module does not import a learning library.
"""

ISOLATION_MODEL_NAME = "alert_priority_isolation_forest"
BASELINE_MODEL_NAME = "alert_priority_baseline"
SUPERVISED_MODEL_NAME = "alert_productivity_logistic"
MODEL_VERSION = "1"

ISOLATION_FAMILY = "isolation_forest"
BASELINE_FAMILY = "robust_zscore"
SUPERVISED_FAMILY = "logistic_regression"

RANDOM_SEED = 20261026
TRAINING_FRACTION = 0.8
N_ESTIMATORS = 200
MIN_CURRENCY_OBSERVATIONS = 8
MIN_SUPERVISED_PER_CLASS = 25
MIN_SUPERVISED_REVIEWERS = 2

ISOLATION_SEMANTICS = (
    "Higher means more unusual than the alerts used to fit this Isolation Forest. "
    "This is an experiment anomaly rank. It is not a probability of financial crime, "
    "a productive investigation, or a false positive."
)
BASELINE_SEMANTICS = (
    "Higher means farther from the typical training alert on a robust z-score average. "
    "This is a non-ML comparison rank. It is not a probability of financial crime, "
    "a productive investigation, or a false positive."
)
