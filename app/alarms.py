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

Boundary policy. Python datetimes only span years 1..9999, but an authored
relative TRIGGER or REPEAT/DURATION span may reach far beyond that. The three
places that add such spans share one strategy:

* **validation** — a REPEAT/DURATION pair whose total fan-out cannot even be
  represented as a duration (it overflows Python's timedelta range) is
  rejected eagerly with :class:`AlarmError` (422);
* **window padding** — the back-projected instance window is shifted in an
  unbounded integer "tick" domain (microseconds, arbitrary precision) and
  saturated at the datetime limits, which is exact because no instance that
  could fire inside the query window lies beyond the unreachable bound;
* **per-instance fan** — each firing is likewise located by tick arithmetic;
  firings outside the representable range that cannot hit the query window
  are safely skipped, and only in-window firings (representable by
  construction) become :class:`Reminder` objects.

Ordinary relative/absolute alarms and the half-open ``[window_start,
window_end)`` semantics are unaffected.
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

# Python datetimes only cover years 1..9999. Every boundary-sensitive
# computation in this module (window padding, per-instance firing math) is
# expressed in this integer "tick" domain — microseconds since the UTC
# epoch, unconstrained in width — so adding or multiplying authored
# durations can never raise OverflowError. Ticks are converted back to
# datetimes only for values known to fall inside the query window, which
# are representable by construction.
_US_PER_SECOND = 1_000_000
_US_PER_DAY = 86_400 * _US_PER_SECOND

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)


def _ticks(value: dt.datetime) -> int:
    """Microseconds since the UTC epoch (arbitrary precision)."""
    delta = value - _EPOCH
    return (
        delta.days * _US_PER_DAY
        + delta.seconds * _US_PER_SECOND
        + delta.microseconds
    )


_DATETIME_MIN_TICKS = _ticks(dt.datetime.min.replace(tzinfo=UTC))
_DATETIME_MAX_TICKS = _ticks(dt.datetime.max.replace(tzinfo=UTC))


def _delta_ticks(value: dt.timedelta) -> int:
    """A duration as microseconds (arbitrary precision)."""
    return (
        value.days * _US_PER_DAY
        + value.seconds * _US_PER_SECOND
        + value.microseconds
    )


def _from_ticks(ticks: int) -> dt.datetime:
    """Rebuild an aware UTC datetime from in-range ticks."""
    days, remainder = divmod(ticks - _DATETIME_MIN_TICKS, _US_PER_DAY)
    return dt.datetime.min.replace(tzinfo=UTC) + dt.timedelta(
        days=days, microseconds=remainder
    )


def _shift_clamped(value: dt.datetime, delta_ticks: int) -> dt.datetime:
    """Shift ``value`` by signed microseconds, saturating at the bounds.

    Used for the padded instance window: padding that would push the bound
    past year 1/9999 saturates instead of raising. Saturation is exact here
    because no instance that could fire inside the query window lives
    beyond the (unreachable) true bound.
    """
    total = _ticks(value) + delta_ticks
    if total <= _DATETIME_MIN_TICKS:
        return dt.datetime.min.replace(tzinfo=UTC)
    if total >= _DATETIME_MAX_TICKS:
        return dt.datetime.max.replace(tzinfo=UTC)
    return _from_ticks(total)


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
        inst_start = _shift_clamped(window_start, -pad_before)
        inst_end = _shift_clamped(window_end, pad_after)
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
) -> tuple[int, int]:
    """How far one UID group's instance expansion must reach beyond the
    query window for its reminders to be found, as signed microseconds.

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

    Reaches are accumulated in the unbounded integer tick domain, so even
    an extreme authored span (e.g. REPEAT*DURATION beyond Python's
    timedelta range) cannot overflow here; the caller saturates the padded
    window to the representable datetime range.
    """
    masters = [e for e in events if "RECURRENCE-ID" not in e]
    master = masters[0] if masters else None
    master_duration = None
    if master is not None:
        start, end, _ = _event_times(master)
        master_duration = end - start

    pad_before = pad_after = 0
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
            offset = (
                _delta_ticks(duration) if spec.related_end else 0
            ) + _delta_ticks(spec.trigger)
            fanout = spec.repeat * _delta_ticks(spec.spacing)
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
    try:
        raw = trigger_prop.dt
    except (OverflowError, ValueError) as exc:
        raise AlarmError(f"VALARM TRIGGER duration cannot be represented: {exc}") from exc

    value_type = str(params.get("VALUE", "")).upper()
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
    try:
        # The full fan-out must itself be expressible as a duration; a
        # REPEAT/DURATION pair whose span leaves the representable timedelta
        # range cannot be expanded and is rejected up front (422) rather
        # than overflowing later.
        repeat * spacing
    except OverflowError as exc:
        raise AlarmError(
            "VALARM REPEAT*DURATION fan-out cannot be represented as a duration"
        ) from exc
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
    # The firing schedule is computed entirely in the unbounded integer
    # tick domain. The first firing or a later one may lie outside the
    # representable datetime range (an extreme relative TRIGGER on an
    # instance near year 1/9999, or a late REPEAT step); such firings are
    # safely skipped when they cannot reach the window, instead of being
    # built as datetimes and raising. Firings that fall in the window are
    # representable by construction, so the half-open comparison semantics
    # are unchanged.
    if isinstance(spec.trigger, dt.datetime):
        first_ticks = _ticks(spec.trigger)
    else:
        anchor = instance.end if spec.related_end else instance.start
        first_ticks = _ticks(anchor) + _delta_ticks(spec.trigger)
    spacing_ticks = _delta_ticks(spec.spacing)

    lo = _ticks(window_start)
    hi = _ticks(window_end)
    count = spec.repeat + 1

    # Restrict the loop to the firing indices whose instant is inside the
    # window. Spacing is positive whenever repeat > 0, so the candidate
    # indices form one contiguous interval; unrepresentable firings below
    # the window are skipped by the interval itself.
    if spacing_ticks > 0:
        needed = lo - first_ticks  # smallest k with first + k*s >= lo
        start_index = (
            (needed + spacing_ticks - 1) // spacing_ticks if needed > 0 else 0
        )
        too_far = hi - first_ticks  # exclusive bound: first + k*s < hi
        end_index = (
            min(count, (too_far - 1) // spacing_ticks + 1) if too_far > 0 else 0
        )
    else:
        start_index = 0
        end_index = count if lo <= first_ticks < hi else 0

    reminders = []
    for index in range(start_index, end_index):
        current_ticks = first_ticks + index * spacing_ticks
        if current_ticks < _DATETIME_MIN_TICKS or current_ticks > _DATETIME_MAX_TICKS:
            # The padded window can only reach instances represented by
            # datetimes, and a monotone fan cannot re-enter the window after
            # leaving the representable range; an unrepresentable firing
            # therefore cannot hit the query window and is safely skipped.
            continue
        reminders.append(
            Reminder(
                uid=instance.uid,
                recurrence_id=instance.recurrence_id,
                summary=instance.summary,
                trigger=_from_ticks(current_ticks),
                description=spec.description,
            )
        )
    return reminders
