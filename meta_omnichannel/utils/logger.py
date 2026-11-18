"""Logging helpers for meta_omnichannel."""

from __future__ import annotations


def get_logger():
	"""Return the app-scoped Frappe logger."""
	import frappe

	return frappe.logger("meta_omnichannel")
