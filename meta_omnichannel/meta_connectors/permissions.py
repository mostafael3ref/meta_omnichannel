"""Authorization helpers for Omnichannel administration."""

from __future__ import annotations

import frappe
from frappe import _

OMNICHANNEL_ADMIN_ROLES = ("System Manager", "Omnichannel Administrator")


def user_is_omnichannel_admin(user: str | None = None) -> bool:
	"""Return whether the user may manage connector settings / test connectivity."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	return bool(roles.intersection(OMNICHANNEL_ADMIN_ROLES))


def ensure_omnichannel_admin() -> None:
	"""Raise PermissionError when the current user is not an Omnichannel admin."""
	if user_is_omnichannel_admin():
		return
	frappe.throw(
		_("Only System Manager or Omnichannel Administrator can manage the connector"),
		frappe.PermissionError,
	)
