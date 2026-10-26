"""The active benchmark is the alert-linked history in ``histories``.

Phase 10A payment dossiers remain available for the qualification audit. They
are not loaded and they are not given investigation outcomes.
"""

from amlog.benchmark.histories import build_cases
from amlog.benchmark.phase10a import audit_phase10a

__all__ = ["audit_phase10a", "build_cases"]
