"""WhatsApp Business provider — internal models ready; connector APIs pending."""

from __future__ import annotations

from meta_omnichannel.providers.base import ChannelProvider, ProviderCapability


class WhatsAppProvider(ChannelProvider):
	provider_name = "WhatsApp"
	capabilities = {
		ProviderCapability.LIST_CONVERSATIONS: None,
		ProviderCapability.GET_MESSAGES: None,
		ProviderCapability.SEND_TEXT: None,
		ProviderCapability.SEND_MEDIA: None,
		ProviderCapability.SEND_TEMPLATE: None,
		ProviderCapability.OAUTH_CONNECT: None,
	}

	# Design note: 24-hour customer service window should be enforced once send APIs exist.
	customer_service_window_hours = 24

	def is_external_enabled(self) -> bool:
		return False

	def is_within_service_window(self, last_inbound_at) -> bool | None:
		"""Return None until WhatsApp messaging APIs are available."""
		return None
