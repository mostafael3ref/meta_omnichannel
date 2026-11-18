"""Shared Meta / connector services.

Phase 1: ``ConnectorClient`` talks to the external FastAPI connector over HTTPS.
"""

from meta_omnichannel.meta_connectors.client import ConnectorClient, ConnectorTestResult

__all__ = ["ConnectorClient", "ConnectorTestResult"]
