# Copyright (c) 2026, Meta Omnichannel and contributors
# License: MIT. See license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class OmnichannelMessage(Document):
	def validate(self):
		"""Enforce provider/channel consistency with Conversation and Channel.

		Never auto-corrects ``provider``, ``channel``, or ``conversation`` —
		mismatches raise ``ValidationError``.

		Compatibility: saving legacy inconsistent rows (e.g. wrong provider for
		the linked conversation) will fail until those fields are corrected
		explicitly. ``frappe.db.set_value`` bypasses this hook; ``insert`` /
		``save`` do not, including when ``ignore_permissions=True``.
		"""
		validate_message_provider_channel_consistency(self)


def validate_message_provider_channel_consistency(doc) -> None:
	"""Shared consistency checks for Omnichannel Message link fields."""
	conversation_name = (doc.conversation or "").strip()
	if not conversation_name:
		frappe.throw(_("Conversation is required"), frappe.ValidationError)

	conversation = frappe.db.get_value(
		"Omnichannel Conversation",
		conversation_name,
		["name", "provider", "channel"],
		as_dict=True,
	)
	if not conversation:
		frappe.throw(
			_("Omnichannel Conversation {0} does not exist").format(conversation_name),
			frappe.ValidationError,
		)

	if doc.provider != conversation.provider:
		frappe.throw(
			_(
				"Message provider ({0}) does not match conversation provider ({1})"
			).format(doc.provider, conversation.provider),
			frappe.ValidationError,
		)

	if doc.channel != conversation.channel:
		frappe.throw(
			_(
				"Message channel ({0}) does not match conversation channel ({1})"
			).format(doc.channel, conversation.channel),
			frappe.ValidationError,
		)

	channel_name = (doc.channel or "").strip()
	if not channel_name:
		frappe.throw(_("Channel is required"), frappe.ValidationError)

	channel = frappe.db.get_value(
		"Omnichannel Channel",
		channel_name,
		["name", "provider"],
		as_dict=True,
	)
	if not channel:
		frappe.throw(
			_("Omnichannel Channel {0} does not exist").format(channel_name),
			frappe.ValidationError,
		)

	if channel.provider != doc.provider:
		frappe.throw(
			_(
				"Channel provider ({0}) does not match message provider ({1})"
			).format(channel.provider, doc.provider),
			frappe.ValidationError,
		)
