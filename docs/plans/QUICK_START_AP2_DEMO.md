# Quick Start - AP2 E-Commerce Demo

**See AP2 (Agent Protocol 2) in action in 60 seconds!**

---

## What You'll See

A realistic e-commerce workflow where AI agents:
1. Search for products
2. Check inventory
3. Reserve items
4. **Process payments using AP2** ⭐
5. Calculate shipping
6. Confirm orders

---

## Run the Demo

### Option 1: Simple Demo (NO API KEY NEEDED) ⭐ Recommended

```bash
python demo_ecommerce_ap2_simple.py
```

**What happens:**
- Scripted e-commerce workflow
- Complete AP2 payment flow demonstrated
- Payment request → Authorization → Processing → Completion
- Order confirmation generated

**Duration:** ~5 seconds

### Option 2: Full Multi-Agent Demo (Requires OpenAI API)

```bash
# 1. Set your OpenAI API key
export OPENAI_API_KEY=your_key_here  # Linux/Mac
set OPENAI_API_KEY=your_key_here     # Windows

# 2. Run the demo
python demo_ecommerce_ap2.py
```

**What happens:**
- 4 AI agents collaborate
- Natural language conversation
- Real-time agent decision making
- Full AP2 integration

**Duration:** ~30-60 seconds (depends on LLM response time)

---

## What is AP2?

**AP2 (Agent Protocol 2)** is the payment protocol for agentic commerce.

It enables AI agents to:
- ✅ Request payments autonomously
- ✅ Handle authorization workflows
- ✅ Process payments through gateways
- ✅ Track transactions with full audit trail
- ✅ Coordinate multi-agent payment workflows

---

## Demo Output Preview

```
================================================================================
  STEP 5: Payment Processor - AP2 Payment Flow
================================================================================

[MONEY] Order Summary:
   Subtotal: $1299.99
   Shipping: $27.50
   ----------------------------------------
   TOTAL: $1327.49

[CARD] Payment Agent: Creating payment request using AP2...
   [OK] Payment ID: d09bac86-3efb-455d-8703-548d10b6bf7e
   [OK] Amount: $1327.49 USD
   [OK] Status: pending

[AUTH] Payment Agent: Requesting payment authorization...
   [OK] Status: authorized
   [OK] Authorized by: customer_CUST001

[PROCESS] Payment Agent: Processing payment through gateway...
   [OK] Payment Processed Successfully!
   [OK] Transaction ID: mock_txn_fec2874286a4
   [OK] Amount charged: $1327.49 USD

============================================================
          ORDER CONFIRMATION
============================================================
Order ID: ORD_1762852381
Customer: CUST001
TOTAL PAID: $1327.49
Transaction ID: mock_txn_fec2874286a4
============================================================
```

---

## Check the Results

### View Payment Ledger

All payments are persisted:

```bash
# View payment ledger
cat agent_data/payment_ledger.json
```

Or using Python:

```python
from integrations.ap2 import payment_ledger

# List all payments
payments = payment_ledger.list_payments()

for payment in payments:
    print(f"Payment: ${payment.amount} {payment.currency}")
    print(f"Status: {payment.status}")
    print(f"Transaction: {payment.gateway_transaction_id}")
```

### View Task Ledger

All workflow tasks are tracked:

```python
from helper_ledger import create_ledger_for_user_prompt

ledger = create_ledger_for_user_prompt(12345, 67890)
summary = ledger.get_progress_summary()

print(f"Total tasks: {summary['total']}")
print(f"Completed: {summary['completed']}")
print(f"Progress: {summary['progress']}")
```

---

## Understanding the AP2 Flow

### 3-Step Payment Lifecycle

```
Step 1: CREATE REQUEST
├─ Agent requests payment
├─ Amount, currency, description specified
└─ Status: PENDING

Step 2: AUTHORIZE
├─ Payment approved by user/admin
├─ Approval chain tracked
└─ Status: AUTHORIZED

Step 3: PROCESS
├─ Payment sent to gateway
├─ Transaction ID generated
└─ Status: COMPLETED
```

