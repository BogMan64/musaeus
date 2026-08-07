"""Network-policy boundary for MUSAEUS command planning.

P0 preview uses this small injected boundary rather than reaching a transport
client directly.  The default policy is local-only and rejects all dispatches.
Future provider-enabled modes must supply their own explicitly authorised policy;
they cannot inherit preview's default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class NetworkAccessDenied(RuntimeError):
    """Raised when a policy denies a transport dispatch."""


@dataclass(frozen=True)
class PreviewNetworkPolicy:
    """Immutable policy facts that a preview may safely report."""

    name: str
    external_lookup_permitted: bool


class NetworkPolicyGateway(Protocol):
    """The sole planning-time interface to outbound transport policy."""

    def preview_policy(self) -> PreviewNetworkPolicy:
        """Return the policy governing a requested preview without dispatching."""

    def dispatch(self, destination: str) -> None:
        """Dispatch outbound work only when an explicit future policy permits it."""


class LocalOnlyNetworkPolicyGateway:
    """Default preview gateway: local inspection only, no outbound transport."""

    _POLICY = PreviewNetworkPolicy(name="local_only", external_lookup_permitted=False)

    def preview_policy(self) -> PreviewNetworkPolicy:
        """Report the policy without resolving a host or creating a transport."""
        return self._POLICY

    def dispatch(self, destination: str) -> None:
        """Reject every transport attempt before a connection can be attempted."""
        raise NetworkAccessDenied(
            f"Network access is denied by the local-only policy: {destination!r}"
        )
