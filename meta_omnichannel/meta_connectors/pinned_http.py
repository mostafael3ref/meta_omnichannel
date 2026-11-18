"""IP-pinned HTTPS transport for connector requests.

Connects to a validated destination IP while preserving:
- TLS certificate verification (never disabled)
- SNI ``server_hostname`` = original connector hostname
- ``assert_hostname`` = original connector hostname
- HTTP ``Host`` header = original connector hostname (and port if non-default)

Proxies are refused (``trust_env=False``, empty proxy map, adapter guard).
"""

from __future__ import annotations

from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

from meta_omnichannel.meta_connectors.errors import ConnectorError
from meta_omnichannel.meta_connectors.ssrf import SafeDestination, format_ip_for_url


class HostnameTLSAdapter(HTTPAdapter):
	"""HTTPS adapter that forces TLS hostname checks for a logical hostname."""

	def __init__(self, server_hostname: str, *args, **kwargs) -> None:
		self.server_hostname = server_hostname
		super().__init__(*args, **kwargs)

	def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
		pool_kwargs["assert_hostname"] = self.server_hostname
		pool_kwargs["server_hostname"] = self.server_hostname
		self.poolmanager = PoolManager(
			num_pools=connections,
			maxsize=maxsize,
			block=block,
			**pool_kwargs,
		)

	def proxy_manager_for(self, proxy, **proxy_kwargs):  # noqa: ARG002
		raise ConnectorError(
			"HTTP proxies are not allowed for connector requests",
			connection_status="Misconfigured",
		)


def create_pinned_session(server_hostname: str) -> requests.Session:
	"""Create a session that ignores env proxies and pins TLS to ``server_hostname``."""
	session = requests.Session()
	session.trust_env = False
	session.proxies = {"http": None, "https": None}
	session.mount("https://", HostnameTLSAdapter(server_hostname=server_hostname))
	return session


def build_pinned_request_url(destination: SafeDestination, path: str) -> str:
	"""Build ``https://<validated-ip>[:port]/...`` URL for the TCP peer."""
	ip_netloc = format_ip_for_url(destination.ip)
	if destination.port != 443:
		netloc = f"{ip_netloc}:{destination.port}"
	else:
		netloc = ip_netloc

	base = f"https://{netloc}{destination.base_path}"
	return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def pinned_request_headers(
	destination: SafeDestination,
	base_headers: dict[str, str],
) -> dict[str, str]:
	"""Merge caller headers with the logical Host header (never the IP)."""
	headers = dict(base_headers)
	headers["Host"] = destination.host_header
	return headers


def assert_tls_verify_enabled(kwargs: dict) -> None:
	"""Fail closed if a caller attempts to disable TLS verification."""
	if "verify" in kwargs and kwargs["verify"] is False:
		raise ConnectorError(
			"TLS certificate verification cannot be disabled",
			connection_status="Misconfigured",
		)
