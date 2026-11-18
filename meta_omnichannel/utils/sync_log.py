"""Shared Omnichannel Sync Log helpers."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import now_datetime


def start_sync_log(provider: str, job_type: str, **links) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Omnichannel Sync Log",
			"provider": provider,
			"job_type": job_type,
			"status": "Running",
			"started_at": now_datetime(),
			**links,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def finish_sync_log(
	name: str,
	*,
	status: str,
	processed: int = 0,
	error: str | None = None,
	details: Any = None,
) -> None:
	frappe.db.set_value(
		"Omnichannel Sync Log",
		name,
		{
			"status": status,
			"finished_at": now_datetime(),
			"records_processed": processed,
			"error_summary": (error or "")[:440],
			"details_json": json.dumps(details or {}, default=str)[:100000],
		},
		update_modified=True,
	)
