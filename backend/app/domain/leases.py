"""Lease lifecycle status and expiry alerts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

STATUS_LABELS = {
    "upcoming": "Por iniciar",
    "active": "Activo",
    "expiring_soon": "Vence pronto",
    "expired": "Vencido",
    "terminated": "Rescindido",
}


def lease_status(start: date, end: date, today: date, expiring_soon_days: int = 90,
                 terminated_at: date | None = None) -> str:
    if terminated_at and terminated_at <= today:
        return "terminated"
    if start > today:
        return "upcoming"
    if end < today:
        return "expired"
    if (end - today).days <= expiring_soon_days:
        return "expiring_soon"
    return "active"


@dataclass(frozen=True)
class ExpiryAlert:
    lease_id: int
    level: str          # 'info' | 'warning' | 'critical'
    message: str
    days_left: int


ALERT_MILESTONES = (90, 60, 30, 7, 0)


def expiry_alerts(lease_id: int, unit: str, end: date, today: date,
                  has_renewal_option: bool, renewal_notice_days: int | None) -> list[ExpiryAlert]:
    """Alerts the daily job raises. Renewal-option notice deadlines are the
    most expensive to miss (the option may lapse), so they're 'critical'."""
    days = (end - today).days
    alerts: list[ExpiryAlert] = []
    if days < 0:
        alerts.append(ExpiryAlert(lease_id, "critical",
                                  f"Unidad {unit}: contrato vencido hace {-days} días sin renovar ni desocupar", days))
    elif days in ALERT_MILESTONES or days <= 7:
        level = "critical" if days <= 30 else "warning" if days <= 60 else "info"
        alerts.append(ExpiryAlert(lease_id, level, f"Unidad {unit}: el contrato vence en {days} días ({end:%d/%m/%Y})", days))
    if has_renewal_option and renewal_notice_days:
        notice_left = days - renewal_notice_days
        if 0 <= notice_left <= 30:
            alerts.append(ExpiryAlert(lease_id, "critical",
                                      f"Unidad {unit}: plazo para notificar opción de renovación " + ("vence hoy" if notice_left == 0 else f"en {notice_left} día{'s' if notice_left != 1 else ''}"), days))
    return alerts
