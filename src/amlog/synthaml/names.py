"""Fixed names for the SynthAML research experiment.

The capacities, seed, and feature definition are part of the experiment
declaration. They are not chosen from held-out scores.
"""

EXPERIMENT_ID = "phase11-synthaml-v1"
FEATURE_SET_NAME = "synthaml_alert_history"
FEATURE_SET_VERSION = "1"
MODEL_NAME = "synthaml_report_logistic"
MODEL_VERSION = "1"
ALGORITHM_FAMILY = "logistic_regression"

RANDOM_SEED = 20261026
MAX_ITER = 1_000_000
LOGISTIC_C = 1.0
SOLVER = "lbfgs"

# Published missing-group value. The smallest synthetic Size is about -2.9.
MISSING_GROUP_SENTINEL = -3.0

ENTRIES = ("Credit", "Debit")
TYPES = ("Card", "Cash", "International", "Wire")
STATISTICS = (
    "minimum",
    "mean",
    "median",
    "maximum",
    "standard_deviation",
    "count",
    "sum",
)
COUNT_OFFSET = STATISTICS.index("count")

FEATURE_NAMES = tuple(
    f"{statistic}_{entry}_{transaction_type}"
    for entry in ENTRIES
    for transaction_type in TYPES
    for statistic in STATISTICS
)

# Integer percent // 100, so 5%, 10%, and 20% never round upward.
CAPACITIES = ((5, 100), (10, 100), (20, 100))

PUBLISHED_ALERTS = 20_000
PUBLISHED_TRAIN_ALERTS = 10_839
PUBLISHED_TEST_ALERTS = 9_161
PUBLISHED_REPORTS = 3_435
PUBLISHED_DISMISSALS = 16_565
PUBLISHED_TRAIN_REPORTS = 1_809
PUBLISHED_TEST_REPORTS = 1_626
PUBLISHED_TRANSACTION_ROWS = 16_232_955
PUBLISHED_ALERT_BYTES = 505_481
PUBLISHED_TRANSACTION_BYTES = 934_561_137

ALERT_URL = "https://ndownloader.figshare.com/files/39841687"
TRANSACTION_URL = "https://ndownloader.figshare.com/files/39841711"
PAPER_DOI = "10.1038/s41597-023-02569-2"
ALERT_DOI = "10.6084/m9.figshare.22396054.v1"
TRANSACTION_DOI = "10.6084/m9.figshare.22396057.v1"

ENTRY_DEFINITION = (
    "SynthAML Entry is preserved as published. Credit is a transaction that "
    "decreases the client's balance. Debit is a transaction that increases it. "
    "These words are not AMLOG payment directions."
)
SIZE_DEFINITION = (
    "SynthAML Size is preserved as published. It is a log-transformed transaction "
    "size in Danske kroner, standardized by the dataset authors to zero mean and "
    "unit variance. It is not an AMLOG currency amount."
)
MISSING_GROUP_RULE = (
    "A type and entry with no transactions has count 0. The other six statistics "
    "are the published sentinel -3. That sentinel is kept and then scaled with the "
    "training mean and population standard deviation. It is not replaced by a training median."
)
SCORE_SEMANTICS = (
    "The score is the logistic regression estimate of the relative likelihood that "
    "the SynthAML investigation outcome is Report rather than Dismiss. It is a "
    "research-benchmark score. It is not a probability of financial crime, a "
    "conviction, or an operational AMLOG investigator label."
)
CHRONOLOGY_LIMIT = (
    "The published alert date is a random date inside the true quarter. Ordering "
    "by Date and then AlertID is the unprioritized queue available in the file. "
    "Within a quarter, that order is not the bank's original review order."
)
LIMITATIONS = (
    CHRONOLOGY_LIMIT,
    "The synthesizer was fit on the full alert extract, including alerts whose published dates fall in the 2021 test period.",
    "The published tables contain no customer or account identifier, so the same client cannot be kept on one side of the split.",
    "Report is about 17 percent of alerts in every quarter. The paper cites live false-positive rates of 95 to 98 percent, so this benchmark is less imbalanced than those live queues.",
    "Report and Dismiss are investigation outcomes in the synthetic benchmark. A Dismiss outcome does not establish that no financial crime occurred. A Report outcome is not a conviction.",
    "These metrics describe the SynthAML research benchmark. They are not measured results for a financial institution.",
)
