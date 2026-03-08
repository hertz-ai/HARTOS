# E-Commerce Demo with AP2 (Agent Protocol 2)

**Real-world demonstration of AP2 agentic commerce in an e-commerce workflow**

---

## Overview

This demo showcases AP2 (Agent Protocol 2) - the payment protocol for agentic commerce - integrated into a realistic e-commerce scenario.

### What is AP2?

AP2 (Agent Protocol 2) enables agents to:
- Request payments as part of their workflows
- Manage payment authorization and processing
- Track payment transactions with full audit trail
- Coordinate multi-agent payment workflows
- Integrate with payment gateways (Stripe, PayPal, etc.)

---

## Demo Files

### 1. `demo_ecommerce_ap2_simple.py` ⭐ **START HERE**

**No LLM required** - Perfect for testing and understanding AP2

A scripted demonstration that shows the complete e-commerce workflow:
1. Product search and selection
2. Inventory check and reservation
3. **Payment processing with AP2** (request → authorize → process)
4. Shipping calculation
5. Order confirmation

**Run it:**
```bash
python demo_ecommerce_ap2_simple.py
```

**Output:** Complete visual walkthrough of:
- Task status tracking
- AP2 payment lifecycle
- Transaction details
- Order confirmation

### 2. `demo_ecommerce_ap2.py`

**Requires OpenAI API** - Full multi-agent LLM version

A complete multi-agent system using AutoGen with:
- **Customer Service Agent** - Handles customer inquiries
- **Inventory Agent** - Manages stock and reservations
- **Payment Agent** - Processes payments using AP2
- **Shipping Agent** - Calculates shipping and delivery
- **User Proxy** - Simulates customer

**Run it:**
```bash
# Set your OpenAI API key first
export OPENAI_API_KEY=your_key_here  # Linux/Mac
set OPENAI_API_KEY=your_key_here     # Windows

python demo_ecommerce_ap2.py
```

---

## E-Commerce Workflow

### Scenario
Customer wants to purchase a high-performance laptop for software development.

### Workflow Steps

```
┌─────────────────────────────────────────────────────┐
│  STEP 1: Product Search                           │
│  Agent: Customer Service                           │
│  Action: Search catalog for suitable laptops       │
└─────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────┐
│  STEP 2: Inventory Check                          │
│  Agent: Inventory Manager                          │
│  Action: Verify product availability               │
└─────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────┐
│  STEP 3: Reserve Inventory                        │
│  Agent: Inventory Manager                          │
│  Action: Reserve product for customer              │
└─────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────┐
│  STEP 4: Calculate Shipping                       │
│  Agent: Shipping Coordinator                       │
│  Action: Calculate shipping cost to destination    │
└─────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────┐
│  STEP 5: Process Payment (AP2)                    │
│  Agent: Payment Processor                          │
│  Actions:                                          │
│    5a. Create payment request                      │
│    5b. Authorize payment                           │
│    5c. Process through gateway                     │
└─────────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────┐
│  STEP 6: Confirm Order                            │
│  Agent: Customer Service                           │
│  Action: Send order confirmation to customer       │
└─────────────────────────────────────────────────────┘
```

---

## AP2 Payment Flow (Step 5 Detail)

The Payment Processor agent uses AP2 for secure payment handling:

### Step 5a: Create Payment Request
```python
payment = payment_ledger.create_payment_request(
    amount=total_amount,
    currency="USD",
    description="Purchase of Professional Laptop Pro 15 + shipping",
    requester_agent_id="PaymentProcessor",
    payment_method=PaymentMethod.CREDIT_CARD,
    gateway=PaymentGateway.MOCK,
    metadata={
        'customer_id': 'CUST001',
        'product_id': 'laptop_pro_15',
        'quantity': 1
    }
)

# Status: PENDING
# Payment ID: Generated UUID
```

### Step 5b: Authorize Payment
```python
success = payment_ledger.authorize_payment(
    payment_id=payment.payment_id,
    approver_id="customer_CUST001"
)

# Status: PENDING → AUTHORIZED
# Approval chain: Tracks who authorized
```

### Step 5c: Process Payment
```python
result = payment_ledger.process_payment(payment_id)

# Status: AUTHORIZED → PROCESSING → COMPLETED
# Gateway transaction ID: Generated
# Amount charged: Confirmed
```

---

## Key Features Demonstrated

### ✅ AP2 (Agent Protocol 2)
- **Payment Requests**: Agents can request payments
- **Authorization Workflow**: Approval chain tracking
- **Gateway Integration**: Mock gateway (extensible to Stripe, PayPal)
- **Transaction Ledger**: Persistent payment history
- **Payment Metadata**: Track order details
- **Multi-step Lifecycle**: PENDING → AUTHORIZED → PROCESSING → COMPLETED

### ✅ Agent Ledger (Task Tracking)
- **Task Management**: Track all workflow steps
- **Status Updates**: Monitor progress in real-time
- **Completion Tracking**: Know when workflow is done
- **Progress Summary**: Get overall workflow status

### ✅ Multi-Agent Coordination
- **Specialized Agents**: Each agent has specific responsibilities
- **Tool Registration**: Agents have access to relevant functions
- **Group Chat**: Agents communicate to complete workflow
- **Workflow Orchestration**: Manager coordinates agent interactions

---

## Demo Output Example

