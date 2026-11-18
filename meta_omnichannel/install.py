"""Install / uninstall hooks for Meta Omnichannel CRM."""

from __future__ import annotations

import frappe

ROLES = (
	"Omnichannel Administrator",
	"Omnichannel Agent",
)


def _ensure_roles() -> None:
	for role_name in ROLES:
		if frappe.db.exists("Role", role_name):
			continue
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 1,
				"is_custom": 1,
			}
		).insert(ignore_permissions=True)


def after_install() -> None:
	_ensure_roles()
	frappe.logger("meta_omnichannel").info(
		"meta_omnichannel installed — configure Omnichannel Settings before use"
	)


def after_migrate() -> None:
	_ensure_roles()


def after_uninstall() -> None:
	frappe.logger("meta_omnichannel").info("meta_omnichannel uninstalled")
