"""A local stand-in for the Gecko in.touch2 cloud service.

Speaks the in.touch2 UDP dialect on port 10022, answers the EN module's
keepalives so its status LED stays blue, and relays traffic between the app and
the spa. Standard library only, so it runs on a box with no internet.
"""

__version__ = "1.0.0"

__all__ = ["protocol", "relay", "poller", "web", "__version__"]