```
================================================================================
  E-COMMERCE DEMO WITH AP2 (AGENT PROTOCOL 2)
================================================================================

Scenario: Customer purchasing Professional Laptop Pro 15
Customer ID: CUST001
Location: USA

[*] Initializing system...
   [OK] Task ledger created: SmartLedger(67890:12345_67890, 0 tasks, 0% complete)
   [OK] Payment system ready

--------------------------------------------------------------------------------
  STEP 5: Payment Processor - AP2 Payment Flow
--------------------------------------------------------------------------------

[MONEY] Order Summary:
   Subtotal: $1299.99
   Shipping: $27.50
   ----------------------------------------
   TOTAL: $1327.49

[CARD] Payment Agent: Creating payment request using AP2...

   [OK] Payment Request Created
   [OK] Payment ID: d09bac86-3efb-455d-8703-548d10b6bf7e
   [OK] Amount: $1327.49 USD
   [OK] Status: pending
   [OK] Method: credit_card

[AUTH] Payment Agent: Requesting payment authorization...

   [OK] Payment Authorized
   [OK] Status: authorized
   [OK] Authorized by: customer_CUST001

[PROCESS] Payment Agent: Processing payment through gateway...

   [OK] Payment Processed Successfully!
   [OK] Status: completed
   [OK] Gateway: mock
   [OK] Transaction ID: mock_txn_fec2874286a4
   [OK] Amount charged: $1327.49 USD

============================================================
          ORDER CONFIRMATION
============================================================
Order ID: ORD_1762852381
Customer: CUST001
Date: 2025-11-11 14:43:01

Items:
  1x Professional Laptop Pro 15
  Price: $1299.99 each

Subtotal: $1299.99
Shipping (USA): $27.50
------------------------------------------------------------
TOTAL PAID: $1327.49

Payment Method: credit_card
Transaction ID: mock_txn_fec2874286a4

Shipping to: USA
Estimated delivery: 3-5 business days
============================================================
```

---

## Persistent Data

### Payment Ledger
All payments are persisted in:
```
agent_data/payment_ledger.json
```

View payment history:
```python
from integrations.ap2 import payment_ledger

# List all payments
payments = payment_ledger.list_payments()

# Filter by agent
agent_payments = payment_ledger.list_payments(agent_id="PaymentProcessor")

# Filter by status
completed = payment_ledger.list_payments(status=PaymentStatus.COMPLETED)
```

### Task Ledger
All workflow tasks are persisted in:
```
agent_data/ledger_67890_12345_67890.json
```

View task status:
```python
from helper_ledger import create_ledger_for_user_prompt

ledger = create_ledger_for_user_prompt(12345, 67890)
summary = ledger.get_progress_summary()
# {'total': 6, 'completed': 6, 'progress': '100.0%'}
```

---

## Extending the Demo

### Add Real Payment Gateway

Replace mock gateway with Stripe:

```python
from integrations.ap2 import PaymentGateway

# Instead of:
gateway=PaymentGateway.MOCK

# Use:
gateway=PaymentGateway.STRIPE

# And implement StripePaymentGateway connector
# (See integrations/ap2/ap2_protocol.py for examples)
```

### Add More Products

Edit the `PRODUCT_CATALOG` in the demo:

```python
PRODUCT_CATALOG = {
    "laptop_pro_15": {...},
    "monitor_4k": {
        "name": "4K Monitor Pro",
        "price": Decimal("499.99"),
        "stock": 10,
        "description": "Professional 4K monitor"
    }
}
```

### Customize Payment Methods

Change payment method:

```python
payment_method=PaymentMethod.PAYPAL       # Use PayPal
payment_method=PaymentMethod.CRYPTO       # Use cryptocurrency
payment_method=PaymentMethod.STRIPE       # Use Stripe
```

---

## Running Comprehensive Tests

Test the entire AP2 system:

```bash
python integrations/ap2/test_ap2_integration.py
```

This runs 8 comprehensive tests:
1. Payment request creation
2. Payment authorization
3. Payment processing
4. Payment listing and filtering
5. Mock gateway operations
6. AutoGen tool functions
7. Ledger persistence
8. Complete payment workflow

---

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────┐
│                 E-Commerce Application                   │
├─────────────────────────────────────────────────────────┤
│  • Customer Service Agent                               │
│  • Inventory Agent                                      │
│  • Payment Agent (AP2-enabled)                          │
│  • Shipping Agent                                       │
└──────────────────┬──────────────────────────────────────┘
                   │
                   ↓
┌─────────────────────────────────────────────────────────┐
│            AP2 (Agent Protocol 2)                       │
├─────────────────────────────────────────────────────────┤
│  • Payment Request Management                           │
│  • Authorization Workflow                               │
│  • Gateway Integration                                  │
│  • Transaction Ledger                                   │
└──────────────────┬──────────────────────────────────────┘
                   │
                   ↓
┌─────────────────────────────────────────────────────────┐
│              Payment Gateway                            │
├─────────────────────────────────────────────────────────┤
│  • Mock Gateway (for demo)                              │
│  • Stripe (production)                                  │
│  • PayPal (production)                                  │
│  • Others...                                            │
└─────────────────────────────────────────────────────────┘
```

---

## Summary

This demo shows how AP2 enables **agentic commerce** - allowing AI agents to request, authorize, and process payments as part of their autonomous workflows.

### Key Takeaways

1. **Agents can handle payments** - No human intervention needed for payment workflow
2. **Full audit trail** - Every payment step is tracked and logged
3. **Secure and structured** - Follows proper payment authorization workflow
4. **Extensible** - Easy to add real payment gateways
5. **Production-ready** - Full error handling and state management

### Next Steps

1. ✅ Run `demo_ecommerce_ap2_simple.py` - See AP2 in action
2. ✅ Check `agent_data/payment_ledger.json` - View persisted payments
3. ✅ Run `test_ap2_integration.py` - Comprehensive testing
4. ✅ Try `demo_ecommerce_ap2.py` - Full multi-agent LLM version
5. ✅ Integrate AP2 into your own agent workflows

---

**AP2 makes agentic commerce possible!** 🚀
