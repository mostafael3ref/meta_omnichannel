"""CRM sync surface — delegates to service helpers."""

from __future__ import annotations

from meta_omnichannel.crm_integration.service import (
	assign_conversation,
	create_lead_from_conversation,
	link_contact,
	link_lead,
)

__all__ = [
	"assign_conversation",
	"create_lead_from_conversation",
	"link_contact",
	"link_lead",
]
