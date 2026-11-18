"""HTTPS connector URL validation and SSRF hardening.

Security model
--------------
1. Hostname must be on the **code-level** allowlist (not user-expandable).
2. DNS is resolved once; every returned address is checked against blocked
   ranges (loopback, private, link-local, metadata, multicast, reserved).
3. ``ConnectorClient`` connects to a **validated IP** (pinned), not to the
   hostname, so a later DNS rebinding cannot change the TCP peer.
4. TLS still uses the original hostname for SNI, ``Host``, and certificate
   verification (see ``pinned_http.py``).

Remaining trust assumptions (not eliminated by application code alone)
---------------------------------------------------------------------
- DNS answers for allowlisted names are trusted *at resolve time*. A
  compromised resolver/registry could point the allowlisted hostname at an
  attacker-controlled **public** IP. Pinning prevents that answer from
  flipping to a *private/metadata* address after validation; it does not
  prove the public peer is the legitimate connector.
- TLS authenticity still depends on PKI (certificate for the allowlisted
  hostname).
- Operators should add network egress allowlists (firewall/DNS) to the
  connector IP/hostname as defense in depth.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from meta_omnichannel.meta_connectors.errors import ConnectorURLError
from meta_omnichannel.utils.constants import ALLOWED_CONNECTOR_HOSTS

__all__ = [
	"ConnectorURLError",
	"SafeDestination",
	"format_ip_for_url",
	"is_blocked_ip",
	"resolve_safe_destination",
	"validate_connector_base_url",
]

_BLOCKED_NETWORKS = (
	ipaddress.ip_network("0.0.0.0/8"),
	ipaddress.ip_network("10.0.0.0/8"),
	ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
	ipaddress.ip_network("127.0.0.0/8"),
	ipaddress.ip_network("169.254.0.0/16"),  # link-local + cloud metadata
	ipaddress.ip_network("172.16.0.0/12"),
	ipaddress.ip_network("192.0.0.0/24"),
	ipaddress.ip_network("192.0.2.0/24"),
	ipaddress.ip_network("192.168.0.0/16"),
	ipaddress.ip_network("198.18.0.0/15"),
	ipaddress.ip_network("198.51.100.0/24"),
	ipaddress.ip_network("203.0.113.0/24"),
	ipaddress.ip_network("224.0.0.0/4"),
	ipaddress.ip_network("240.0.0.0/4"),
	ipaddress.ip_network("::/128"),
	ipaddress.ip_network("::1/128"),
	ipaddress.ip_network("fc00::/7"),
	ipaddress.ip_network("fe80::/10"),
	ipaddress.ip_network("ff00::/8"),
)

_BLOCKED_HOSTNAMES = frozenset(
	{
		"localhost",
		"localhost.localdomain",
		"metadata.google.internal",
		"metadata",
	}
)


@dataclass(frozen=True)
class SafeDestination:
	"""Validated connector destination used for a single pinned request."""

	hostname: str
	port: int
	ip: str
	base_path: str
	normalized_url: str

	@property
	def host_header(self) -> str:
		"""RFC Host header value (include non-default port)."""
		if self.port == 443:
			return self.hostname
		return f"{self.hostname}:{self.port}"


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
	"""Return True if the IP must not be used as a connector destination."""
	if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
		return True
	if ip.is_reserved or ip.is_unspecified:
		return True
	if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
		return is_blocked_ip(ip.ipv4_mapped)
	for network in _BLOCKED_NETWORKS:
		if ip in network:
			return True
	return False


def format_ip_for_url(ip: str) -> str:
	"""Format an IP for use in a URL netloc (bracket IPv6)."""
	addr = ipaddress.ip_address(ip)
	if isinstance(addr, ipaddress.IPv6Address):
		return f"[{addr.compressed}]"
	return str(addr)


def _resolve_host_ips(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
	try:
		infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
	except socket.gaierror as exc:
		raise ConnectorURLError(f"DNS resolution failed for host '{hostname}'") from exc

	addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
	seen: set[str] = set()
	for info in infos:
		ip_str = info[4][0]
		if ip_str in seen:
			continue
		seen.add(ip_str)
		try:
			addresses.append(ipaddress.ip_address(ip_str))
		except ValueError as exc:
			raise ConnectorURLError(f"Invalid resolved address for host '{hostname}'") from exc

	if not addresses:
		raise ConnectorURLError(f"No addresses resolved for host '{hostname}'")
	return addresses


def _parse_https_base(url: str):
	if not url or not str(url).strip():
		raise ConnectorURLError("URL is required")

	normalized = str(url).strip().rstrip("/")
	parsed = urlparse(normalized)

	if parsed.scheme.lower() != "https":
		raise ConnectorURLError("URL must use HTTPS")

	if not parsed.hostname:
		raise ConnectorURLError("URL must include a hostname")

	if parsed.username is not None or parsed.password is not None:
		raise ConnectorURLError("URL must not contain embedded credentials")

	if parsed.query or parsed.fragment:
		raise ConnectorURLError("Base URL must not include query string or fragment")

	hostname = parsed.hostname.lower().rstrip(".")
	if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
		raise ConnectorURLError("Hostname is not allowed")

	# Literal IPs cannot provide a stable TLS hostname for our connector model.
	try:
		ipaddress.ip_address(hostname)
	except ValueError:
		pass
	else:
		raise ConnectorURLError("Connector URL must use a hostname, not a literal IP")

	if hostname not in ALLOWED_CONNECTOR_HOSTS:
		raise ConnectorURLError("Connector hostname is not on the allowed host list")

	port = parsed.port or 443
	path = parsed.path.rstrip("/") if parsed.path and parsed.path != "/" else ""
	netloc = hostname if port == 443 else f"{hostname}:{port}"
	normalized_url = f"https://{netloc}{path}"
	return hostname, port, path, normalized_url


def validate_connector_base_url(url: str, *, resolve_dns: bool = True) -> str:
	"""Validate and normalize a connector base URL (allowlist + optional DNS)."""
	hostname, _port, _path, normalized_url = _parse_https_base(url)

	if resolve_dns:
		for ip in _resolve_host_ips(hostname):
			if is_blocked_ip(ip):
				raise ConnectorURLError(
					f"Host '{hostname}' resolves to a blocked address ({ip})"
				)

	return normalized_url


def resolve_safe_destination(url: str) -> SafeDestination:
	"""Resolve DNS once and return a destination pinned to a validated IP.

	Fails closed if **any** resolved address is blocked. When both families are
	present and safe, prefer a public IPv4 pin (IPv6-only destinations still
	use IPv6).
	"""
	hostname, port, path, normalized_url = _parse_https_base(url)
	addresses = _resolve_host_ips(hostname)

	for ip in addresses:
		if is_blocked_ip(ip):
			raise ConnectorURLError(f"Host '{hostname}' resolves to a blocked address ({ip})")

	ipv4 = next((ip for ip in addresses if isinstance(ip, ipaddress.IPv4Address)), None)
	chosen = ipv4 if ipv4 is not None else addresses[0]
	return SafeDestination(
		hostname=hostname,
		port=port,
		ip=str(chosen),
		base_path=path,
		normalized_url=normalized_url,
	)
