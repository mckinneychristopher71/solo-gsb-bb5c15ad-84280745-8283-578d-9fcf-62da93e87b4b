"""VALARM expansion for VEVENT instances.

Each :class:`~app.recurrence.Instance` produced by the recurrence kernel
carries the raw VALARM components of the event that defines it:

* a master event's VALARMs apply to **every** expanded instance of the
  recurring series (master alarms are never re-attached per occurrence in
  the ICS text — one definition applies to the whole series);
* a ``RECURRENCE-ID`` override carries **its own** VALARMs and replaces the
  master's alarms for that instance (an override with no VALARM has no
  reminders).

Only ``ACTION:DISPLAY`` alarms are supported. ``TRIGGER`` may be absolute
(``VALUE=DATE-TIME``) or a positive/negative duration relative to DTSTART
(the default) or DTEND (``RELATED=END``). ``REPEAT``/``DURATION`` (which
must appear together per RFC 5545) fan the alarm out into repeated
reminders spaced by DURATION. Only reminders whose trigger instant falls
inside the half-open query window ``[window_start, window_end)`` are
returned.

A reminder fires inside the window even when its instance does not overlap
the window itself (a two-day-ahead reminder for an event just past
``window_end``, a follow-up ping long after an event before
``window_start``). The instance expansion is therefore back-projected from
every duration TRIGGER — positive or negative, anchored at DTSTART or
DTEND, including the final REPEAT/DURATION firing — by exactly the amounts
the alarm definitions imply (never a fixed number of days). Absolute
``DATE-TIME`` triggers do not move with the instance, so they are not
back-projected: they keep firing for the instances overlapping the query
window and are filtered by their trigger time only.

Anything invalid or unsupported raises :class:`AlarmError`, which the HTTP
layer maps to 422.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .recurrence import (
    Instance,
    _event_times,
    _overlaps,
    _parse_calendar,
    _to_utc,
    compute_candidates,
    group_events_by_uid,
)

UTC = dt.timezone.utc

# Upper bound on reminders one alarm may fan out to (1 initial trigger plus
# REPEAT repeats), keeping an abusive REPEAT from flooding a response.
MAX_REMINDERS_PER_ALARM = 1_000


class AlarmError(Exception):
    """Invalid or unsupported VALARM input; surfaced to the client as 422."""


@dataclass(frozen=True)
class Reminder:
    """One concrete alarm trigger bound to a specific event instance."""

    uid: str
    recurrence_id: dt.datetime | None  # aware UTC; None for non-recurring events
    summary: str | None
    trigger: dt.datetime  # aware UTC
    description: str


@dataclass(frozen=True)
class _AlarmSpec:
    """A validated VALARM definition, independent of a concrete instance."""

    trigger: dt.datetime | dt.timedelta  # absolute datetime, or relative delta
    related_end: bool  # a relative trigger anchors to DTEND, not DTSTART
    description: str
    repeat: int  # additional repetitions after the first trigger
    spacing: dt.timedelta  # time between repeated triggers


def expand_alarms(
    ics_text: str, window_start: dt.datetime, window_end: dt.datetime
) -> list[Reminder]:
    """Expand every VALARM whose reminders fire inside the UTC window.

    Instances come from the same recurrence kernel as /expand, computed per
    UID over a window back-projected from the group's own duration TRIGGERs
    (see :func:`_expansion_padding`), so a reminder is found even when its
    instance does not overlap the query window itself. An instance whose
    event has no VALARMs contributes nothing. All VALARMs in the calendar
    are validated eagerly (like the expander validates every event), so an
    unsupported alarm yields 422 even if its trigger would fall outside the
    window.
    """
    calendar = _parse_calendar(ics_text)

    # Eager validation of every VALARM in the calendar; the parsed specs
    # also drive the instance-window back-projection below.
    specs: dict[int, _AlarmSpec] = {}
    for alarm in calendar.walk("VALARM"):
        specs[id(alarm)] = _parse_alarm(alarm)

    reminders: list[Reminder] = []
    for uid, events in group_events_by_uid(calendar).items():
        pad_before, pad_after = _expansion_padding(events, specs)
        inst_start = window_start - pad_before
        inst_end = window_end + pad_after
        for slot in compute_candidates(uid, events, inst_start, inst_end):
            instance = slot.instance
            if instance is None or not _overlaps(
                instance.start, instance.end, inst_start, inst_end
            ):
                continue
            in_window = _overlaps(
                instance.start, instance.end, window_start, window_end
            )
            for alarm in instance.alarms:
                spec = specs[id(alarm)]
                if isinstance(spec.trigger, dt.datetime) and not in_window:
                    # Absolute triggers are not back-projected: they fire
                    # for the instances overlapping the query window itself
                    # and are filtered by their trigger time only.
                    continue
                reminders.extend(
                    _materialize(spec, instance, window_start, window_end)
                )

    no_rid = dt.datetime.min.replace(tzinfo=UTC)
    reminders.sort(
        key=lambda r: (
            r.trigger,
            r.uid,
            r.recurrence_id or no_rid,
            r.description,
        )
    )
    return reminders


def _expansion_padding(
    events: list, specs: dict[int, _AlarmSpec]
) -> tuple[dt.timedelta, dt.timedelta]:
    """How far one UID group's instance expansion must reach beyond the
    query window for its reminders to be found.

    A duration TRIGGER fires at ``anchor + delta`` (anchor = DTSTART, or
    DTEND for ``RELATED=END``), and REPEAT/DURATION extends the fan-out to
    ``anchor + delta + repeat * spacing``. An instance starting at ``t``
    therefore fires inside ``[window_start, window_end)`` iff its first
    trigger starts before the window ends and its last one does not end
    before it begins::

        window_start - (offset + fanout) <= t < window_end - offset
        with offset = (duration if RELATED=END else 0) + delta

    The returned ``(pad_before, pad_after)`` covers exactly those reaches —
    derived from the authored TRIGGER/REPEAT/DURATION values, never a fixed
    number of days. Absolute DATE-TIME triggers do not move with the
    instance, so they contribute no padding.
    """
    masters = [e for e in events if "RECURRENCE-ID" not in e]
    master = masters[0] if masters else None
    master_duration = None
    if master is not None:
        start, end, _ = _event_times(master)
        master_duration = end - start

    pad_before = pad_after = dt.timedelta(0)
    for event in events:
        alarms = [c for c in event.subcomponents if c.name == "VALARM"]
        if not alarms:
            continue
        if event is master:
            duration = master_duration
        else:
            # An override without DTEND/DURATION inherits the master's
            # duration, exactly as the expansion kernel applies it.
            start, end, _ = _event_times(event, default_duration=master_duration)
            duration = end - start
        for alarm in alarms:
            spec = specs[id(alarm)]
            if isinstance(spec.trigger, dt.datetime):
                continue  # absolute trigger: independent of the instance time
            offset = spec.trigger
            if spec.related_end:
                offset = duration + offset
            fanout = spec.repeat * spec.spacing
            pad_before = max(pad_before, offset + fanout)
            pad_after = max(pad_after, -offset)
    return pad_before, pad_after


# ---------------------------------------------------------------------------
# VALARM parsing
# ---------------------------------------------------------------------------


def _parse_alarm(alarm) -> _AlarmSpec:
    action = alarm.get("ACTION")
    if action is None:
        raise AlarmError("VALARM is missing the required ACTION property")
    action = str(action).upper()
    if action != "DISPLAY":
        raise AlarmError(
            f"unsupported VALARM ACTION {action!r}; only DISPLAY is supported"
        )

    description = alarm.get("DESCRIPTION")
    if description is None:
        raise AlarmError("DISPLAY VALARM is missing the required DESCRIPTION")

    trigger_prop = alarm.get("TRIGGER")
    if trigger_prop is None:
        raise AlarmError("VALARM is missing the required TRIGGER property")
    params = trigger_prop.params or {}

    value_type = str(params.get("VALUE", "")).upper()
    raw = trigger_prop.dt
    related = str(params.get("RELATED", "")).upper()

    if isinstance(raw, dt.timedelta):
        if value_type not in ("", "DURATION"):
            raise AlarmError(
                f"TRIGGER VALUE={value_type} is incompatible with a duration value"
            )
        if related not in ("", "START", "END"):
            raise AlarmError(f"unsupported TRIGGER RELATED={related}")
        trigger: dt.datetime | dt.timedelta = raw
        related_end = related == "END"
    elif isinstance(raw, dt.datetime):
        if value_type not in ("", "DATE-TIME"):
            raise AlarmError(
                f"TRIGGER VALUE={value_type} is incompatible with a DATE-TIME value"
            )
        if related:
            raise AlarmError(
                "TRIGGER RELATED is only valid for duration-valued triggers"
            )
        trigger = _absolute_trigger(raw, params)
        related_end = False
    else:
        raise AlarmError(
            "unsupported TRIGGER value type: only DURATION and DATE-TIME are supported"
        )

    repeat, spacing = _parse_repeat(alarm)
    return _AlarmSpec(
        trigger=trigger,
        related_end=related_end,
        description=str(description),
        repeat=repeat,
        spacing=spacing,
    )


def _absolute_trigger(value: dt.datetime, params) -> dt.datetime:
    """Normalize an absolute TRIGGER value to an aware UTC datetime."""
    if value.tzinfo is None:
        tzid = params.get("TZID")
        if tzid:
            tzname = str(tzid)
            try:
                tz = ZoneInfo(tzname)
            except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
                raise AlarmError(f"unknown timezone: {tzname!r}") from exc
            value = value.replace(tzinfo=tz)
        else:
            # Floating time, like the expander's convention, is assumed UTC.
            value = value.replace(tzinfo=UTC)
    return _to_utc(value)


def _parse_repeat(alarm) -> tuple[int, dt.timedelta]:
    """Validate the REPEAT/DURATION pair; both or neither must be present."""
    repeat_prop = alarm.get("REPEAT")
    duration_prop = alarm.get("DURATION")
    if repeat_prop is None and duration_prop is None:
        return 0, dt.timedelta(0)
    if repeat_prop is None:
        raise AlarmError("VALARM DURATION requires REPEAT (they must appear together)")
    if duration_prop is None:
        raise AlarmError("VALARM REPEAT requires DURATION (they must appear together)")
    try:
        repeat = int(repeat_prop)
    except (TypeError, ValueError) as exc:
        raise AlarmError(f"invalid VALARM REPEAT value: {repeat_prop!r}") from exc
    if repeat < 0:
        raise AlarmError("VALARM REPEAT must not be negative")
    spacing = duration_prop.dt
    if not isinstance(spacing, dt.timedelta):
        raise AlarmError("VALARM DURATION must be a duration value")
    if spacing <= dt.timedelta(0):
        raise AlarmError("VALARM DURATION must be a positive duration")
    if repeat + 1 > MAX_REMINDERS_PER_ALARM:
        raise AlarmError(
            f"VALARM fans out to more than {MAX_REMINDERS_PER_ALARM} reminders"
        )
    return repeat, spacing


# ---------------------------------------------------------------------------
# Per-instance materialization
# ---------------------------------------------------------------------------


def _materialize(
    spec: _AlarmSpec,
    instance: Instance,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> list[Reminder]:
    if isinstance(spec.trigger, dt.datetime):
        first = spec.trigger
    else:
        anchor = instance.end if spec.related_end else instance.start
        first = anchor + spec.trigger

    reminders = []
    current = first
    for _ in range(spec.repeat + 1):
        if window_start <= current < window_end:
            reminders.append(
                Reminder(
                    uid=instance.uid,
                    recurrence_id=instance.recurrence_id,
                    summary=instance.summary,
                    trigger=current,
                    description=spec.description,
                )
            )
        current += spec.spacing
    return reminders
