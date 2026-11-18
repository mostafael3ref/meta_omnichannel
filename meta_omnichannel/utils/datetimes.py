"""Connector datetime parsing helpers (pure where possible)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_connector_datetime(value: Any) -> datetime | None:
	"""Convert a connector timestamp into a naive ``datetime``, or ``None``.

	Supports:

	- ``datetime`` objects (tz-aware values are converted to naive UTC)
	- Unix epoch seconds or milliseconds (``int`` / ``float``)
	- Numeric strings treated as Unix epoch
	- ISO-8601 / Frappe-parseable date strings

	Does **not** pass values through ``get_datetime(None)`` (which means "now").
	Invalid or empty inputs return ``None`` and never raise.
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
