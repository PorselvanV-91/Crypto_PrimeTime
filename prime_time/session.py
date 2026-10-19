"""NYSE opening calendar, scoped to 2025-2028, and IANA time zones."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
IST = ZoneInfo('Asia/Kolkata')
UTC = timezone.utc


def nth_weekday(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def observed(day):
    return day + timedelta(days={5: -1, 6: 1}.get(day.weekday(), 0))


def holidays(year):
    if not 2025 <= year <= 2028:
        raise ValueError('Calendar supports 2025-2028; review NYSE closures before extending.')
    # Gregorian Easter; Good Friday is two days earlier.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, rem = divmod(h + l - 7 * m + 114, 31)
    easter = date(year, month, rem + 1)
    memorial = date(year, 5, 31)
    memorial -= timedelta(days=memorial.weekday())
    result = {
        nth_weekday(year, 1, 0, 3), nth_weekday(year, 2, 0, 3),
        easter - timedelta(days=2), memorial,
        observed(date(year, 6, 19)), observed(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1), nth_weekday(year, 11, 3, 4),
        observed(date(year, 12, 25)),
    }
    # NYSE does not observe a Saturday New Year's Day on the prior Friday.
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:
        result.add(observed(new_year))
    if year == 2025:
        result.add(date(2025, 1, 9))  # Carter national day of mourning
    return result


def session_open(day):
    closed = holidays(day.year)
    if day.weekday() >= 5 or day in closed:
        return None
    return datetime.combine(day, time(9, 30), NY).astimezone(UTC)
