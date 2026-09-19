"""Regression tests: ambiguous UID groups (duplicate master events) → 422.

A recurring series is defined by exactly one *master* VEVENT (one without
``RECURRENCE-ID``) plus any number of ``RECURRENCE-ID`` overrides. When two
or more masters share a ``UID`` the shared recurrence kernel cannot tell
which one defines the series, so the grouping stage
(:func:`app.recurrence.group_events_by_uid`) rejects the whole calendar
with ``ExpansionError`` — before any expansion or provenance projection
runs — and every endpoint built on the kernel answers 422 with a message
naming the UID and the number of masters found.

These tests lock that contract:

* ``/expand`` and ``/provenance`` reject the ambiguity with identical
  details (both project the same kernel);
* every endpoint reusing the expander (agenda, alarms, attendance,
  availability, conflicts, free/busy export, calendar diff, fair
  rotation) rejects the same calendar the same way;
* a legal single master plus overrides is unaffected, several overrides
  for one ``RECURRENCE-ID`` still resolve last-wins (they are overrides,
  not masters), and an override-only UID (zero masters) still expands.

Run from the repository root:  python -m pytest tests/test_duplicate_master.py -v
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar

from app.main import app
from app.recurrence import (
    ExpansionError,
    compute_candidates,
    group_events_by_uid,
)

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//duplicate-master//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"

JAN = ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
UTC = dt.timezone.utc


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


# A UID with two masters: one recurring, one single. The kernel must not
# silently pick the first — the grouping stage rejects the calendar.
DUP_ICS = cal(
    event(
        "UID:dup-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "SUMMARY:first master",
    ),
    event(
        "UID:dup-1",
        "DTSTART:20260106T120000Z",
        "DTEND:20260106T130000Z",
        "SUMMARY:second master",
    ),
)

DUP_DETAIL = (
    "UID 'dup-1' has 2 VEVENTs without RECURRENCE-ID; "
    "a recurring series may have only one master event"
)


def expand(ics, window_start=JAN[0], window_end=JAN[1]):
    return client.post(
        "/expand",
        json={"ics": ics, "window_start": window_start, "window_end": window_end},
    )


def provenance(ics, uid, window_start=JAN[0], window_end=JAN[1]):
    return client.post(
        "/provenance",
        json={
            "ics": ics,
            "uid": uid,
            "window_start": window_start,
            "window_end": window_end,
        },
    )


# ---------------------------------------------------------------------------
# /expand and /provenance: the shared kernel rejects duplicate masters
# ---------------------------------------------------------------------------


def test_expand_rejects_duplicate_masters_with_uid_and_count():
    resp = expand(DUP_ICS)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail == DUP_DETAIL
    assert "dup-1" in detail  # the UID is named
    assert "2 VEVENTs without RECURRENCE-ID" in detail  # the master count


def test_provenance_rejects_duplicate_masters_identically():
    exp = expand(DUP_ICS)
    prov = provenance(DUP_ICS, "dup-1")
    assert exp.status_code == 422
    assert prov.status_code == 422
    assert prov.json()["detail"] == exp.json()["detail"]


def test_provenance_rejects_ambiguous_calendar_even_for_a_clean_uid():
    # The grouping stage validates the whole calendar before the requested
    # series is traced — exactly like a VEVENT missing its UID — so a
    # duplicate master under *any* UID fails the request.
    ics = cal(
        event(
            "UID:clean-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
        ),
        event("UID:dup-1", "DTSTART:20260106T090000Z", "DTEND:20260106T100000Z"),
        event("UID:dup-1", "DTSTART:20260107T090000Z", "DTEND:20260107T100000Z"),
    )
    prov = provenance(ics, "clean-1")
    assert prov.status_code == 422
    assert "dup-1" in prov.json()["detail"]


def test_master_count_in_message_follows_the_actual_number():
    ics = cal(
        event("UID:trio", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:trio", "DTSTART:20260106T090000Z", "DTEND:20260106T100000Z"),
        event("UID:trio", "DTSTART:20260107T090000Z", "DTEND:20260107T100000Z"),
    )
    resp = expand(ics)
    assert resp.status_code == 422
    assert "UID 'trio' has 3 VEVENTs without RECURRENCE-ID" in resp.json()["detail"]


def test_two_non_recurring_events_sharing_a_uid_are_duplicate_masters():
    # No RRULE anywhere: two single VEVENTs with one UID are still two
    # masters for one series.
    ics = cal(
        event("UID:single", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"),
        event("UID:single", "DTSTART:20260106T090000Z", "DTEND:20260106T100000Z"),
    )
    resp = expand(ics)
    assert resp.status_code == 422
    assert "UID 'single' has 2 VEVENTs without RECURRENCE-ID" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Legal shapes that must keep working (no false positives)
# ---------------------------------------------------------------------------


def test_single_master_with_overrides_is_unaffected():
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T150000Z",
            "DTEND:20260106T160000Z",
            "SUMMARY:moved",
        ),
    )
    resp = expand(ics)
    assert resp.status_code == 200
    assert [i["start"] for i in resp.json()["instances"]] == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T15:00:00Z",
        "2026-01-07T09:00:00Z",
    ]
    prov = provenance(ics, "evt-1")
    assert prov.status_code == 200
    assert prov.json()["count"] == 3


def test_duplicate_override_same_recurrence_id_is_not_a_duplicate_master():
    # Two overrides for one RECURRENCE-ID: the last component wins. They
    # are overrides, not masters, so the grouping stage must not complain.
    ics = cal(
        event(
            "UID:evt-1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T150000Z",
            "DTEND:20260106T160000Z",
            "SUMMARY:first override",
        ),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T170000Z",
            "DTEND:20260106T180000Z",
            "SUMMARY:last override wins",
        ),
    )
    resp = expand(ics)
    assert resp.status_code == 200
    instances = resp.json()["instances"]
    assert [i["start"] for i in instances] == [
        "2026-01-05T09:00:00Z",
        "2026-01-06T17:00:00Z",  # the last override for the Jan 6 slot
        "2026-01-07T09:00:00Z",
    ]
    assert instances[1]["summary"] == "last override wins"
    assert instances[1]["recurrence_id"] == "2026-01-06T09:00:00Z"


def test_override_only_uid_has_no_master_and_still_expands():
    # Zero masters is legal: an orphan override produces its own instance.
    ics = cal(event(
        "UID:orphan-1",
        "RECURRENCE-ID:20260110T090000Z",
        "DTSTART:20260110T120000Z",
        "DTEND:20260110T130000Z",
    ))
    resp = expand(ics)
    assert resp.status_code == 200
    assert [i["start"] for i in resp.json()["instances"]] == ["2026-01-10T12:00:00Z"]


# ---------------------------------------------------------------------------
# Every endpoint reusing the expansion kernel rejects the same calendar
# ---------------------------------------------------------------------------


def test_agenda_rejects_duplicate_masters():
    resp = client.post(
        "/agenda",
        json={
            "ics": DUP_ICS,
            "window_start": JAN[0],
            "window_end": JAN[1],
            "timezone": "UTC",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == DUP_DETAIL


def test_alarms_rejects_duplicate_masters():
    resp = client.post(
        "/alarms",
        json={"ics": DUP_ICS, "window_start": JAN[0], "window_end": JAN[1]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == DUP_DETAIL


def test_attendance_rejects_duplicate_masters():
    resp = client.post(
        "/attendance",
        json={"ics": DUP_ICS, "window_start": JAN[0], "window_end": JAN[1]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == DUP_DETAIL


def test_availability_rejects_duplicate_masters():
    resp = client.post(
        "/availability",
        json={
            "calendars": {"alice": DUP_ICS},
            "window_start": JAN[0],
            "window_end": JAN[1],
            "timezone": "UTC",
            "working_hours": {"monday": [{"start": "09:00", "end": "17:00"}]},
            "minimum_minutes": 30,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == DUP_DETAIL


def test_conflicts_rejects_duplicate_masters():
    resp = client.post(
        "/conflicts",
        json={
            "calendars": {"alice": DUP_ICS},
            "window_start": JAN[0],
            "window_end": JAN[1],
            "minimum_concurrency": 2,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == DUP_DETAIL


def test_freebusy_export_rejects_duplicate_masters():
    resp = client.post(
        "/freebusy-export",
        json={
            "calendars": [
                {"calendar_id": "alice", "ics": DUP_ICS, "timezone": "UTC"}
            ],
            "window_start": JAN[0],
            "window_end": JAN[1],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == DUP_DETAIL


def test_calendar_diff_rejects_duplicate_masters_on_either_side():
    ok_ics = cal(event(
        "UID:evt-1", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"
    ))
    for old_ics, new_ics in ((DUP_ICS, ok_ics), (ok_ics, DUP_ICS)):
        resp = client.post(
            "/calendar-diff",
            json={
                "old_ics": old_ics,
                "new_ics": new_ics,
                "window_start": JAN[0],
                "window_end": JAN[1],
            },
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == DUP_DETAIL


def test_fair_rotation_rejects_duplicate_masters_in_a_participant_calendar():
    resp = client.post(
        "/fair-rotation",
        json={
            "organizer_timezone": "UTC",
            "dates": ["2026-01-05"],
            "candidate_times": ["09:00"],
            "duration_minutes": 60,
            "participants": [
                {"participant_id": "p1", "timezone": "UTC", "ics": DUP_ICS}
            ],
        },
    )
    assert resp.status_code == 422
    # The rotation endpoint prefixes participant context; the kernel
    # message with the UID and the master count is preserved.
    assert DUP_DETAIL in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Kernel level: the grouping stage and the candidate pipeline both refuse
# ---------------------------------------------------------------------------


def test_grouping_stage_raises_before_any_projection():
    calendar = Calendar.from_ical(DUP_ICS)
    with pytest.raises(ExpansionError, match=r"UID 'dup-1' has 2 VEVENTs"):
        group_events_by_uid(calendar)


def test_compute_candidates_never_silently_picks_a_master():
    # Direct kernel callers (bypassing group_events_by_uid) get the same
    # refusal instead of a silently chosen first master.
    calendar = Calendar.from_ical(DUP_ICS)
    events = list(calendar.walk("VEVENT"))
    with pytest.raises(ExpansionError, match=r"UID 'dup-1' has 2 VEVENTs"):
        compute_candidates("dup-1", events, dt.datetime(2026, 1, 1, tzinfo=UTC),
                           dt.datetime(2026, 2, 1, tzinfo=UTC))
