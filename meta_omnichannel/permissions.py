"""DocType permission helpers for Omnichannel records."""

from __future__ import annotations

import frappe
from frappe import _

from meta_omnichannel.meta_connectors.permissions import (
	OMNICHANNEL_ADMIN_ROLES,
	user_is_omnichannel_admin,
)

AGENT_ROLES = OMNICHANNEL_ADMIN_ROLES + ("Omnichannel Agent",)


def user_is_omnichannel_agent(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user_is_omnichannel_admin(user):
		return True
	return "Omnichannel Agent" in set(frappe.get_roles(user))


def ensure_omnichannel_agent() -> None:
	if user_is_omnichannel_agent():
		return
	frappe.throw(
		_("Only Omnichannel agents or administrators can access the inbox"),
		frappe.PermissionError,
	)


def conversation_query(user: str | None = None) -> str:
	"""Restrict non-admin agents to conversations assigned to them or unassigned."""
	user = user or frappe.session.user
	if user_is_omnichannel_admin(user):
		return ""
	user_esc = frappe.db.escape(user)
	return (
		f"(`tabOmnichannel Conversation`.assigned_to = {user_esc} "
		f"or ifnull(`tabOmnichannel Conversation`.assigned_to, '') = '')"
	)


def message_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user_is_omnichannel_admin(user):
		return ""
	user_esc = frappe.db.escape(user)
	return (
		"`tabOmnichannel Message`.conversation in ("
		"select name from `tabOmnichannel Conversation` "
		f"where assigned_to = {user_esc} or ifnull(assigned_to, '') = '')"
	)


def has_conversation_permission(doc, ptype: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user_is_omnichannel_admin(user):
		return True
	if not user_is_omnichannel_agent(user):
		return False
	assigned = doc.get("assigned_to") if hasattr(doc, "get") else None
	if not assigned or assigned == user:
		return True
	return False
