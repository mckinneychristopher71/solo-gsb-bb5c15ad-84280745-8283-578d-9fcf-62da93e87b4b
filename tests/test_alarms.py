"""Key tests for the /alarms endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//alarms-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def alarm(*lines: str) -> str:
    return "BEGIN:VALARM\r\n" + "".join(line + "\r\n" for line in lines) + "END:VALARM\r\n"


def alarms(ics: str, window_start="2026-01-01T00:00:00Z", window_end="2026-02-01T00:00:00Z"):
    return client.post(
        "/alarms",
        json={"ics": ics, "window_start": window_start, "window_end": window_end},
    )


def rows(response):
    return [
        (a["trigger"], a["uid"], a["recurrence_id"], a["description"])
        for a in response.json()["alarms"]
    ]


DISPLAY = ("ACTION:DISPLAY", "DESCRIPTION:Reminder")


# ---------------------------------------------------------------------------
# Basic triggers
# ---------------------------------------------------------------------------

def test_relative_trigger_before_dtstart():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:standup",
        alarm("TRIGGER:-PT15M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    item = resp.json()["alarms"][0]
    assert item == {
        "uid": "evt-1",
        "recurrence_id": None,
        "summary": "standup",
        "trigger": "2026-01-05T08:45:00Z",
        "description": "Reminder",
    }


def test_relative_trigger_anchored_to_dtend_positive_delta():
    # +10 minutes after the end.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;RELATED=END:PT10M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T10:10:00Z", "evt-1", None, "Reminder")]


def test_relative_trigger_before_dtend():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;RELATED=END:-PT1H", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T09:00:00Z", "evt-1", None, "Reminder")]


def test_absolute_trigger_utc():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;VALUE=DATE-TIME:20260105T073000Z", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T07:30:00Z", "evt-1", None, "Reminder")]


def test_absolute_trigger_with_tzid_is_converted_to_utc():
    # 08:00 America/New_York on 2026-01-05 is EST (UTC-5) = 13:00Z.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART;TZID=America/New_York:20260105T090000",
        "DTEND;TZID=America/New_York:20260105T100000",
        alarm(
            "TRIGGER;VALUE=DATE-TIME;TZID=America/New_York:20260105T080000",
            *DISPLAY,
        ),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T13:00:00Z", "evt-1", None, "Reminder")]


def test_floating_absolute_trigger_is_assumed_utc():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;VALUE=DATE-TIME:20260105T080000", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T08:00:00Z", "evt-1", None, "Reminder")]


# ---------------------------------------------------------------------------
# REPEAT / DURATION
# ---------------------------------------------------------------------------

def test_repeat_generates_spaced_reminders():
    # Initial trigger at -15m, then two more every 5 minutes: 08:45, 08:50,
    # 08:55 before a 09:00 start.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT15M", "REPEAT:2", "DURATION:PT5M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:45:00Z",
        "2026-01-05T08:50:00Z",
        "2026-01-05T08:55:00Z",
    ]


def test_repeat_zero_still_fires_once():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT15M", "REPEAT:0", "DURATION:PT5M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:45:00Z",
    ]


def test_repeat_works_with_absolute_trigger():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm(
            "TRIGGER;VALUE=DATE-TIME:20260105T080000Z",
            "REPEAT:2",
            "DURATION:PT10M",
            *DISPLAY,
        ),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:00:00Z",
        "2026-01-05T08:10:00Z",
        "2026-01-05T08:20:00Z",
    ]


# ---------------------------------------------------------------------------
# Recurring events and overrides
# ---------------------------------------------------------------------------

def weekly_master(extra: str = "") -> str:
    return event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",  # Mondays Jan 5/12/19
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        "SUMMARY:weekly sync",
        alarm("TRIGGER:-PT1H", *DISPLAY),
        extra,
    )


def test_master_alarm_applies_to_every_expanded_instance():
    ics = cal(weekly_master())
    resp = alarms(ics)
    assert rows(resp) == [
        ("2026-01-05T08:00:00Z", "evt-1", "2026-01-05T09:00:00Z", "Reminder"),
        ("2026-01-12T08:00:00Z", "evt-1", "2026-01-12T09:00:00Z", "Reminder"),
        ("2026-01-19T08:00:00Z", "evt-1", "2026-01-19T09:00:00Z", "Reminder"),
    ]


def test_override_uses_its_own_alarm_not_the_masters():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
            "SUMMARY:weekly sync (moved)",
            alarm("TRIGGER:-PT5M", "ACTION:DISPLAY", "DESCRIPTION:Override ping"),
        ),
    )
    resp = alarms(ics)
    assert rows(resp) == [
        ("2026-01-05T08:00:00Z", "evt-1", "2026-01-05T09:00:00Z", "Reminder"),
        ("2026-01-12T10:55:00Z", "evt-1", "2026-01-12T09:00:00Z", "Override ping"),
        ("2026-01-19T08:00:00Z", "evt-1", "2026-01-19T09:00:00Z", "Reminder"),
    ]


def test_override_without_valarm_alarm_nobody():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T110000Z",
            "DTEND:20260112T120000Z",
        ),
    )
    resp = alarms(ics)
    rids = [a["recurrence_id"] for a in resp.json()["alarms"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_cancelled_override_produces_no_reminders():
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260112T090000Z",
            "DTEND:20260112T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = alarms(ics)
    rids = [a["recurrence_id"] for a in resp.json()["alarms"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_exdated_occurrence_has_no_reminder():
    ics = cal(weekly_master("EXDATE:20260112T090000Z"))
    resp = alarms(ics)
    rids = [a["recurrence_id"] for a in resp.json()["alarms"]]
    assert rids == ["2026-01-05T09:00:00Z", "2026-01-19T09:00:00Z"]


def test_non_recurring_event_alarm_has_null_recurrence_id():
    ics = cal(event(
        "UID:oneoff",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT1H", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.json()["alarms"][0]["recurrence_id"] is None


# ---------------------------------------------------------------------------
# Cross-window reminders: the trigger lands inside the window although the
# instance itself does not overlap it
# ---------------------------------------------------------------------------

def test_reminder_before_event_beyond_window_end():
    # The event sits entirely after the window, but its 2-day-ahead
    # reminder fires inside it.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260202T090000Z",
        "DTEND:20260202T100000Z",
        alarm("TRIGGER:-P2D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-31T09:00:00Z", "evt-1", None, "Reminder")]


def test_positive_trigger_after_event_before_window_start():
    # The event ended before the window; the follow-up reminder anchored
    # to DTEND fires inside it.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20251228T090000Z",
        "DTEND:20251228T100000Z",
        alarm("TRIGGER;RELATED=END:P5D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-02T10:00:00Z", "evt-1", None, "Reminder")]


def test_positive_trigger_relative_to_start_before_window():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20251230T090000Z",
        "DTEND:20251230T100000Z",
        alarm("TRIGGER:P3D", *DISPLAY),  # 3 days after the start
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-02T09:00:00Z", "evt-1", None, "Reminder")]


def test_end_anchored_reminder_before_event_start_beyond_window():
    # A 1-hour event just past the window with a reminder 2 hours before
    # its end fires 1 hour before its start — inside the window.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260201T003000Z",
        "DTEND:20260201T013000Z",
        alarm("TRIGGER;RELATED=END:-PT2H", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-31T23:30:00Z", "evt-1", None, "Reminder")]


def test_recurring_instance_beyond_window_end_reminder_inside():
    # Weekly Mondays 09:00 with a 7-day-ahead reminder: every instance's
    # reminder lands on the previous instance's slot. The Jan 26 instance
    # is outside the window but its reminder (Jan 19) is inside.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=4",  # Jan 5/12/19/26
        alarm("TRIGGER:-P7D", *DISPLAY),
    ))
    resp = alarms(
        ics,
        window_start="2026-01-12T00:00:00Z",
        window_end="2026-01-26T00:00:00Z",
    )
    assert rows(resp) == [
        ("2026-01-12T09:00:00Z", "evt-1", "2026-01-19T09:00:00Z", "Reminder"),
        ("2026-01-19T09:00:00Z", "evt-1", "2026-01-26T09:00:00Z", "Reminder"),
    ]


def test_repeat_fanout_reaches_into_window_from_before():
    # The event ends just before the window; only the later repeats of its
    # post-event alarm land inside it.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20251231T220000Z",
        "DTEND:20251231T230000Z",
        # fires 23:00, 23:30, then 2026-01-01 00:00 and 00:30
        alarm("TRIGGER:PT1H", "REPEAT:3", "DURATION:PT30M", *DISPLAY),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:30:00Z",
    ]


def test_repeat_fanout_from_instance_beyond_window_end():
    # Event after the window; only the first of its repeated ahead-of-time
    # reminders fires inside the window.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260201T030000Z",
        "DTEND:20260201T040000Z",
        # fires 2026-01-31 23:00, then 2026-02-01 00:00 and 01:00
        alarm("TRIGGER:-PT4H", "REPEAT:2", "DURATION:PT1H", *DISPLAY),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-31T23:00:00Z",
    ]


def test_override_moved_out_of_window_keeps_its_reminder_inside():
    # The override moves the Jan 12 instance to Feb 2 (outside the window)
    # with its own 3-day-ahead alarm, which fires inside the window.
    ics = cal(
        weekly_master(),
        event(
            "UID:evt-1",
            "RECURRENCE-ID:20260112T090000Z",
            "DTSTART:20260202T090000Z",
            "DTEND:20260202T100000Z",
            alarm("TRIGGER:-P3D", "ACTION:DISPLAY", "DESCRIPTION:Override ping"),
        ),
    )
    resp = alarms(ics)
    assert rows(resp) == [
        ("2026-01-05T08:00:00Z", "evt-1", "2026-01-05T09:00:00Z", "Reminder"),
        ("2026-01-19T08:00:00Z", "evt-1", "2026-01-19T09:00:00Z", "Reminder"),
        ("2026-01-30T09:00:00Z", "evt-1", "2026-01-12T09:00:00Z", "Override ping"),
    ]


def test_exdated_instance_beyond_window_still_fires_no_reminder():
    # The Jan 19 instance is EXDATEd; its 7-day-ahead reminder would have
    # fired inside the window (Jan 12) and must stay silent. The Jan 26
    # instance is outside the window and its reminder (Jan 19) is inside.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=4",  # Jan 5/12/19/26
        "EXDATE:20260119T090000Z",
        alarm("TRIGGER:-P7D", *DISPLAY),
    ))
    resp = alarms(
        ics,
        window_start="2026-01-10T00:00:00Z",
        window_end="2026-01-20T00:00:00Z",
    )
    assert rows(resp) == [
        ("2026-01-19T09:00:00Z", "evt-1", "2026-01-26T09:00:00Z", "Reminder"),
    ]


def test_absolute_trigger_of_instance_outside_window_stays_silent():
    # Absolute triggers are filtered by their trigger time only; they are
    # not back-projected onto instances outside the window.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260202T090000Z",
        "DTEND:20260202T100000Z",
        alarm("TRIGGER;VALUE=DATE-TIME:20260115T090000Z", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.json()["count"] == 0
    assert resp.json()["alarms"] == []


# ---------------------------------------------------------------------------
# Window filtering (half-open)
# ---------------------------------------------------------------------------

def test_only_triggers_inside_window_are_returned():
    # Event starts inside the window; reminders straddle both edges.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm(
            "TRIGGER;VALUE=DATE-TIME:20260105T070000Z",
            "REPEAT:4",
            "DURATION:PT1H",
            *DISPLAY,
        ),  # 07:00, 08:00, 09:00, 10:00, 11:00
    ))
    resp = alarms(
        ics,
        window_start="2026-01-05T08:00:00Z",
        window_end="2026-01-05T11:00:00Z",
    )
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:00:00Z",  # == window_start: included
        "2026-01-05T09:00:00Z",
        "2026-01-05T10:00:00Z",
        # 11:00 == window_end: excluded
    ]


def test_event_with_alarms_all_outside_window_returns_nothing():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT1H", *DISPLAY),
    ))
    resp = alarms(
        ics,
        window_start="2026-03-01T00:00:00Z",
        window_end="2026-04-01T00:00:00Z",
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "window_start": "2026-03-01T00:00:00Z",
        "window_end": "2026-04-01T00:00:00Z",
        "count": 0,
        "alarms": [],
    }


def test_calendar_without_alarms_returns_empty_list():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "SUMMARY:no alarm here",
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["alarms"] == []


def test_multiple_alarms_on_one_instance_all_returned():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT1H", "ACTION:DISPLAY", "DESCRIPTION:one hour before"),
        alarm("TRIGGER:-PT5M", "ACTION:DISPLAY", "DESCRIPTION:five minutes before"),
    ))
    resp = alarms(ics)
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:00:00Z",
        "2026-01-05T08:55:00Z",
    ]


# ---------------------------------------------------------------------------
# Sorting: trigger time, then UID, then RECURRENCE-ID
# ---------------------------------------------------------------------------

def test_sorted_by_trigger_then_uid():
    ics = cal(
        event(
            "UID:b-event",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            alarm("TRIGGER:-PT1H", *DISPLAY),
        ),
        event(
            "UID:a-event",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            alarm("TRIGGER:-PT1H", *DISPLAY),
        ),
        event(
            "UID:c-event",
            "DTSTART:20260104T090000Z",
            "DTEND:20260104T100000Z",
            alarm("TRIGGER:-PT1H", *DISPLAY),
        ),
    )
    resp = alarms(ics)
    got = [(a["trigger"], a["uid"]) for a in resp.json()["alarms"]]
    assert got == [
        ("2026-01-04T08:00:00Z", "c-event"),
        ("2026-01-05T09:00:00Z", "a-event"),
        ("2026-01-05T09:00:00Z", "b-event"),
    ]


def test_same_trigger_sorted_by_recurrence_id():
    # Two recurring series with an identical trigger instant; the UID then
    # RECURRENCE-ID tiebreak keeps the order stable.
    ics = cal(
        event(
            "UID:z-series",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
            alarm("TRIGGER:-PT1H", *DISPLAY),
        ),
        event(
            "UID:a-series",
            "DTSTART:20260105T100000Z",
            "DTEND:20260105T110000Z",
            "RRULE:FREQ=DAILY;COUNT=2",
            alarm("TRIGGER:-PT2H", *DISPLAY),
        ),
    )
    resp = alarms(ics)
    got = [(a["trigger"], a["uid"], a["recurrence_id"]) for a in resp.json()["alarms"]]
    assert got == [
        ("2026-01-05T08:00:00Z", "a-series", "2026-01-05T10:00:00Z"),
        ("2026-01-05T08:00:00Z", "z-series", "2026-01-05T09:00:00Z"),
        ("2026-01-06T08:00:00Z", "a-series", "2026-01-06T10:00:00Z"),
        ("2026-01-06T08:00:00Z", "z-series", "2026-01-06T09:00:00Z"),
    ]


# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------

def post_422(alarm_lines: str, *, event_lines: tuple[str, ...] = ()):
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        *event_lines,
        alarm_lines,
    ))
    resp = alarms(ics)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def test_unsupported_action_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "ACTION:EMAIL"))
    assert "ACTION" in detail and "DISPLAY" in detail


def test_audio_action_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "ACTION:AUDIO"))
    assert "unsupported VALARM ACTION" in detail


def test_missing_action_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "DESCRIPTION:ping"))
    assert "ACTION" in detail


def test_display_without_description_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "ACTION:DISPLAY"))
    assert "DESCRIPTION" in detail


def test_missing_trigger_returns_422():
    detail = post_422(alarm("ACTION:DISPLAY", "DESCRIPTION:ping"))
    assert "TRIGGER" in detail


def test_unsupported_related_value_returns_422():
    detail = post_422(
        alarm("TRIGGER;RELATED=MIDDLE:PT10M", *DISPLAY)
    )
    assert "RELATED" in detail


def test_related_on_absolute_trigger_returns_422():
    detail = post_422(
        alarm("TRIGGER;VALUE=DATE-TIME;RELATED=START:20260105T080000Z", *DISPLAY)
    )
    assert "RELATED" in detail


def test_repeat_without_duration_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "REPEAT:2", *DISPLAY))
    assert "DURATION" in detail


def test_duration_without_repeat_returns_422():
    detail = post_422(alarm("TRIGGER:-PT15M", "DURATION:PT5M", *DISPLAY))
    assert "REPEAT" in detail


def test_negative_repeat_returns_422():
    detail = post_422(
        alarm("TRIGGER:-PT15M", "REPEAT:-1", "DURATION:PT5M", *DISPLAY)
    )
    assert "REPEAT" in detail


def test_non_positive_alarm_duration_returns_422():
    detail = post_422(
        alarm("TRIGGER:-PT15M", "REPEAT:2", "DURATION:PT0S", *DISPLAY)
    )
    assert "DURATION" in detail


def test_excessive_repeat_returns_422():
    detail = post_422(
        alarm("TRIGGER:-PT15M", "REPEAT:1001", "DURATION:PT1S", *DISPLAY)
    )
    assert "1000" in detail


def test_unknown_tzid_on_absolute_trigger_returns_422():
    detail = post_422(
        alarm(
            "TRIGGER;VALUE=DATE-TIME;TZID=Mars/Olympus_Mons:20260105T080000",
            *DISPLAY,
        )
    )
    assert "unknown timezone" in detail
    assert "Mars/Olympus_Mons" in detail


def test_invalid_alarm_outside_window_still_returns_422():
    # Validation is eager for every VALARM in the calendar, even when its
    # event (and therefore its trigger) lies outside the query window.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260305T090000Z",
        "DTEND:20260305T100000Z",
        alarm("TRIGGER:-PT15M", "ACTION:EMAIL"),
    ))
    resp = alarms(
        ics,
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-02-01T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "ACTION" in resp.json()["detail"]


def test_calendar_level_failures_still_return_422():
    # Unparseable ICS and invalid windows reuse the expander's validation.
    resp = alarms("not an iCalendar document")
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]

    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        alarm("TRIGGER:-PT15M", *DISPLAY),
    ))
    resp = alarms(ics, "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Extreme ranges: triggers and fan-outs beyond the representable datetime
# range (years 1-9999) must never overflow into a 500. Out-of-window
# triggers are safely skipped; input that cannot be represented or expanded
# at all is a 422; ordinary alarms are unaffected.
# ---------------------------------------------------------------------------

def test_extreme_negative_trigger_is_safely_skipped():
    # The reminder would fire ~27 000 years before the event: outside every
    # representable window, so it is skipped instead of overflowing.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-P9999999D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["alarms"] == []


def test_extreme_positive_trigger_is_safely_skipped():
    # The reminder would fire ~27 000 years after the event (past year 9999).
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:P9999999D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_extreme_end_anchored_trigger_is_safely_skipped():
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER;RELATED=END:-P9999999D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_extreme_trigger_on_recurring_series_is_safely_skipped():
    # The clamped instance window still expands the bounded series; every
    # reminder lands ~27 000 years before its instance and is skipped.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        alarm("TRIGGER:-P9999999D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_repeat_steps_back_into_representable_range():
    # Fires at anchor - 9999999d (before year 1: skipped), exactly at the
    # anchor (inside the window), and at anchor + 9999999d (past year 9999:
    # skipped) — each REPEAT step is judged on its own.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-P9999999D", "REPEAT:2", "DURATION:P9999999D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert rows(resp) == [("2026-01-05T09:00:00Z", "evt-1", None, "Reminder")]


def test_huge_repeat_fanout_does_not_overflow():
    # 999 repetitions spaced ~27 000 years apart: the first reminder lands
    # in the window, every later one is past year 9999 and is skipped.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        alarm("TRIGGER:-PT15M", "REPEAT:999", "DURATION:P9999999D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "2026-01-05T08:45:00Z",
    ]


def test_repeat_past_datetime_max_is_clipped():
    # The instance and the first trigger sit at the very edge of the
    # representable range; both repetitions fall past year 9999.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:99991231T090000Z",
        "DTEND:99991231T100000Z",
        alarm(
            "TRIGGER;VALUE=DATE-TIME:99991231T230000Z",
            "REPEAT:2",
            "DURATION:PT1H",
            *DISPLAY,
        ),
    ))
    resp = alarms(
        ics,
        window_start="9999-12-31T00:00:00Z",
        window_end="9999-12-31T23:59:59Z",
    )
    assert resp.status_code == 200
    assert [a["trigger"] for a in resp.json()["alarms"]] == [
        "9999-12-31T23:00:00Z",
    ]


def test_large_but_representable_reach_still_fires():
    # 7305 days = 20 years (5 leap days): the reminder of an event 20 years
    # past the window lands exactly on 2026-01-05T09:00Z, inside it.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20460105T090000Z",
        "DTEND:20460105T100000Z",
        alarm("TRIGGER:-P7305D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert rows(resp) == [("2026-01-05T09:00:00Z", "evt-1", None, "Reminder")]


def test_extreme_trigger_does_not_poison_other_events():
    # One event's unrepresentable trigger is skipped while another event's
    # ordinary reminder is still returned.
    ics = cal(
        event(
            "UID:normal",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            alarm("TRIGGER:-PT15M", *DISPLAY),
        ),
        event(
            "UID:extreme",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            alarm("TRIGGER:-P9999999D", *DISPLAY),
        ),
    )
    resp = alarms(ics)
    assert resp.status_code == 200
    assert rows(resp) == [("2026-01-05T08:45:00Z", "normal", None, "Reminder")]


def test_unbounded_series_with_unreachable_trigger_is_safely_skipped():
    # An unbounded series whose alarm can never fire inside the window is
    # not expanded across the whole representable range either: the answer
    # is simply "no reminders".
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=DAILY",
        alarm("TRIGGER:-P9999999D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_unbounded_series_with_extreme_reach_hits_expansion_cap():
    # This alarm fires 9999999 days before each instance and once more
    # 9999999 days later, so instances up to year 9999 could fire inside
    # the window; expanding the clamped window trips the per-UID
    # occurrence cap — the same safety limit /expand applies to any
    # padded window.
    ics = cal(event(
        "UID:evt-1",
        "DTSTART:20260105T090000Z",
        "DTEND:20260105T100000Z",
        "RRULE:FREQ=MONTHLY",
        alarm("TRIGGER:-P9999999D", "REPEAT:1", "DURATION:P9999999D", *DISPLAY),
    ))
    resp = alarms(ics)
    assert resp.status_code == 422
    assert "10000" in resp.json()["detail"]


def test_unrepresentable_trigger_duration_returns_422():
    detail = post_422(alarm("TRIGGER:P1000000000D", *DISPLAY))
    assert "duration" in detail.lower()


def test_unrepresentable_repeat_duration_returns_422():
    detail = post_422(
        alarm("TRIGGER:-PT15M", "REPEAT:2", "DURATION:P1000000000D", *DISPLAY)
    )
    assert "duration" in detail.lower()


def test_absolute_trigger_beyond_datetime_max_returns_422():
    # 23:00 at UTC-11 on the last representable day is 10:00 UTC on
    # 10000-01-01: the UTC instant cannot be represented.
    detail = post_422(
        alarm(
            "TRIGGER;VALUE=DATE-TIME;TZID=Pacific/Midway:99991231T230000",
            *DISPLAY,
        )
    )
    assert "representable" in detail
