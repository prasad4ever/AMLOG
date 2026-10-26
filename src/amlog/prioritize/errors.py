"""Errors for alert prioritization."""


class PriorityError(ValueError):
    """The scoring run cannot be completed with the stored snapshots."""


class SupervisedTrainingBlocked(PriorityError):
    """Eligible reviewed outcomes are not sufficient to train."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
