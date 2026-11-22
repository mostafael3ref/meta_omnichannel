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


def has_conversation_permission(
	doc, ptype: str, user: str | None = None, debug: bool = False
) -> bool:
	# ``debug`` accepted for Frappe v15 has_permission hook kwargs; unused.
	user = user or frappe.session.user
	if user_is_omnichannel_admin(user):
		return True
	if not user_is_omnichannel_agent(user):
		return False
	assigned = doc.get("assigned_to") if hasattr(doc, "get") else None
	if not assigned or assigned == user:
		return True
	return False


def has_message_permission(
	doc, ptype: str, user: str | None = None, debug: bool = False
) -> bool:
	"""Conversation-scoped access for Omnichannel Message documents.

	Resolves the parent conversation and reuses :func:`has_conversation_permission`
	so assignment rules stay in one place. Missing/invalid conversation links deny
	access.

	Frappe ``has_permission`` hooks only **deny** (return ``False``); returning
	``True`` does not grant rights beyond DocType role permissions. This helper
	therefore scopes which messages a user may touch but does not invent
	create/write/delete — those still require the Omnichannel Message role table.
	Does not call ``frappe.has_permission`` on the conversation (avoids hook recursion).
	"""
	user = user or frappe.session.user
	conversation_name = ""
	if hasattr(doc, "get"):
		conversation_name = str(doc.get("conversation") or "").strip()
	elif getattr(doc, "conversation", None):
		conversation_name = str(doc.conversation).strip()
	if not conversation_name:
		return False

	# Permission-free lookup — never frappe.has_permission / get_doc permission path.
	conversation = frappe.db.get_value(
		"Omnichannel Conversation",
		conversation_name,
		["name", "assigned_to"],
		as_dict=True,
	)
	if not conversation:
		return False

	# Normalize to a mapping with ``.get`` so has_conversation_permission works for
	# both frappe._dict rows and plain attribute objects (no duplicated rules).
	if hasattr(conversation, "get"):
		conv_doc = conversation
	else:
		conv_doc = {
			"name": getattr(conversation, "name", None),
			"assigned_to": getattr(conversation, "assigned_to", None),
		}
	return has_conversation_permission(conv_doc, ptype, user=user, debug=debug)
