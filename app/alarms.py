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

Extreme reaches never overflow date arithmetic: padding and trigger
computations are done in exact integer microseconds (see the "Range-safe
time arithmetic" section), the back-projected instance window is clamped
to the representable datetime range, and a trigger instant outside that
range — which can never land inside a representable query window — is
safely skipped, per REPEAT step. Alarm definitions that cannot be
represented or expanded at all (an unparseable ``TRIGGER``/``DURATION``
value, an absolute trigger whose UTC instant is out of range) raise
:class:`AlarmError` like any other invalid input.

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


# ---------------------------------------------------------------------------
# Range-safe time arithmetic
#
# Python datetimes cover the years 1..9999 and timedeltas roughly ±2.7
# million years, but VALARM definitions may carry reaches far beyond either
# (a TRIGGER of -P9999999D, a REPEAT/DURATION fan-out of 999 × P9999999D).
# Doing that arithmetic directly on datetimes and timedeltas raises
# OverflowError. All alarm reach and trigger computations are therefore
# done in exact integer microseconds relative to _EPOCH — Python ints are
# unbounded — and values are converted back to datetimes only once they are
# known to lie inside the query window, which is itself always
# representable, so the conversion can never overflow. A trigger instant
# outside the representable range can never satisfy the window check and is
# skipped exactly, never approximated.
# ---------------------------------------------------------------------------

_EPOCH = dt.datetime.min.replace(tzinfo=UTC)


def _timedelta_us(delta: dt.timedelta) -> int:
    """Exact integer microseconds of ``delta`` (unbounded magnitude)."""
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


_MAX_US = _timedelta_us(dt.datetime.max.replace(tzinfo=UTC) - _EPOCH)

# Room the recurrence kernel needs beyond a clamped instance-window bound:
# its own duration back-padding is covered by the event duration, and this
# margin covers the local-frame conversion of the bounds (the largest UTC
# offset is well under two days).
_KERNEL_WINDOW_MARGIN = dt.timedelta(days=2)


def _datetime_us(moment: dt.datetime) -> int:
    """Exact integer microseconds from _EPOCH to an aware UTC datetime."""
    return _timedelta_us(moment - _EPOCH)


def _datetime_from_us(us: int) -> dt.datetime:
    """Inverse of :func:`_datetime_us`; only safe for 0 <= us <= _MAX_US."""
    return _EPOCH + dt.timedelta(microseconds=us)


def _padded_bound(us: int, margin_us: int) -> dt.datetime:
    """Closest safely representable datetime to ``us`` microseconds.

    A bound inside the representable range is returned unchanged. One that
    underflows/overflows it is clamped to ``min + margin`` / ``max - margin``
    so the recurrence kernel's own window arithmetic (duration back-padding,
    timezone conversion) stays in range as well. Clamping only trims
    unrepresentable territory — never an instance that could fire inside
    the query window.
    """
    margin_us = min(margin_us, _MAX_US // 2)
    if us < 0:
        us = margin_us
    elif us > _MAX_US:
        us = _MAX_US - margin_us
    return _datetime_from_us(us)


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
    window_start_us = _datetime_us(window_start)
    window_end_us = _datetime_us(window_end)
    for uid, events in group_events_by_uid(calendar).items():
        try:
            pad_before_us, pad_after_us, margin_us = _expansion_padding(
                events, specs, window_start_us, window_end_us
            )
            inst_start = _padded_bound(window_start_us - pad_before_us, margin_us)
            inst_end = _padded_bound(window_end_us + pad_after_us, margin_us)
            candidates = compute_candidates(uid, events, inst_start, inst_end)
        except OverflowError as exc:
            # An input the clamped window still cannot expand safely (e.g.
            # an event DURATION of millions of days) is rejected like any
            # other invalid alarm definition.
            raise AlarmError(
                "alarm expansion overflows the representable datetime range"
            ) from exc
        for slot in candidates:
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

    reminders.sort(
        key=lambda r: (
            r.trigger,
            r.uid,
            r.recurrence_id or _EPOCH,
            r.description,
        )
    )
    return reminders


def _expansion_padding(
    events: list, specs: dict[int, _AlarmSpec], start_us: int, end_us: int
) -> tuple[int, int, int]:
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

    ``start_us``/``end_us`` are the query window bounds in integer
    microseconds (see the range-safe arithmetic note above). Returns
    ``(pad_before_us, pad_after_us, margin_us)`` in the same unit, so
    arbitrarily extreme TRIGGER/REPEAT/DURATION values cannot overflow.
    The pads cover exactly the reach above — derived from the authored
    values, never a fixed number of days; absolute DATE-TIME triggers do
    not move with the instance, so they contribute no padding. An alarm
    whose firing condition no representable ``t`` can satisfy (its whole
    trigger envelope falls outside any reachable window) contributes no
    padding either: its reminders can never land inside the query window,
    so no instance needs to be expanded for it. ``margin_us`` is the extra
    room :func:`_padded_bound` leaves at the datetime edges for the
    recurrence kernel's own window arithmetic: the master event's duration
    (its backward padding) plus a fixed margin for local-frame conversion.
    """
    masters = [e for e in events if "RECURRENCE-ID" not in e]
    master = masters[0] if masters else None
    master_duration = None
    master_duration_us = 0
    if master is not None:
        start, end, _ = _event_times(master)
        master_duration = end - start
        master_duration_us = _timedelta_us(master_duration)

    pad_before = pad_after = 0
    for event in events:
        alarms = [c for c in event.subcomponents if c.name == "VALARM"]
        if not alarms:
            continue
        if event is master:
            duration_us = master_duration_us
        else:
            # An override without DTEND/DURATION inherits the master's
            # duration, exactly as the expansion kernel applies it.
            start, end, _ = _event_times(event, default_duration=master_duration)
            duration_us = _timedelta_us(end - start)
        for alarm in alarms:
            spec = specs[id(alarm)]
            if isinstance(spec.trigger, dt.datetime):
                continue  # absolute trigger: independent of the instance time
            offset_us = _timedelta_us(spec.trigger)
            if spec.related_end:
                offset_us += duration_us
            fanout_us = spec.repeat * _timedelta_us(spec.spacing)
            # The firing condition above has a solution only when some
            # representable t (0 <= t <= _MAX_US) lies in
            # [start_us - (offset + fanout), end_us - offset); otherwise
            # this alarm's triggers are all unrepresentable or outside the
            # window and it needs no expansion reach.
            if offset_us >= end_us or offset_us + fanout_us < start_us - _MAX_US:
                continue
            pad_before = max(pad_before, offset_us + fanout_us)
            pad_after = max(pad_after, -offset_us)
    margin_us = max(0, master_duration_us) + _timedelta_us(_KERNEL_WINDOW_MARGIN)
    return pad_before, pad_after, margin_us


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
    try:
        raw = trigger_prop.dt
    except (OverflowError, ValueError) as exc:
        raise AlarmError(f"TRIGGER value cannot be represented: {exc}") from exc
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
    try:
        return _to_utc(value)
    except OverflowError as exc:
        # e.g. a TZID behind UTC on the last representable day pushes the
        # UTC instant past year 9999.
        raise AlarmError(
            "absolute TRIGGER is outside the representable datetime range"
        ) from exc


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
    try:
        spacing = duration_prop.dt
    except (OverflowError, ValueError) as exc:
        raise AlarmError(f"VALARM DURATION cannot be represented: {exc}") from exc
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
    # Every instant is handled in exact integer microseconds (see the
    # range-safe arithmetic note above): the query window is always
    # representable, so a trigger instant outside the datetime range can
    # never satisfy the window check, and only in-window instants are
    # converted back to datetimes — a conversion that cannot overflow.
    # Later REPEAT steps may therefore come back into range after earlier
    # ones left it, and each is judged on its own.
    if isinstance(spec.trigger, dt.datetime):
        first_us = _datetime_us(spec.trigger)
    else:
        anchor = instance.end if spec.related_end else instance.start
        first_us = _datetime_us(anchor) + _timedelta_us(spec.trigger)
    spacing_us = _timedelta_us(spec.spacing)
    start_us = _datetime_us(window_start)
    end_us = _datetime_us(window_end)

    reminders = []
    current_us = first_us
    for _ in range(spec.repeat + 1):
        if start_us <= current_us < end_us:
            reminders.append(
                Reminder(
                    uid=instance.uid,
                    recurrence_id=instance.recurrence_id,
                    summary=instance.summary,
                    trigger=_datetime_from_us(current_us),
                    description=spec.description,
                )
            )
        current_us += spacing_us
    return reminders
