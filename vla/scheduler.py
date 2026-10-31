"""Programación de trabajos con especificaciones sencillas.

Formatos admitidos (hora local de VLA_TIMEZONE):
  "every 15m", "every 3h"   cada 15 minutos, cada 3 horas
  "09:00"                   todos los días a las 09:00
  "mon 08:30"               cada lunes a las 08:30 (mon..sun o lun..dom)
  "mon,wed,fri 08:30"       lunes, miércoles y viernes a las 08:30
  "1 09:00"                 el día 1 de cada mes a las 09:00
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6,
}


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    kind: str  # interval | daily | weekly | monthly
    minutes: int = 0
    hour: int = 0
    minute: int = 0
    weekdays: Tuple[int, ...] = ()
    day: int = 1


def parse(spec: str) -> Schedule:
    text = spec.strip().lower()
    m = re.fullmatch(r"every\s+(\d+)\s*(m|h)", text)
    if m:
        minutes = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
        return Schedule("interval", minutes=max(1, minutes))
    m = re.fullmatch(r"(?:([\w,]+)\s+)?(\d{1,2}):(\d{2})", text)
    if not m:
        raise ScheduleError(f"Programación no válida: {spec!r}")
    prefix, hour, minute = m.group(1), int(m.group(2)), int(m.group(3))
    if hour > 23 or minute > 59:
        raise ScheduleError(f"Hora no válida: {spec!r}")
    if prefix is None:
        return Schedule("daily", hour=hour, minute=minute)
    days = [d.strip() for d in prefix.split(",") if d.strip()]
    if days and all(d in WEEKDAYS for d in days):
        return Schedule("weekly", hour=hour, minute=minute, weekdays=tuple(sorted({WEEKDAYS[d] for d in days})))
    if prefix.isdigit() and 1 <= int(prefix) <= 31:
        return Schedule("monthly", hour=hour, minute=minute, day=int(prefix))
    raise ScheduleError(f"Programación no válida: {spec!r}")


def last_due(s: Schedule, now: datetime) -> datetime:
    """Último momento programado igual o anterior a `now` (no aplica a intervalos)."""
    at = now.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
    if s.kind == "daily":
        return at if at <= now else at - timedelta(days=1)
    if s.kind == "weekly":
        for back in range(8):
            candidate = at - timedelta(days=back)
            if candidate.weekday() in s.weekdays and candidate <= now:
                return candidate
        raise ScheduleError("programación semanal sin días")
    if s.kind == "monthly":
        year, month = now.year, now.month
        for _ in range(2):
            day = min(s.day, calendar.monthrange(year, month)[1])
            candidate = at.replace(year=year, month=month, day=day)
            if candidate <= now:
                return candidate
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        return candidate
    raise ScheduleError("last_due no se aplica a intervalos")


def next_due(s: Schedule, now: datetime) -> datetime:
    """Próximo momento programado después de `now` (para /estado)."""
    if s.kind == "interval":
        return now + timedelta(minutes=s.minutes)
    probe = now
    for _ in range(40):
        probe += timedelta(days=1)
        due = last_due(s, probe)
        if due > now:
            # el primero posterior a `now`: retrocede mientras el anterior también lo sea
            while True:
                earlier = last_due(s, due - timedelta(minutes=1))
                if earlier <= now:
                    return due
                due = earlier
    return probe


def is_due(s: Schedule, now: datetime, last_run: Optional[datetime]) -> bool:
    if s.kind == "interval":
        return last_run is None or now - last_run >= timedelta(minutes=s.minutes)
    if last_run is None:
        return False  # la primera vez solo se registra; así un reinicio no dispara nada atrasado
    return last_due(s, now) > last_run
