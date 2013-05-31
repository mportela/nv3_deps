from __future__ import division
from datetime import datetime
from django.conf import settings

try:
    # Django 1.4+
    from django.utils import timezone
    timezone = settings.USE_TZ
except:
    timezone = None

try:
    # Django 1.4+
    from django.utils import formats
except:
    from django.utils.translation import get_date_formats
    from django.utils import dateformat
    formats = None

def now():
    """
    Returns a time-zone aware, UTC ``datetime``, if necessary, otherwise just a
    simple "naive" ``datetime`` object of the current date and time.
    """
    if timezone:
        return timezone.now()
    return datetime.now()

def get_timezone(tz):
    if timezone:
        tz = tz.lower()
        if tz == 'utc':
            return timezone.utc
        elif tz == 'default':
            return timezone.get_default_timezone()
        elif tz == 'current':
            return timezone.get_current_timezone()
    return None

def get_tz_date(dt, meth, tz):
    if timezone:
        tzobj = get_timezone(tz)
        dt = meth(dt, tzobj)
    return dt

def convert_timezone(dt, tz='utc'):
    tzobj = get_timezone(tz)
    dt = dt.astimezone(tzobj)
    # pytz time zones have a normalize function that will fix certain 
    # issues caused by daylight savings time.
    if hasattr(tzobj, 'normalize'):
        dt = tzobj.normalize(dt)
    return dt

def make_naive(dt, tz='utc'):
    if timezone:
        dt = get_tz_date(dt, timezone.make_naive, tz)
    return dt

def make_aware(dt, tz='utc'):
    if timezone:
        # If this timezone is already aware, just convert to the requested 
        # timezone instead.
        if dt.tzinfo != None:
            dt = convert_timezone(dt, tz)
        else:
            dt = get_tz_date(dt, timezone.make_aware, tz)
    return dt

def local_dateformat(dt):
    """
    Returns a string representation of the given ``datetime`` ``dt``.
    """
    if formats:
        try:
            return formats.localize(dt, use_l10n=True)
        except TypeError:
            # Django 1.2
            return formats.localize(dt)
    return dateformat.format(dt, get_date_formats()[1])


def localtime(dt):
    if timezone and dt != None:
        return timezone.localtime(dt)
    return dt

def total_seconds(td):
    # The timedelta.total_seconds() function was added in Python 2.7.
    # If total_seconds() exists, use it.  Otherwise calculate manually.
    if hasattr(td, 'total_seconds'):
        return td.total_seconds()
    else:
        return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6
