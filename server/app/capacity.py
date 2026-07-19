"""Capacity monitoring for the DeepBox server.

Two finite resources can take the service down: the SQLite database file growing
without bound, and the recording directory filling the disk. This module turns
raw measurements into operator-facing *status* (ok / warn / alert) using the
configurable thresholds on :class:`server.app.config.Settings`.

The functions here are deliberately pure where possible so they can be unit
tested without touching a real filesystem: :func:`evaluate_capacity` takes the
already-measured numbers, while :func:`collect_capacity` gathers them from disk.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Status ordering, worst last. Used to compute the overall status.
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ALERT = "alert"
_SEVERITY = {STATUS_OK: 0, STATUS_WARN: 1, STATUS_ALERT: 2}

_BYTES_PER_MB = 1024 * 1024


def transition_event(previous: str, current: str) -> str | None:
    """Return the event name for a capacity-state transition, if any."""

    if previous == current:
        return None
    return "capacity.recovered" if current == STATUS_OK else "capacity.threshold"


@dataclass(frozen=True)
class ResourceStatus:
    name: str
    status: str
    value_mb: float
    warn_mb: float
    alert_mb: float
    detail: str


@dataclass(frozen=True)
class CapacityReport:
    status: str
    resources: tuple[ResourceStatus, ...]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "resources": [
                {
                    "name": r.name,
                    "status": r.status,
                    "value_mb": round(r.value_mb, 3),
                    "warn_mb": r.warn_mb,
                    "alert_mb": r.alert_mb,
                    "detail": r.detail,
                }
                for r in self.resources
            ],
        }


def _overall(statuses: list[str]) -> str:
    worst = STATUS_OK
    for s in statuses:
        if _SEVERITY[s] > _SEVERITY[worst]:
            worst = s
    return worst


def classify_growing(value_mb: float, warn_mb: float, alert_mb: float) -> str:
    """Classify a metric where *larger is worse* (e.g. database size)."""

    if value_mb >= alert_mb:
        return STATUS_ALERT
    if value_mb >= warn_mb:
        return STATUS_WARN
    return STATUS_OK


def classify_remaining(value_mb: float, warn_mb: float, alert_mb: float) -> str:
    """Classify a metric where *smaller is worse* (e.g. free disk space)."""

    if value_mb <= alert_mb:
        return STATUS_ALERT
    if value_mb <= warn_mb:
        return STATUS_WARN
    return STATUS_OK


def evaluate_capacity(
    *,
    db_size_mb: float,
    disk_free_mb: float,
    db_size_warn_mb: float,
    db_size_alert_mb: float,
    disk_free_warn_mb: float,
    disk_free_alert_mb: float,
) -> CapacityReport:
    """Build a :class:`CapacityReport` from measured numbers and thresholds."""

    db_status = classify_growing(db_size_mb, db_size_warn_mb, db_size_alert_mb)
    disk_status = classify_remaining(
        disk_free_mb, disk_free_warn_mb, disk_free_alert_mb
    )

    db = ResourceStatus(
        name="database",
        status=db_status,
        value_mb=db_size_mb,
        warn_mb=db_size_warn_mb,
        alert_mb=db_size_alert_mb,
        detail=f"database file is {db_size_mb:.1f} MB",
    )
    disk = ResourceStatus(
        name="recording_disk_free",
        status=disk_status,
        value_mb=disk_free_mb,
        warn_mb=disk_free_warn_mb,
        alert_mb=disk_free_alert_mb,
        detail=f"{disk_free_mb:.1f} MB free on recording volume",
    )
    return CapacityReport(
        status=_overall([db_status, disk_status]),
        resources=(db, disk),
    )


def database_size_mb(database_url: str) -> Optional[float]:
    """Return the size of a SQLite database file in MB, or ``None``.

    Only ``sqlite`` URLs map to a file on disk; other backends (or an in-memory
    database) return ``None`` so the caller can skip the check.
    """

    path = sqlite_path(database_url)
    if path is None:
        return None
    try:
        return path.stat().st_size / _BYTES_PER_MB
    except OSError:
        return 0.0


def sqlite_path(database_url: str) -> Optional[Path]:
    """Extract the filesystem path from a ``sqlite:///`` URL, if any."""

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw = database_url[len(prefix) :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def disk_free_mb(path: Path) -> float:
    """Return free space in MB on the volume that would hold ``path``.

    Walks up to the nearest existing ancestor so the measurement works even
    before the recording directory has been created.
    """

    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    usage = shutil.disk_usage(os.fspath(probe))
    return usage.free / _BYTES_PER_MB


def collect_capacity(settings) -> CapacityReport:
    """Measure and evaluate capacity for the given settings object."""

    db_mb = database_size_mb(settings.database_url)
    recordings = Path(settings.data_dir) / "sessions"
    free_mb = disk_free_mb(recordings)

    resources: list[ResourceStatus] = []
    statuses: list[str] = []

    if db_mb is not None:
        db_status = classify_growing(
            db_mb, settings.db_size_warn_mb, settings.db_size_alert_mb
        )
        resources.append(
            ResourceStatus(
                name="database",
                status=db_status,
                value_mb=db_mb,
                warn_mb=settings.db_size_warn_mb,
                alert_mb=settings.db_size_alert_mb,
                detail=f"database file is {db_mb:.1f} MB",
            )
        )
        statuses.append(db_status)

    disk_status = classify_remaining(
        free_mb, settings.disk_free_warn_mb, settings.disk_free_alert_mb
    )
    resources.append(
        ResourceStatus(
            name="recording_disk_free",
            status=disk_status,
            value_mb=free_mb,
            warn_mb=settings.disk_free_warn_mb,
            alert_mb=settings.disk_free_alert_mb,
            detail=f"{free_mb:.1f} MB free on recording volume",
        )
    )
    statuses.append(disk_status)

    return CapacityReport(status=_overall(statuses), resources=tuple(resources))
