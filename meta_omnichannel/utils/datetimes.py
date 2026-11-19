"""Connector datetime parsing helpers (pure where possible)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_connector_datetime(value: Any) -> datetime | None:
	"""Convert a connector timestamp into a naive **UTC** ``datetime``, or ``None``.

	Supports:

	- ``datetime`` objects (tz-aware values are converted to naive UTC)
	- Unix epoch seconds or milliseconds (``int`` / ``float``)
	- Numeric strings treated as Unix epoch
	- ISO-8601 / Frappe-parseable date strings

	Does **not** pass values through ``get_datetime(None)`` (which means "now").
	Invalid or empty inputs return ``None`` and never raise.

	For Frappe DocType Datetime storage (same convention as ``now_datetime()``),
	use :func:`parse_connector_datetime_for_db`.
	"""
	if value is None:
		return None

	if isinstance(value, datetime):
		if value.tzinfo is not None:
			return value.astimezone(timezone.utc).replace(tzinfo=None)
		return value

	# bool is a subclass of int — reject explicitly
	if isinstance(value, bool):
		return None

	if isinstance(value, (int, float)):
		return _from_unix_epoch(float(value))

	if isinstance(value, str):
		text = value.strip()
		if not text:
			return None
		if _looks_like_unix_numeric(text):
			try:
				return _from_unix_epoch(float(text))
			except ValueError:
				return None
		normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
		try:
			parsed = datetime.fromisoformat(normalized)
			if parsed.tzinfo is not None:
				return parsed.astimezone(timezone.utc).replace(tzinfo=None)
			return parsed
		except ValueError:
			pass
		try:
			from frappe.utils import get_datetime

			parsed = get_datetime(text)
			if parsed is None or not isinstance(parsed, datetime):
				return None
			if parsed.tzinfo is not None:
				return parsed.astimezone(timezone.utc).replace(tzinfo=None)
			return parsed
		except Exception:
			return None

	return None


def _looks_like_unix_numeric(text: str) -> bool:
	if text.isdigit():
		return True
	if text.count(".") == 1:
		left, right = text.split(".", 1)
		return left.isdigit() and right.isdigit()
	return False


def _from_unix_epoch(ts: float) -> datetime | None:
	if ts <= 0:
		return None
	# Heuristic: values >= 1e12 are milliseconds since epoch.
	if ts >= 1_000_000_000_000:
		ts = ts / 1000.0
	try:
		return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
	except (OverflowError, OSError, ValueError):
		return None


def _resolve_system_timezone_name(system_timezone: str | None = None) -> str:
	"""Return the Frappe system timezone name (same source as ``now_datetime()``)."""
	if system_timezone:
		return system_timezone
	from frappe.utils import get_system_timezone

	return str(get_system_timezone())


def utc_naive_to_system_naive(
	dt: datetime, *, system_timezone: str | None = None
) -> datetime:
	"""Convert a connector UTC-naive (or tz-aware) datetime to Frappe DB convention.

	Frappe v15 ``now_datetime()`` stores wall time in the site/system timezone as a
	**naive** ``datetime``. Database Datetime fields follow that same convention.
	Connector epochs / ``Z`` timestamps are UTC — convert before write so inbound
	``sent_at`` aligns with ERPNext outbound timestamps.
	"""
	if dt.tzinfo is None:
		aware_utc = dt.replace(tzinfo=timezone.utc)
	else:
		aware_utc = dt.astimezone(timezone.utc)
	tz_name = _resolve_system_timezone_name(system_timezone)
	try:
		zone = ZoneInfo(tz_name)
	except ZoneInfoNotFoundError:
		zone = timezone.utc
	return aware_utc.astimezone(zone).replace(tzinfo=None)


def parse_connector_datetime_for_db(
	value: Any, *, system_timezone: str | None = None
) -> datetime | None:
	"""Parse a connector timestamp and convert to naive system-timezone for DB storage.

	Returns ``None`` for missing/invalid input (never raises, never means "now").
	"""
	parsed = parse_connector_datetime(value)
	if parsed is None:
		return None
	return utc_naive_to_system_naive(parsed, system_timezone=system_timezone)


def connector_timestamp_is_present(value: Any) -> bool:
	"""True when the connector supplied a non-empty timestamp candidate."""
	if value is None or isinstance(value, bool):
		return False
	if isinstance(value, str):
		return bool(value.strip())
	# int/float/datetime — including numeric 0 treated as absent/invalid upstream
	if isinstance(value, (int, float)):
		return value != 0
	return True
