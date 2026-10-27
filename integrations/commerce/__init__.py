"""McGroce agentic-commerce connector.

    bindings        user_id <-> McGroce customer (single writer of
                    commerce_bindings.json)
    mcgroce_client  REST I/O only (Basic service account + customerId from
                    the binding, https, circuit breaker)
    commerce_tools  the agent tools (Tier-2, goal tag 'commerce'); never on
                    the MCP bridge, whose caller is not a shopper
    signing         the MACs on every McGroce <-> HARTOS message (session
                    exchange, consumed-mandate attestation)
    commerce_api    Flask blueprint /api/commerce/* (session exchange,
                    mandate status, health)

Payments stay in integrations.ap2 (PaymentLedger + ap2_mandate); this
package never moves money itself.
"""

# The approval actions this package answers (ap2_pay:<payment_id>,
# merchant_onboard:/merchant_sku:<draft_id>).  Defined here, in the
# import-free package root, so the approval route can still recognise -- and
# refuse -- one when integrations.commerce.approvals itself fails to import.
COMMERCE_ACTION_PREFIXES = ('ap2_pay:', 'merchant_onboard:', 'merchant_sku:')
