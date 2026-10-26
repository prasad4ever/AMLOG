"""Synthetic financial workload generation.

Importing this package does not open a database connection. The loader
writes only to a disposable database named by the caller. It does not
monitor payments or train a model.
"""

from amlog.workload.generate import (
    SCENARIO_CODES,
    GeneratedEvent,
    WorkloadSpec,
    iter_events,
    planned_customer_count,
)

__all__ = [
    "SCENARIO_CODES",
    "GeneratedEvent",
    "WorkloadSpec",
    "iter_events",
    "planned_customer_count",
]
