"""Pure Python mirror of frontend Frappe response unwrap contract (for offline tests)."""

from __future__ import annotations

import json
from typing import Any


def unwrap_frappe_message(data: dict[str, Any] | None, *, ok: bool = True) -> Any:
	"""Match frontend: keep ``message`` unless HTTP failed or ``exc`` is set."""
	data = data or {}
	if not ok or data.get("exc"):
		message = data.get("exc") or "Request failed"
		raw = data.get("_server_messages") or "[]"
		try:
			msgs = json.loads(raw)
			if msgs:
				message = json.loads(msgs[0]).get("message") or message
		except Exception:
			pass
		if data.get("exception"):
			message = data["exception"]
		raise ValueError(str(message))
	return data.get("message")
