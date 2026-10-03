"""McGroce agentic-commerce connector.

    bindings        user_id <-> McGroce customer (single writer of
                    commerce_bindings.json)
    mcgroce_client  REST I/O only (Basic service account + customerId from
                    the binding, https, circuit breaker)
    commerce_tools  the agent tools (Tier-2, goal tag 'commerce') and the
                    COMMERCE_TOOLS list the MCP bridge exposes
    commerce_api    Flask blueprint /api/commerce/* (session exchange,
                    mandate status, health)

Payments stay in integrations.ap2 (PaymentLedger + ap2_mandate); this
package never moves money itself.
"""
