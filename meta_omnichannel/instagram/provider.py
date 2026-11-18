"""Instagram Direct provider — list conversations enabled (Step 1)."""

from __future__ import annotations

from meta_omnichannel.providers.base import ChannelProvider, ProviderCapability


class InstagramProvider(ChannelProvider):
	provider_name = "Instagram"
	capabilities = {
		ProviderCapability.LIST_CONVERSATIONS: "/api/instagram/conversations",
		ProviderCapability.GET_MESSAGES: None,  # history disabled until path/schema confirmed
		ProviderCapability.SEND_TEXT: None,  # outbound disabled in Step 1
		ProviderCapability.SEND_MEDIA: None,
		ProviderCapability.OAUTH_CONNECT: None,
	}

	def is_external_enabled(self) -> bool:
		"""True when at least list-conversations is wired to the connector."""
		return bool(self.capabilities.get(ProviderCapability.LIST_CONVERSATIONS))
