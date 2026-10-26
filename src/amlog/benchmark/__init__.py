"""Alert-linked investigation-outcome benchmark.

Judgments are protocol results on post-alert support findings. They are stored
only for alerts produced by the existing R001 and R002 replay, and they are
not operational training labels.
"""

from amlog.benchmark.protocol import DESIGN_ID, PROTOCOL_ACTOR, judge

__all__ = ["DESIGN_ID", "PROTOCOL_ACTOR", "judge"]
