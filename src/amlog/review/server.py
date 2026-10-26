"""Minimal local review pages.

The server binds to 127.0.0.1. It is not authentication and it is not a
second product surface. Later ranking and explanation can be added to
these same pages.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import UUID

from amlog.prioritize.names import ISOLATION_MODEL_NAME, BASELINE_MODEL_NAME
from amlog.prioritize.supervised import assess_supervised_readiness
from amlog.review.service import (
    DISPOSITIONS,
    AlertDetail,
    ReviewError,
    add_finding,
    close_case,
    count_alerts,
    get_alert,
    list_alerts,
    open_case,
    readiness,
    record_assessment,
    reopen_case,
)
from amlog.synthaml.errors import SynthamlError
from amlog.synthaml.exhibit import ResearchExhibit, load_exhibit
from amlog.synthaml.run import default_artifact_dir

PAGE_SIZE = 40
_RESEARCH_ARTIFACT = "synthaml-phase11-evaluation.json"
_SELECTION_ARTIFACT = "synthaml-phase11c-selection.json"


class ReviewServer(ThreadingHTTPServer):
    """One local review session bound to a database URL."""

    def __init__(self, address: tuple[str, int], database_url: str) -> None:
        self.database_url = database_url
        super().__init__(address, ReviewHandler)


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/synthaml":
            self._send_html(_render_synthaml())
            return
        connection = None
        try:
            connection = self._connect()
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                self._send_html(_render_list(connection, query))
                return
            if parsed.path == "/alert":
                alert_id = _uuid(query.get("id", [""])[0])
                error = query.get("error", [""])[0]
                detail = get_alert(connection, alert_id)
                self._send_html(_render_detail(detail, error))
                return
            self._send_html("<p>That page was not found.</p>", status=404)
        except ReviewError as exc:
            self._send_html(f"<p>{html.escape(str(exc))}</p>", status=400)
        except Exception:
            self._send_html("<p>The page could not be shown.</p>", status=500)
        finally:
            if connection is not None:
                connection.close()

    def do_POST(self) -> None:
        raw = ""
        try:
            if urlparse(self.path).path == "/synthaml":
                self._send_html("<p>The SynthAML research benchmark is read-only.</p>", status=405)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            form = parse_qs(raw)
            alert_id = _uuid(form.get("alert_id", [""])[0])
            reviewer = form.get("reviewer", [""])[0]
            rationale = form.get("rationale", [""])[0]
            parsed = urlparse(self.path)
            with self._connect() as connection:
                if parsed.path == "/open":
                    open_case(connection, alert_id, reviewer)
                elif parsed.path == "/finding":
                    add_finding(connection, alert_id, reviewer, rationale)
                elif parsed.path == "/assessment":
                    record_assessment(
                        connection,
                        alert_id,
                        reviewer,
                        form.get("usefulness", [""])[0],
                        rationale,
                    )
                elif parsed.path == "/close":
                    close_case(
                        connection,
                        alert_id,
                        reviewer,
                        form.get("disposition", [""])[0],
                        rationale,
                    )
                elif parsed.path == "/reopen":
                    reopen_case(connection, alert_id, reviewer, rationale)
                else:
                    self._send_html("<p>That action was not found.</p>", status=404)
                    return
            self._redirect(f"/alert?id={alert_id}")
        except ReviewError as exc:
            alert_value = ""
            try:
                alert_value = parse_qs(raw).get("alert_id", [""])[0]
            except Exception:
                alert_value = ""
            if alert_value:
                self._redirect("/alert?" + urlencode({"id": alert_value, "error": str(exc)}))
                return
            self._send_html(f"<p>{html.escape(str(exc))}</p>", status=400)
        except Exception:
            self._send_html("<p>The action could not be saved.</p>", status=500)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _connect(self):
        import psycopg

        return psycopg.connect(self.server.database_url, connect_timeout=5)

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = _page(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


def _render_list(connection, query: dict[str, list[str]]) -> str:
    try:
        rule = query.get("rule", ["all"])[0]
        state = query.get("state", ["all"])[0]
        sort = query.get("sort", ["time"])[0]
        page = max(0, int(query.get("page", ["0"])[0]))
    except ValueError as exc:
        raise ReviewError("The list filters are not valid.") from exc
    rule_code = None if rule in {"", "all"} else rule
    if rule_code not in {None, "R001", "R002"}:
        raise ReviewError("Rule filter must be R001, R002, or all.")
    offset = page * PAGE_SIZE
    rows = list_alerts(
        connection,
        rule_code=rule_code,
        review_state=state,
        sort=sort,
        limit=PAGE_SIZE,
        offset=offset,
    )
    total = count_alerts(connection, rule_code=rule_code, review_state=state)
    report = readiness(connection)
    supervised = assess_supervised_readiness(connection)
    body = [
        "<h1>Alert review</h1>",
        "<p><strong>Investigator queue.</strong> These rows are the AMLOG workload alerts. "
        "The <a href='/synthaml'>SynthAML Research Benchmark</a> is a separate read-only page. "
        "Its research outcomes are not in this queue and are not training labels.</p>",
        "<p>Open an alert to record a human review. Nothing on this list has an outcome until a reviewer saves one.</p>",
        "<p>An anomaly rank is an experimental ordering of alerts. It is not a probability of financial crime, a productive investigation, or a false positive.</p>",
        "<p>"
        f"Alerts: {report.alerts}. "
        f"Cases: {report.alerts_with_a_case}. "
        f"Closed cases: {report.closed_cases}. "
        f"Assessments: {report.investigator_assessments}. "
        f"Usable training outcomes: {report.usable_training_outcomes}."
        "</p>",
        f"<p>{_h(supervised.message)}</p>",
        "<form method='get' action='/'>"
        "<label>Rule <select name='rule'>"
        + _options(("all", "R001", "R002"), rule)
        + "</select></label> "
        "<label>Review <select name='state'>"
        + _options(("all", "unreviewed", "open", "closed"), state)
        + "</select></label> "
        "<label>Order <select name='sort'>"
        + _options(("time", "priority"), sort)
        + "</select></label> "
        "<button type='submit'>Apply</button></form>",
        "<table><thead><tr><th>When</th><th>Rule</th><th>Customer</th><th>Account</th><th>Anomaly rank</th><th>Review</th></tr></thead><tbody>",
    ]
    for row in rows:
        review = "Not opened"
        if row.case_status == "closed":
            review = f"Closed: {row.disposition}"
        elif row.case_status:
            review = row.case_status
        body.append(
            "<tr>"
            f"<td>{_h(row.triggered_at.isoformat())}</td>"
            f"<td>{_h(row.rule_code)} v{row.rule_version}</td>"
            f"<td><a href='/alert?id={row.alert_id}'>{_h(row.customer_reference)}</a></td>"
            f"<td>{_h(row.account_reference or '')}</td>"
            f"<td>{'' if row.priority_rank is None else row.priority_rank}</td>"
            f"<td>{_h(review)}</td>"
            "</tr>"
        )
    body.append("</tbody></table>")
    if page > 0:
        body.append(f"<a href='/?{_query(rule, state, page - 1, sort)}'>Previous</a> ")
    if offset + PAGE_SIZE < total:
        body.append(f"<a href='/?{_query(rule, state, page + 1, sort)}'>Next</a>")
    body.append(f"<p>Showing {len(rows)} of {total}.</p>")
    return "".join(body)


def _render_synthaml(path: Path | None = None) -> str:
    """Read-only research page. It does not query or write the review database."""

    artifact = path if path is not None else default_artifact_dir() / _RESEARCH_ARTIFACT
    try:
        exhibit = load_exhibit(artifact)
    except (SynthamlError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return (
            "<p><a href='/'>Back to the investigator queue</a></p>"
            "<h1>SynthAML Research Benchmark</h1>"
            "<p>The saved evaluation artifact could not be read. This page does not fit a model.</p>"
            + _research_boundary()
        )
    if exhibit is None:
        return (
            "<p><a href='/'>Back to the investigator queue</a></p>"
            "<h1>SynthAML Research Benchmark</h1>"
            "<p>The completed evaluation artifact is not on this machine. "
            "This page does not fit a model and does not invent benchmark rows.</p>"
            + _research_boundary()
        )
    selection = _load_selection(artifact.parent / _SELECTION_ARTIFACT)
    return _research_body(exhibit, selection)


def _research_boundary() -> str:
    return (
        "<h2>Limitations</h2>"
        "<ul>"
        "<li>The data are synthetic research alerts from the published SynthAML extract, not a financial institution's queue.</li>"
        "<li>Published alert dates are reliable to the quarter. Within a quarter, the date was randomized.</li>"
        "<li>The synthesizer was fit on the full extract, including alerts later held out by date, so synthesis-related leakage remains.</li>"
        "<li>The published tables have no account or customer identifier.</li>"
        "<li>This page shows no verified real-world productivity improvement.</li>"
        "<li>Report and Dismiss stay in the research artifact. They are not written to investigator labels, "
        "and they do not open the operational supervised-training gate.</li>"
        "</ul>"
    )


def _load_selection(path: Path) -> dict[str, Any] | None:
    """Read the Phase 11C comparison when it is present. A missing file leaves the baseline page."""

    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"unreadable": True}
    if not isinstance(document, dict) or document.get("experiment_id") != "phase11c-synthaml-v1":
        return {"unreadable": True}
    return document


def _selection_section(selection: dict[str, Any] | None) -> str:
    """Show the locked candidate beside the logistic baseline. This does not fit a model."""

    if not selection:
        return ""
    if selection.get("unreadable"):
        return (
            "<h2>Model selection</h2>"
            "<p>The saved model-selection artifact could not be read. "
            "The logistic regression baseline below is unchanged.</p>"
        )
    parts = [
        "<h2>Selected experimental model</h2>",
        "<p>The candidate below was locked from 2020 forward-quarter validation. "
        "The logistic regression section further down is the Phase 11 baseline, "
        "kept so the two results stay distinct. Neither model is approved for operational use.</p>",
        f"<p>{_h(selection.get('holdout_disclosure', ''))}</p>",
        "<ul>",
        f"<li>Selected candidate: {_h(selection.get('selected_candidate'))}</li>",
        f"<li>Primary metric: {_h(selection.get('primary_metric'))}</li>",
        f"<li>{_h(selection.get('selection_reason'))}</li>",
        "</ul>",
        "<h2>2020 validation comparison</h2>",
        "<table><thead><tr><th>Candidate</th><th>Reports at 10 percent</th>"
        "<th>Precision at 10 percent, standard deviation</th>"
        "<th>Mean PR-AUC</th></tr></thead><tbody>",
    ]
    for row in selection.get("validation_totals") or []:
        parts.append(
            "<tr>"
            f"<td>{_h(row.get('candidate'))}</td>"
            f"<td>{_n(int(row.get('reports_at_10', 0)))}</td>"
            f"<td>{float(row.get('precision_at_10_std', 0.0)):.6f}</td>"
            f"<td>{float(row.get('average_precision_mean', 0.0)):.6f}</td>"
            "</tr>"
        )
    date_total = sum(int(row.get("reports_at_10", 0)) for row in selection.get("date_order_validation") or [])
    parts.append(
        f"<tr><td>date_order</td><td>{_n(date_total)}</td><td></td><td></td></tr>"
        "</tbody></table>"
        "<h2>2021 comparison after the choice was locked</h2>"
        "<table><thead><tr><th>Candidate</th><th>Reports at 5 percent</th>"
        "<th>Reports at 10 percent</th><th>Reports at 20 percent</th>"
        "<th>Precision at 10 percent</th><th>Recall at 10 percent</th>"
        "<th>PR-AUC</th></tr></thead><tbody>"
    )
    for row in selection.get("holdout_2021") or []:
        label = str(row.get("candidate"))
        if row.get("selected"):
            label = f"{label} (selected)"
        parts.append(
            "<tr>"
            f"<td>{_h(label)}</td>"
            f"<td>{_n(int(row.get('reports_at_5', 0)))}</td>"
            f"<td>{_n(int(row.get('reports_at_10', 0)))}</td>"
            f"<td>{_n(int(row.get('reports_at_20', 0)))}</td>"
            f"<td>{float(row.get('precision_at_10', 0.0)):.6f}</td>"
            f"<td>{float(row.get('recall_at_10', 0.0)):.6f}</td>"
            f"<td>{float(row.get('average_precision', 0.0)):.6f}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    sample = selection.get("selected_sample") or []
    if sample:
        parts.append(
            "<h2>Highest saved ranks of the selected model</h2>"
            "<p>These are the first 2021 alerts in the selected order. "
            "The outcome is the published research label.</p>"
            "<table><thead><tr><th>Rank</th><th>AlertID</th><th>Score</th>"
            "<th>Published research outcome</th></tr></thead><tbody>"
        )
        for row in sample:
            parts.append(
                "<tr>"
                f"<td>{_n(int(row.get('rank', 0)))}</td>"
                f"<td>{_n(int(row.get('alert_id', 0)))}</td>"
                f"<td>{float(row.get('score', 0.0)):.6f}</td>"
                f"<td>{_h(row.get('outcome'))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
    return "".join(parts)


def _research_body(exhibit: ResearchExhibit, selection: dict[str, Any] | None = None) -> str:
    model_heading = "Logistic regression baseline" if selection else "Model"
    parts = [
        "<p><a href='/'>Back to the investigator queue</a></p>",
        "<h1>SynthAML Research Benchmark</h1>",
        "<p>This read-only view shows the completed supervised ranking of published research alerts. "
        "It is not the investigator queue. The business objective is to see whether a supervised model "
        "identifies more report-worthy alerts than unprioritized date order when both review the same number of alerts.</p>",
        _research_boundary(),
        _selection_section(selection),
        "<h2>Population</h2>",
        "<ul>"
        f"<li>Published research alerts: {_n(exhibit.alerts)}</li>"
        f"<li>Training alerts, 2020: {_n(exhibit.train_alerts)} "
        f"({_n(exhibit.train_reports)} Report, {_n(exhibit.train_dismissals)} Dismiss)</li>"
        f"<li>Held-out alerts, 2021: {_n(exhibit.test_alerts)} "
        f"({_n(exhibit.test_reports)} Report, {_n(exhibit.test_dismissals)} Dismiss)</li>"
        f"<li>Report outcomes: {_n(exhibit.reports)}. Dismiss outcomes: {_n(exhibit.dismissals)}.</li>"
        "</ul>",
        f"<h2>{model_heading}</h2>",
        "<ul>"
        f"<li>Experiment {_h(exhibit.experiment_id)}</li>"
        f"<li>Model {_h(exhibit.model_name)} version {_h(exhibit.model_version)}, "
        f"{_h(exhibit.algorithm_family)}</li>"
        f"<li>scikit-learn {_h(exhibit.sklearn_version)}, seed {_h(exhibit.random_state)}, "
        f"iterations {_h(exhibit.n_iter)}, converged: {'yes' if exhibit.converged else 'no'}</li>"
        f"<li>{_h(exhibit.score_semantics)}</li>"
        "</ul>",
        "<h2>Date order and supervised ranking</h2>",
        "<p>Both policies review the same held-out alerts. Capacity was fixed at 5, 10, and 20 percent before scoring. "
        "Date order uses the published date, then AlertID. The supervised order uses the saved Report score.</p>",
        "<table><thead><tr>"
        "<th>Capacity</th><th>Reviewed</th><th>Date-order reports</th><th>Supervised reports</th>"
        "<th>Date-order precision</th><th>Supervised precision</th>"
        "<th>Date-order recall</th><th>Supervised recall</th>"
        "<th>Report outcomes remaining, date order</th>"
        "<th>Report outcomes remaining, supervised</th>"
        "</tr></thead><tbody>",
    ]
    for item in exhibit.comparisons:
        parts.append(
            "<tr>"
            f"<td>{item.percent}%</td>"
            f"<td>{_n(item.alerts_reviewed)}</td>"
            f"<td>{_n(item.date_reports)}</td>"
            f"<td>{_n(item.supervised_reports)}</td>"
            f"<td>{item.date_precision:.6f}</td>"
            f"<td>{item.supervised_precision:.6f}</td>"
            f"<td>{item.date_recall:.6f}</td>"
            f"<td>{item.supervised_recall:.6f}</td>"
            f"<td>{_n(item.date_remaining)}</td>"
            f"<td>{_n(item.supervised_remaining)}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    parts.append("<h2>Highest saved held-out ranks</h2>")
    if not exhibit.sample:
        parts.append("<p>The artifact has no held-out score rows. None were invented.</p>")
    else:
        parts.append(
            "<p>These rows are the first held-out alerts in the saved supervised order. "
            "The outcome is the published research label. It is not an investigator finding.</p>"
            "<table><thead><tr><th>Rank</th><th>AlertID</th><th>Predicted Report score</th>"
            "<th>Published research outcome</th></tr></thead><tbody>"
        )
        for row in exhibit.sample:
            parts.append(
                "<tr>"
                f"<td>{_n(row.model_position)}</td>"
                f"<td>{_n(row.alert_id)}</td>"
                f"<td>{row.score:.6f}</td>"
                f"<td>{_h(row.outcome)}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
    if exhibit.limitations:
        parts.append("<h2>Recorded experiment limitations</h2><ul>")
        for item in exhibit.limitations:
            parts.append(f"<li>{_h(item)}</li>")
        parts.append("</ul>")
    return "".join(parts)


def _render_detail(detail: AlertDetail, error: str) -> str:
    summary = detail.summary
    parts = [
        f"<p><a href='/'>Back to alerts</a></p>",
        f"<h1>{_h(summary.alert_reference)}</h1>",
    ]
    if error:
        parts.append(f"<p><strong>{_h(error)}</strong></p>")
    parts.append(
        "<p>An assessment says whether the alert was useful to review. "
        "It does not establish that a crime occurred, and it is not used for model training. "
        "The operational outcome is a separate case field.</p>"
    )
    parts.append(
        "<h2>Alert</h2><ul>"
        f"<li>Rule {_h(summary.rule_code)} version {summary.rule_version}, severity {_h(summary.severity)}</li>"
        f"<li>Triggered {_h(summary.triggered_at.isoformat())}</li>"
        f"<li>Window {_h(_span(detail.window_started_at, detail.window_ended_at))}</li>"
        f"<li>Customer {_h(summary.customer_reference)}, {_h(detail.customer_name)}</li>"
        f"<li>Account {_h(summary.account_reference or '')}</li>"
        f"<li>{_h(detail.alert_summary or '')}</li>"
        "</ul>"
    )
    parts.append("<h2>Linked payments</h2>")
    parts.append(_payment_table(detail))
    parts.append("<h2>Incoming and outgoing totals</h2>")
    parts.append(_direction_totals(detail))
    parts.append("<h2>Feature values</h2>")
    if detail.features:
        parts.append("<table><thead><tr><th>Feature</th><th>Value</th></tr></thead><tbody>")
        for feature in detail.features:
            parts.append(f"<tr><td>{_h(feature.name)}</td><td>{_h(feature.value)}</td></tr>")
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No feature snapshot is stored for this alert.</p>")
    parts.append(_priority_section(detail))
    parts.append("<h2>Investigation</h2>")
    parts.extend(_case_section(detail))
    return "".join(parts)


def _priority_section(detail: AlertDetail) -> str:
    isolation = next((item for item in detail.scores if item.model_name == ISOLATION_MODEL_NAME), None)
    baseline = next((item for item in detail.scores if item.model_name == BASELINE_MODEL_NAME), None)
    parts = [
        "<h2>Provisional priority</h2>",
        "<p>An anomaly rank is an experimental ordering. It says which alerts look more unusual than the alerts used to fit the model. "
        "It is not a probability of financial crime, a productive investigation, or a false positive.</p>",
    ]
    if isolation is None:
        parts.append("<p>No anomaly ranking is stored for this alert.</p>")
        return "".join(parts)
    explanation = isolation.explanation
    parts.append(
        "<ul>"
        f"<li>Model {_h(isolation.model_name)} version {_h(isolation.version_label)}, "
        f"status {_h(isolation.lifecycle_status)}, use {_h(isolation.score_use)}</li>"
        f"<li>Anomaly rank {_h(explanation.get('rank', ''))} of {_h(explanation.get('alerts_scored', ''))}. "
        f"Score {_h(isolation.score)}. {_h(isolation.score_semantics)}</li>"
        f"<li>Sample role {_h(explanation.get('sample_role', ''))}. "
        "A training-account score repeats alerts the model has already seen.</li>"
        "</ul>"
    )
    if baseline is not None:
        parts.append(
            f"<p>Non-ML baseline rank {_h(baseline.explanation.get('rank', ''))}, "
            f"score {_h(baseline.score)}. The baseline is a robust z-score average for comparison.</p>"
        )
    features = explanation.get("top_features") or []
    if features:
        parts.append(
            "<p>Largest deviations from the typical training alert, as a robust z-score. "
            "A deviation is a distance from that typical value. It is not a cause of the rank, "
            "not a cause of the alert, and not evidence of a crime or a false positive.</p><ul>"
        )
        for item in features:
            parts.append(f"<li>{_h(item.get('name', ''))}: robust z {_h(item.get('robust_z', ''))}</li>")
        parts.append("</ul>")
    return "".join(parts)


def _case_section(detail: AlertDetail) -> list[str]:
    alert_id = str(detail.summary.alert_id)
    parts: list[str] = []
    if detail.case is None:
        parts.append("<p>No investigation is open.</p>")
        parts.append(
            "<form method='post' action='/open'>"
            f"<input type='hidden' name='alert_id' value='{_h(alert_id)}'>"
            "<label>Reviewer <input name='reviewer' required></label> "
            "<button type='submit'>Open investigation</button></form>"
        )
        return parts
    case = detail.case
    parts.append(
        "<ul>"
        f"<li>Reference {_h(case.case_reference)}</li>"
        f"<li>Status {_h(case.lifecycle_status)}</li>"
        f"<li>Operational outcome {_h(case.disposition or 'not closed')}</li>"
        f"<li>Assigned {_h(case.assigned_to or '')}</li>"
        f"<li>{_h(case.status_summary or '')}</li>"
        "</ul>"
    )
    if case.notes:
        parts.append("<h3>Notes</h3><ul>")
        for note in case.notes:
            parts.append(
                f"<li>{_h(note.noted_at.isoformat())} by {_h(note.author_reference)}: {_h(note.note_text)}</li>"
            )
        parts.append("</ul>")
    if detail.assessments:
        parts.append("<h3>Usefulness assessments</h3><ul>")
        for item in detail.assessments:
            training = "eligible for training" if item.eligible_for_evaluation else "not eligible for training"
            parts.append(
                "<li>"
                f"{_h(item.labeled_at.isoformat())} {_h(item.label_value)} by {_h(item.labeled_by)}, "
                f"{_h(item.data_origin)}, {_h(training)}"
                "</li>"
            )
        parts.append("</ul>")
    if case.lifecycle_status == "closed":
        parts.append(
            "<form method='post' action='/reopen'>"
            f"<input type='hidden' name='alert_id' value='{_h(alert_id)}'>"
            "<label>Reviewer <input name='reviewer' required></label> "
            "<label>Reason <input name='rationale' required></label> "
            "<button type='submit'>Reopen</button></form>"
        )
        return parts
    parts.append(
        "<form method='post' action='/finding'>"
        f"<input type='hidden' name='alert_id' value='{_h(alert_id)}'>"
        "<p><label>Reviewer <input name='reviewer' required></label></p>"
        "<p><label>Finding <textarea name='rationale' required></textarea></label></p>"
        "<button type='submit'>Save finding</button></form>"
    )
    parts.append(
        "<form method='post' action='/assessment'>"
        f"<input type='hidden' name='alert_id' value='{_h(alert_id)}'>"
        "<p><label>Reviewer <input name='reviewer' required></label></p>"
        "<p><label>Was the alert useful to review? <select name='usefulness'>"
        "<option value='productive'>Productive</option>"
        "<option value='non_productive'>Non-productive</option>"
        "<option value='inconclusive'>Inconclusive</option>"
        "</select></label></p>"
        "<p><label>Rationale <textarea name='rationale' required></textarea></label></p>"
        "<button type='submit'>Save assessment</button></form>"
    )
    parts.append(
        "<form method='post' action='/close'>"
        f"<input type='hidden' name='alert_id' value='{_h(alert_id)}'>"
        "<p><label>Reviewer <input name='reviewer' required></label></p>"
        "<p><label>Operational outcome <select name='disposition'>"
        + "".join(f"<option value='{_h(item)}'>{_h(item)}</option>" for item in DISPOSITIONS)
        + "</select></label></p>"
        "<p><label>Reason <textarea name='rationale' required></textarea></label></p>"
        "<button type='submit'>Close case</button></form>"
    )
    return parts


def _payment_table(detail: AlertDetail) -> str:
    if not detail.payments:
        return "<p>No linked payments on this account.</p>"
    rows = [
        "<table><thead><tr><th>Role</th><th>Direction</th><th>Amount</th><th>Currency</th>"
        "<th>Channel</th><th>When</th><th>Counterparty</th></tr></thead><tbody>"
    ]
    for payment in detail.payments:
        rows.append(
            "<tr>"
            f"<td>{_h(payment.role)}</td>"
            f"<td>{_h(payment.direction)}</td>"
            f"<td>{_h(format(payment.amount, 'f'))}</td>"
            f"<td>{_h(payment.currency_code)}</td>"
            f"<td>{_h(payment.payment_channel)}</td>"
            f"<td>{_h(payment.event_time.isoformat())}</td>"
            f"<td>{_h(payment.counterparty_name or '')}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _direction_totals(detail: AlertDetail) -> str:
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for payment in detail.payments:
        key = (payment.direction, payment.currency_code)
        totals[key] += payment.amount
        counts[key] += 1
    if not totals:
        return "<p>No linked amounts.</p>"
    rows = ["<table><thead><tr><th>Direction</th><th>Currency</th><th>Count</th><th>Total</th></tr></thead><tbody>"]
    for direction, currency in sorted(totals):
        rows.append(
            "<tr>"
            f"<td>{_h(direction)}</td><td>{_h(currency)}</td>"
            f"<td>{counts[(direction, currency)]}</td>"
            f"<td>{_h(format(totals[(direction, currency)], 'f'))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _page(body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>AMLOG review</title>"
        "<style>body{font-family:sans-serif;margin:1.5rem;max-width:76rem;background:#fff;color:#111}"
        "a{color:#0645ad}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ccc;text-align:left;padding:.3rem;color:#111}"
        "textarea{width:100%;min-height:4rem}</style></head><body>"
        f"{body}</body></html>"
    )


def _options(values: tuple[str, ...], selected: str) -> str:
    return "".join(
        f"<option value='{_h(value)}'{' selected' if value == selected else ''}>{_h(value)}</option>"
        for value in values
    )


def _query(rule: str, state: str, page: int, sort: str) -> str:
    return urlencode({"rule": rule, "state": state, "page": page, "sort": sort})


def _span(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return ""
    return f"{start.isoformat()} to {end.isoformat()}"


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ReviewError("The alert identifier is not valid.") from exc


def _h(value: object) -> str:
    return html.escape(str(value), quote=True)


def _n(value: int) -> str:
    return f"{value:,}"
