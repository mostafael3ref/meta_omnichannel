"""Tests for connector datetime parsing (no Frappe required for pure paths)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from meta_omnichannel.utils.datetimes import (
	connector_timestamp_is_present,
	parse_connector_datetime,
	parse_connector_datetime_for_db,
	utc_naive_to_system_naive,
)


class TestParseConnectorDatetime(unittest.TestCase):
	def test_none_and_empty(self):
		self.assertIsNone(parse_connector_datetime(None))
		self.assertIsNone(parse_connector_datetime(""))
		self.assertIsNone(parse_connector_datetime("   "))

	def test_iso_string(self):
		dt = parse_connector_datetime("2026-11-18T10:30:00Z")
		self.assertEqual(dt, datetime(2026, 11, 18, 10, 30, 0))

	def test_unix_seconds(self):
		dt = parse_connector_datetime(1_700_000_000)
		self.assertEqual(dt, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc).replace(tzinfo=None))

	def test_unix_seconds_production_sample(self):
		# Confirmed Connector sample from production investigation.
		dt = parse_connector_datetime(1_795_014_521)
		self.assertEqual(dt, datetime(2026, 11, 18, 15, 8, 41))

	def test_unix_milliseconds(self):
		dt = parse_connector_datetime(1_700_000_000_000)
		self.assertEqual(dt, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc).replace(tzinfo=None))

	def test_unix_numeric_string(self):
		dt = parse_connector_datetime("1795014521")
		self.assertEqual(dt, datetime(2026, 11, 18, 15, 8, 41))

	def test_invalid_string(self):
		self.assertIsNone(parse_connector_datetime("not-a-date"))

	def test_bool_rejected(self):
		self.assertIsNone(parse_connector_datetime(True))
		self.assertIsNone(parse_connector_datetime(False))

	def test_datetime_passthrough(self):
		raw = datetime(2026, 1, 2, 3, 4, 5)
		self.assertEqual(parse_connector_datetime(raw), raw)


class TestSystemTimezoneConversion(unittest.TestCase):
	def test_utc_site_unchanged(self):
		utc_naive = datetime(2026, 11, 18, 15, 8, 41)
		self.assertEqual(
			utc_naive_to_system_naive(utc_naive, system_timezone="UTC"),
			utc_naive,
		)

	def test_non_utc_site_shifts_wall_clock(self):
		utc_naive = datetime(2026, 11, 18, 15, 8, 41)
		# Africa/Cairo is UTC+2 (no DST in 2026 for this date).
		local = utc_naive_to_system_naive(utc_naive, system_timezone="Africa/Cairo")
		self.assertEqual(local, datetime(2026, 11, 18, 17, 8, 41))

	def test_parse_for_db_uses_system_timezone(self):
		dt = parse_connector_datetime_for_db(1_795_014_521, system_timezone="Africa/Cairo")
		self.assertEqual(dt, datetime(2026, 11, 18, 17, 8, 41))

	def test_parse_for_db_invalid_returns_none(self):
		self.assertIsNone(parse_connector_datetime_for_db("not-a-date", system_timezone="UTC"))
		self.assertIsNone(parse_connector_datetime_for_db(None, system_timezone="UTC"))

	def test_parse_for_db_reads_frappe_system_timezone_when_omitted(self):
		# create=True: prior tests may have stubbed frappe.utils without this attr.
		with patch(
			"frappe.utils.get_system_timezone",
			create=True,
			return_value="Africa/Cairo",
		):
			dt = parse_connector_datetime_for_db(1_795_014_521)
		self.assertEqual(dt, datetime(2026, 11, 18, 17, 8, 41))


class TestConnectorTimestampPresent(unittest.TestCase):
	def test_presence(self):
		self.assertFalse(connector_timestamp_is_present(None))
		self.assertFalse(connector_timestamp_is_present(""))
		self.assertFalse(connector_timestamp_is_present("  "))
		self.assertFalse(connector_timestamp_is_present(0))
		self.assertFalse(connector_timestamp_is_present(False))
		self.assertTrue(connector_timestamp_is_present(1795014521))
		self.assertTrue(connector_timestamp_is_present("1795014521"))
		self.assertTrue(connector_timestamp_is_present("2026-11-18T15:08:41Z"))


if __name__ == "__main__":
	unittest.main()
