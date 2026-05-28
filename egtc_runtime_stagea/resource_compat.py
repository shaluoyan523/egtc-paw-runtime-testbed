from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceUsage:
    ru_utime: float = 0.0
    ru_stime: float = 0.0
    ru_maxrss: float = 0.0


try:  # pragma: no cover - platform-specific import
    import resource as _resource
except ModuleNotFoundError:  # pragma: no cover - exercised on Windows
    _resource = None


def get_child_usage() -> ResourceUsage:
    if _resource is None:
        return ResourceUsage()
    usage = _resource.getrusage(_resource.RUSAGE_CHILDREN)
    return ResourceUsage(
        ru_utime=float(getattr(usage, "ru_utime", 0.0)),
        ru_stime=float(getattr(usage, "ru_stime", 0.0)),
        ru_maxrss=float(getattr(usage, "ru_maxrss", 0.0)),
    )
