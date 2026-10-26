"""Isolated SynthAML research benchmark.

The package ranks published alerts from an external synthetic monitoring
extract. It does not open an AMLOG database, generate monitoring alerts, or
approve a model.
"""

from amlog.synthaml.names import EXPERIMENT_ID

__all__ = ["EXPERIMENT_ID"]
