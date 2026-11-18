"""Provider registry for Facebook / Instagram / WhatsApp."""

from __future__ import annotations

from meta_omnichannel.instagram.provider import InstagramProvider
from meta_omnichannel.whatsapp.provider import WhatsAppProvider


class FacebookProviderStatus:
	"""Facebook is implemented against confirmed connector routes."""

	provider_name = "Facebook"
	capabilities = {
		"list_conversations": "/api/facebook/conversations",
		"get_messages": "/api/facebook/conversations/{page_id}/{sender_id}/messages",
		"send_text": "/api/facebook/messages/send",
	}

	def is_external_enabled(self) -> bool:
		return True

	def missing_contracts(self) -> list[str]:
		return []

	def status(self) -> dict:
		return {
			"provider": self.provider_name,
			"external_enabled": True,
			"capabilities": self.capabilities,
			"missing_contracts": [],
		}


def get_provider_statuses() -> list[dict]:
	return [
		FacebookProviderStatus().status(),
		InstagramProvider().status(),
		WhatsAppProvider().status(),
	]
