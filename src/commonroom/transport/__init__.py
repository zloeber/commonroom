"""Transport plugin boundary.

Importing this package loads the interface and the loopback plugin.
Vendor plugins are loaded only when ``create_transport`` asks for them.
"""

from commonroom.transport.base import CAPABILITY_NAMES, Connection, Transport
from commonroom.transport.loopback import LoopbackTransport
from commonroom.transport.registry import (
    connect_invitation,
    create_transport,
    register_transport,
    registered_transports,
)

__all__ = [
    "CAPABILITY_NAMES",
    "Connection",
    "LoopbackTransport",
    "Transport",
    "connect_invitation",
    "create_transport",
    "register_transport",
    "registered_transports",
]
