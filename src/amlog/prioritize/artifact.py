"""Save and reload a fitted ranking without fitting it again.

The database stores a digest of the preprocessing parameters on the model
version. The trees and the parameter values are written under
``var/model-artifacts``. That folder is local output. A validated model row
is not edited. Loading a file calls ``decision_function`` and does not call
``fit``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
import sklearn

from amlog.prioritize.errors import PriorityError
from amlog.prioritize.preprocess import AlertSample, PreparedMatrix, _dense_row
from amlog.prioritize.rank import Ranking

DEVIATION_MEANING = (
    "Each robust z-score is the distance from the typical training value. "
    "It does not say that the feature caused the rank or the alert."
)


def default_artifact_dir() -> Path:
    """Local folder for fitted rankings. It is not part of the published source."""

    return Path(__file__).resolve().parents[3] / "var" / "model-artifacts"


def parameter_reference(prepared: PreparedMatrix, *, n_estimators: int | None, random_state: int) -> str:
    """Digest the training-fitted parameters. This is the database artifact reference."""

    import hashlib
    import json

    payload = {
        "columns": list(prepared.columns),
        "medians": {name: round(value, 8) for name, value in sorted(prepared.medians.items())},
        "scales": {
            f"{name}|{currency}": {"median": round(center, 8), "mad": round(spread, 8)}
            for (name, currency), (center, spread) in sorted(prepared.scales.items())
        },
        "n_estimators": n_estimators,
        "random_state": random_state,
        "sklearn": sklearn.__version__,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def artifact_file(directory: Path, reference: str) -> Path:
    """Map a parameter digest to one file inside the artifact directory."""

    prefix = "sha256:"
    if not reference.startswith(prefix) or len(reference) != len(prefix) + 64:
        raise PriorityError("The artifact reference is not a parameter digest.")
    digest = reference[len(prefix) :]
    if any(character not in "0123456789abcdef" for character in digest):
        raise PriorityError("The artifact reference is not a parameter digest.")
    return directory / f"sha256-{digest}.joblib"


def save_ranking_artifacts(
    prepared: PreparedMatrix,
    ranking: Ranking,
    *,
    isolation_reference: str,
    baseline_reference: str,
    n_estimators: int,
    random_state: int,
    directory: Path | None = None,
) -> tuple[Path, Path]:
    """Write the forest and the baseline parameters. This does not write a score."""

    root = (directory or default_artifact_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    shared = _parameters(prepared, ranking, random_state)
    isolation_path = _write(
        root,
        isolation_reference,
        {
            **shared,
            "parameter_reference": isolation_reference,
            "n_estimators": n_estimators,
            "forest": ranking.forest,
        },
    )
    baseline_path = _write(
        root,
        baseline_reference,
        {
            **shared,
            "parameter_reference": baseline_reference,
            "n_estimators": None,
            "forest": None,
        },
    )
    return isolation_path, baseline_path


def load_ranking_artifact(reference: str, directory: Path | None = None) -> dict[str, Any]:
    """Load one saved ranking. The file must match the requested parameter digest."""

    root = (directory or default_artifact_dir()).resolve()
    path = artifact_file(root, reference).resolve()
    if path.parent != root:
        raise PriorityError("The artifact path leaves the artifact directory.")
    if not path.is_file():
        raise PriorityError("No saved artifact matches this model version.")
    bundle = joblib.load(path)
    if not isinstance(bundle, dict) or bundle.get("parameter_reference") != reference:
        raise PriorityError("The saved artifact does not match this model version.")
    return bundle


def score_saved(samples: Sequence[AlertSample], bundle: Mapping[str, Any]) -> list[float]:
    """Score with a loaded ranking. This does not fit the model again."""

    matrix = np.asarray(_matrix(samples, bundle), dtype=np.float64)
    forest = bundle.get("forest")
    if forest is not None:
        return (-forest.decision_function(matrix)).tolist()
    return _baseline_from_spreads(matrix, bundle)


def _parameters(prepared: PreparedMatrix, ranking: Ranking, random_state: int) -> dict[str, Any]:
    return {
        "format_version": 1,
        "columns": list(prepared.columns),
        "imputation_medians": {
            name: float(value) for name, value in sorted(prepared.imputation_medians.items())
        },
        "scales": [
            {
                "feature": name,
                "currency": currency,
                "median": float(center),
                "mad": float(spread),
            }
            for (name, currency), (center, spread) in sorted(prepared.scales.items())
        ],
        "dropped_constant": list(prepared.dropped_constant),
        "baseline_spreads": [
            {"name": name, "center": float(center), "spread": float(spread)}
            for name, center, spread in ranking.baseline_spreads
        ],
        "random_state": random_state,
        "sklearn_version": sklearn.__version__,
        "deviation_meaning": DEVIATION_MEANING,
    }


def _write(root: Path, reference: str, bundle: dict[str, Any]) -> Path:
    path = artifact_file(root, reference)
    temporary = path.with_suffix(".joblib.tmp")
    joblib.dump(bundle, temporary)
    temporary.replace(path)
    return path


def _matrix(samples: Sequence[AlertSample], bundle: Mapping[str, Any]) -> list[tuple[float, ...]]:
    scales = {
        (item["feature"], item["currency"]): (float(item["median"]), float(item["mad"]))
        for item in bundle["scales"]
    }
    return [
        _dense_row(sample, scales, bundle["imputation_medians"], bundle["columns"])
        for sample in samples
    ]


def _baseline_from_spreads(matrix: np.ndarray, bundle: Mapping[str, Any]) -> list[float]:
    columns = list(bundle["columns"])
    scores: list[float] = []
    for row in matrix:
        distances = []
        for item in bundle["baseline_spreads"]:
            name = item["name"]
            indicator = f"{name}__missing"
            if indicator in columns and row[columns.index(indicator)] == 1.0:
                continue
            index = columns.index(name)
            distances.append(abs((float(row[index]) - float(item["center"])) / float(item["spread"])))
        scores.append(sum(distances) / len(distances) if distances else 0.0)
    return scores
