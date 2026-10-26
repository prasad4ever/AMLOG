"""Transaction ingestion.

Importing this package does not open a database connection and does not
contact AWS. A caller supplies a PostgreSQL connection when it is ready
to store an event.
"""

from amlog.ingest.contract import (
    EVENT_TYPE,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    EventValidationError,
    FieldError,
    TransactionEvent,
    parse_transaction_event,
)
from amlog.ingest.service import IngestionResult, TransactionIngestor

__all__ = [
    "EVENT_TYPE",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "EventValidationError",
    "FieldError",
    "IngestionResult",
    "TransactionEvent",
    "TransactionIngestor",
    "parse_transaction_event",
]
