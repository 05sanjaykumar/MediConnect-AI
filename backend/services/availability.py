# backend/services/availability.py
"""What a doctor's live status means for bookability.

Kept in one place because three different callers need to agree on it: the slot
query, the booking guard, and the status-change routine that blocks slots.
"""

from datetime import datetime, timedelta

# How far ahead of `now` each status stops bookings.
#   None  -> not blocking at all
#   "day" -> blocks the rest of the working day
BLOCK_WINDOWS = {
    "available": None,
    "busy": timedelta(minutes=15),      # just the consultation in progress
    "on_break": timedelta(minutes=30),
    "in_surgery": "day",
    "off_duty": "day",
}


def end_of_day(moment: datetime) -> datetime:
    return moment.replace(hour=23, minute=59, second=59, microsecond=0)


def blocked_until(status: str, now: datetime | None = None) -> datetime | None:
    """The instant after which bookings are fine again, or None if unblocked."""
    now = now or datetime.now()
    window = BLOCK_WINDOWS.get(status)
    if window is None:
        return None
    if window == "day":
        return end_of_day(now)
    return now + window


def is_slot_bookable(status: str, slot_start: datetime, now: datetime | None = None) -> bool:
    """Can this specific slot be booked, given the doctor's current status?"""
    now = now or datetime.now()
    if slot_start <= now:
        return False
    until = blocked_until(status, now)
    return until is None or slot_start > until
