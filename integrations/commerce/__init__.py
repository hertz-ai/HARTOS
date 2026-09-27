"""McGroce agentic commerce: tools, client, HTTP surface.

Importing the package registers the ``mcgroce_order`` AP2 payment hook
(commerce_tools), so an approval answered anywhere in the process settles the
McGroce order.
"""
from integrations.commerce import bindings, commerce_tools  # noqa: F401
from integrations.commerce.commerce_tools import (  # noqa: F401
    COMMERCE_TOOLS, register_commerce_tools,
)