### Code Example

```python
from integrations.ap2 import payment_ledger, PaymentMethod
from decimal import Decimal

# Step 1: Create payment request
payment = payment_ledger.create_payment_request(
    amount=Decimal("1327.49"),
    currency="USD",
    description="Laptop purchase",
    requester_agent_id="PaymentProcessor",
    payment_method=PaymentMethod.CREDIT_CARD
)

# Step 2: Authorize payment
payment_ledger.authorize_payment(
    payment_id=payment.payment_id,
    approver_id="customer_001"
)

# Step 3: Process payment
result = payment_ledger.process_payment(payment.payment_id)

if result['success']:
    print(f"Payment successful! Transaction: {payment.gateway_transaction_id}")
```

---

## What Makes AP2 Powerful?

### ✅ Agent Autonomy
Agents can request and process payments without human intervention (with proper authorization)

### ✅ Full Audit Trail
Every payment step is logged:
- Who requested it
- Who authorized it
- When it was processed
- Transaction details
- Associated metadata

### ✅ Gateway Agnostic
Works with any payment gateway:
- Mock (for testing)
- Stripe
- PayPal
- Square
- Custom gateways

### ✅ State Management
Proper payment lifecycle with state transitions:
- PENDING → AUTHORIZED → PROCESSING → COMPLETED
- Or: PENDING → CANCELLED
- Or: AUTHORIZED → FAILED

### ✅ Metadata Tracking
Attach any data to payments:
- Order details
- Customer info
- Product info
- Shipping details

---

## Next Steps

1. ✅ **Run the simple demo** - See it in action
   ```bash
   python demo_ecommerce_ap2_simple.py
   ```

2. ✅ **Read the full documentation**
   ```bash
   cat ECOMMERCE_AP2_DEMO_README.md
   ```

3. ✅ **Run comprehensive tests**
   ```bash
   python integrations/ap2/test_ap2_integration.py
   ```

4. ✅ **Try the full multi-agent version**
   ```bash
   python demo_ecommerce_ap2.py
   ```

5. ✅ **Integrate AP2 into your agents**
   ```python
   from integrations.ap2 import get_ap2_tools_for_autogen

   # Get AP2 tools for your agent
   tools = get_ap2_tools_for_autogen("YourAgentName")

   # Register with AutoGen agent
   for tool in tools:
       agent.register_for_llm(
           name=tool['name'],
           description=tool['description']
       )(tool['function'])
   ```

---

## Files Created

After running the demo:

```
agent_data/
├── payment_ledger.json          # All payment transactions
└── ledger_67890_12345_67890.json  # Workflow tasks

Created by demo:
├── demo_ecommerce_ap2_simple.py     # Simple demo (no LLM)
├── demo_ecommerce_ap2.py            # Full multi-agent demo
├── ECOMMERCE_AP2_DEMO_README.md     # Complete documentation
└── QUICK_START_AP2_DEMO.md          # This file
```

---

## Troubleshooting

### Payment ledger not found?

The ledger is created automatically on first payment request. Run the demo:
```bash
python demo_ecommerce_ap2_simple.py
```

### Want to reset the demo?

Delete the ledger files:
```bash
rm agent_data/payment_ledger.json
rm agent_data/ledger_67890_12345_67890.json
```

Then run the demo again.

---

## Summary

**AP2 enables agentic commerce** - AI agents can autonomously handle payment workflows with:
- ✅ Payment requests
- ✅ Authorization tracking
- ✅ Gateway processing
- ✅ Transaction persistence
- ✅ Full audit trail

**Get started in 60 seconds:**
```bash
python demo_ecommerce_ap2_simple.py
```

🚀 **Welcome to the future of agentic commerce!**
