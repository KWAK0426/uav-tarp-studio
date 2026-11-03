from __future__ import annotations

import math
from datetime import datetime, timezone


def solar_position(when: datetime, latitude_deg: float, longitude_deg: float) -> tuple[float, float]:
    """Return geometric solar zenith and azimuth (clockwise from north).

    Uses the NOAA fractional-year approximation. Accuracy is appropriate for
    UAV BRDF geometry, while the input timestamp and timezone remain explicit.
    """
    if when.tzinfo is None:
        raise ValueError("capture time must include a timezone")
    utc = when.astimezone(timezone.utc)
    day = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60.0 + utc.second / 3600.0 + utc.microsecond / 3.6e9
    gamma = 2.0 * math.pi / 365.0 * (day - 1 + (hour - 12.0) / 24.0)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_minutes = (hour * 60.0 + equation_of_time + 4.0 * longitude_deg) % 1440.0
    hour_angle_deg = true_solar_minutes / 4.0 - 180.0
    lat = math.radians(latitude_deg)
    hour_angle = math.radians(hour_angle_deg)
    cos_zenith = (
        math.sin(lat) * math.sin(declination)
        + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    )
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    azimuth = math.degrees(
        math.atan2(
            math.sin(hour_angle),
            math.cos(hour_angle) * math.sin(lat) - math.tan(declination) * math.cos(lat),
        )
    )
    return zenith, (azimuth + 180.0) % 360.0


def relative_azimuth(view_azimuth_deg, solar_azimuth_deg):
    """Unsigned relative azimuth in [0, 180] degrees."""
    import numpy as np

    return np.abs((np.asarray(view_azimuth_deg) - solar_azimuth_deg + 180.0) % 360.0 - 180.0)
