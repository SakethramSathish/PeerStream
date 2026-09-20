"""Errors raised by the DHT.

The split follows the one the rest of the client already uses (TRD §43): a
:class:`KrpcError` means "that packet is not Kademlia", which is terminal for
*that datagram* and never for the node — a DHT is a network of strangers, and
one stranger sending nonsense is a Tuesday, not a reason to stop. A
:class:`DhtError` covers failures in our own use of the protocol, such as
announcing with a token we were never given.
"""

from __future__ import annotations


class DhtError(Exception):
    """Base class for DHT failures."""


class KrpcError(DhtError):
    """A KRPC packet could not be decoded, or violated the protocol.

    Raised for malformed bencode, a missing transaction id or node id, a
    ``nodes`` string whose length is not a multiple of 26, and packets too large
    to arrive in one piece. None of it is fatal: the packet is dropped, and the
    node that sent it is not trusted with anything.
    """


class DhtTimeoutError(DhtError):
    """A node did not answer within the timeout, across every attempt.

    In a DHT this is the common case, not an emergency: nodes come and go
    constantly, and a silent node is usually one that has left. Callers respond
    by asking the next closest node, not by giving up.
    """


class DhtRemoteError(DhtError):
    """A node answered with a KRPC error.

    Attributes:
        code: The KRPC error code (201 generic, 202 server, 203 protocol,
            204 unknown method).
        address: Where the refusal came from, when we know it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int = 201,
        address: tuple[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.address = address


class DhtBootstrapError(DhtError):
    """The node could not join the DHT at all.

    Every bootstrap node answered nothing. Raised so a caller can say "the DHT
    is unreachable" instead of pretending the search ran and found nothing —
    those are different failures, and only one of them is the swarm's fault.
    """
