"""Failures of the isolated SynthAML research benchmark."""


class SynthamlError(Exception):
    """The published files or the declared experiment setup cannot be used."""


class SynthamlLeakageError(SynthamlError):
    """A transaction after the published alert date was present in the extract."""
