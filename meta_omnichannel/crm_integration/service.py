"""Frappe CRM v1.91.1 integration helpers (CRM Lead / Contact)."""

from __future__ import annotations

import frappe
from frappe import _

from meta_omnichannel.permissions import ensure_omnichannel_agent, has_conversation_permission


def _get_conversation(name: str):
	doc = frappe.get_doc("Omnichannel Conversation", name)
	if not has_conversation_permission(doc, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return doc


@frappe.whitelist()
def link_contact(conversation: str, contact: str) -> dict:
	ensure_omnichannel_agent()
	conv = _get_conversation(conversation)
	if not frappe.db.exists("Contact", contact):
		frappe.throw(_("Contact not found"))
	conv.db_set("crm_contact", contact)
	return {"ok": True, "crm_contact": contact}


@frappe.whitelist()
def link_lead(conversation: str, lead: str) -> dict:
	ensure_omnichannel_agent()
	conv = _get_conversation(conversation)
	if not frappe.db.exists("CRM Lead", lead):
		frappe.throw(_("CRM Lead not found"))
	conv.db_set("crm_lead", lead)
	return {"ok": True, "crm_lead": lead}


@frappe.whitelist()
def create_lead_from_conversation(conversation: str) -> dict:
	ensure_omnichannel_agent()
	conv = _get_conversation(conversation)
	if conv.crm_lead and frappe.db.exists("CRM Lead", conv.crm_lead):
		return {"ok": True, "crm_lead": conv.crm_lead, "created": False}

	first_name = (conv.participant_name or "Messenger").strip().split(" ")[0] or "Messenger"
	lead = frappe.get_doc(
		{
			"doctype": "CRM Lead",
			"first_name": first_name,
			"lead_name": conv.participant_name or first_name,
			"status": "New",
			"source": _ensure_lead_source(conv.provider),
		}
	)
	# Optional fields if present on site
	if hasattr(lead, "facebook") and conv.provider == "Facebook":
		lead.facebook = conv.participant_id
	lead.insert(ignore_permissions=False)
	conv.db_set("crm_lead", lead.name)
	return {"ok": True, "crm_lead": lead.name, "created": True}


def _ensure_lead_source(provider: str) -> str | None:
	"""Return a CRM Lead Source name when the DocType exists; otherwise None."""
	if not frappe.db.exists("DocType", "CRM Lead Source"):
		return None
	label = f"Omnichannel {provider}"
	existing = frappe.db.get_value("CRM Lead Source", {"name": label}, "name")
	if existing:
		return existing
	try:
		doc = frappe.get_doc({"doctype": "CRM Lead Source", "source_name": label})
		# Some sites use `name` as source label
		if not doc.get("source_name"):
			doc = frappe.get_doc({"doctype": "CRM Lead Source", "lead_source_name": label})
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		return None


@frappe.whitelist()
def assign_conversation(conversation: str, user: str | None = None) -> dict:
	ensure_omnichannel_agent()
	conv = _get_conversation(conversation)
	user = (user or frappe.session.user).strip()
	if user and not frappe.db.exists("User", user):
		frappe.throw(_("User not found"))
	conv.db_set("assigned_to", user or None)
	if user:
		_notify_assignment(conv, user)
	return {"ok": True, "assigned_to": user}


@frappe.whitelist()
def set_conversation_status(conversation: str, status: str) -> dict:
	ensure_omnichannel_agent()
	conv = _get_conversation(conversation)
	if status not in ("Open", "Pending", "Resolved", "Closed"):
		frappe.throw(_("Invalid status"))
	conv.db_set("status", status)
	return {"ok": True, "status": status}


@frappe.whitelist()
def add_internal_note(conversation: str, note: str) -> dict:
	ensure_omnichannel_agent()
	conv = _get_conversation(conversation)
	note = (note or "").strip()
	if not note:
		frappe.throw(_("Note is required"))
	doc = frappe.get_doc(
		{
			"doctype": "Omnichannel Note",
			"conversation": conv.name,
			"note": note,
			"author": frappe.session.user,
		}
	)
	doc.insert()
	return {"ok": True, "note": doc.name}


@frappe.whitelist()
def get_customer_panel(conversation: str) -> dict:
	ensure_omnichannel_agent()
	conv = frappe.get_doc("Omnichannel Conversation", conversation)
	if not has_conversation_permission(conv, "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	contact = None
	lead = None
	if conv.crm_contact and frappe.db.exists("Contact", conv.crm_contact):
		contact = frappe.db.get_value(
			"Contact",
			conv.crm_contact,
			["name", "first_name", "last_name", "email_id", "mobile_no"],
			as_dict=True,
		)
	if conv.crm_lead and frappe.db.exists("CRM Lead", conv.crm_lead):
		lead = frappe.db.get_value(
			"CRM Lead",
			conv.crm_lead,
			["name", "lead_name", "status", "email", "mobile_no", "lead_owner"],
			as_dict=True,
		)
	notes = frappe.get_all(
		"Omnichannel Note",
		filters={"conversation": conversation},
		fields=["name", "note", "author", "creation"],
		order_by="creation desc",
		limit_page_length=20,
	)
	return {
		"conversation": {
			"name": conv.name,
			"provider": conv.provider,
			"status": conv.status,
			"assigned_to": conv.assigned_to,
			"participant_name": conv.participant_name,
			"participant_id": conv.participant_id,
		},
		"contact": contact,
		"lead": lead,
		"notes": notes,
	}


def _notify_assignment(conv, user: str) -> None:
	try:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"subject": _("Conversation assigned: {0}").format(conv.participant_name or conv.name),
				"email_content": _("You were assigned an omnichannel conversation ({0}).").format(
					conv.provider
				),
				"type": "Assignment",
				"document_type": "Omnichannel Conversation",
				"document_name": conv.name,
				"for_user": user,
				"from_user": frappe.session.user,
			}
		).insert(ignore_permissions=True)
	except Exception:
		# Notification Log shape can vary by Frappe version — never fail assignment.
		pass
