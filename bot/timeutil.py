"""Helpers temporels — tout est en Europe/Paris, datetimes toujours aware."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

TZ = ZoneInfo("Europe/Paris")


def now() -> datetime:
    return datetime.now(TZ)


def parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def friday_deadline(session_dt: datetime) -> datetime:
    """Clôture des votes : vendredi 23 h 59 veille de la séance (point n° 3 de la spec)."""
    return (session_dt - timedelta(days=1)).replace(
        hour=23, minute=59, second=0, microsecond=0
    )


def sunday_announce(session_dt: datetime) -> datetime:
    """Annonce de la séance suivante : lendemain (dimanche) à 10 h."""
    return (session_dt + timedelta(days=1)).replace(
        hour=config.ANNOUNCE_HOUR, minute=0, second=0, microsecond=0
    )


def monday_reminder(session_dt: datetime) -> datetime:
    """Rappel du lundi de la semaine de séance, 18 h (samedi - 5 jours)."""
    return (session_dt - timedelta(days=5)).replace(
        hour=config.MONDAY_REMINDER_HOUR, minute=0, second=0, microsecond=0
    )


def consult_deadline(session_dt: datetime) -> datetime:
    """Deadline de consultation GM : min(maintenant + 48 h, samedi 19 h) — point validé n° 3."""
    cap = session_dt.replace(
        hour=config.CONSULT_CAP_HOUR, minute=0, second=0, microsecond=0
    )
    return min(now() + timedelta(hours=config.CONSULT_TIMEOUT_H), cap)


def ts(dt: datetime, style: str = "F") -> str:
    """Timestamp Discord natif : s'affiche dans le fuseau de chaque utilisateur."""
    return f"<t:{int(dt.timestamp())}:{style}>"
