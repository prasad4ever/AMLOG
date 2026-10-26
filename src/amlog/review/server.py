"""Minimal local review pages.

The server binds to 127.0.0.1. It is not authentication and it is not a
second product surface. Later ranking and explanation can be added to
these same pages.
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

PAGE_SIZE = 40


class ReviewServer(ThreadingHTTPServer):
    """One local review session bound to a database URL."""

    def __init__(self, address: tuple[str, int], database_url: str) -> None:
        self.database_url = database_url
        super().__init__(address, ReviewHandler)


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def do_GET(self) -> None:
        connection = None
        try:
            connection = self._connect()
            parsed = urlparse(self.path)
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
        "<style>body{font-family:sans-serif;margin:1.5rem;max-width:60rem}"
        "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ccc;text-align:left;padding:.3rem}"
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
