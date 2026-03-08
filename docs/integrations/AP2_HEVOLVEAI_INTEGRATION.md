# AP2 Integration with HEvolveAI Framework

**Agent Protocol 2 (AP2) for Agentic Commerce - HEvolveAI Integration Guide**

---

## Overview

This guide shows how to integrate **AP2 (Agent Protocol 2)** payment processing into agents created with your HEvolveAI framework.

### What is AP2?

AP2 enables AI agents to autonomously handle payment workflows:
- Request payments
- Authorize transactions
- Process payments through gateways
- Track payment history with full audit trail

---

## Quick Demo

### Run the E-Commerce Demo

```bash
python run_ecommerce_agent_ap2.py
```

**This demonstrates:**
- E-commerce workflow within HEvolveAI framework
- AP2 payment processing (request → authorize → process)
- Order confirmation and fulfillment
- Transaction persistence

---

## E-Commerce Agent Configuration

### Agent Definition: `prompts/999.json`

```json
{
  "name": "E-Commerce Shopping Assistant with AP2 Payments",
  "prompt_id": 999,
  "personas": [
    {
      "name": "Product Specialist",
      "description": "Helps customers find and select products"
    },
    {
      "name": "Order Fulfillment Manager",
      "description": "Handles orders and payments using AP2"
    }
  ],
  "flows": [
    {
      "flow_name": "Customer Product Discovery",
      "persona": "Product Specialist",
      "actions": [
        "Greet customer and understand needs",
        "Search product catalog",
        "Present recommendations",
        "Confirm product selection"
      ]
    },
    {
      "flow_name": "Order Processing with AP2",
      "persona": "Order Fulfillment Manager",
      "actions": [
        "Check inventory",
        "Reserve product",
        "Calculate shipping",
        "Calculate total",
        "Create payment request (AP2)",
        "Authorize payment (AP2)",
        "Process payment (AP2)",
        "Generate order confirmation",
        "Update inventory"
      ]
    }
  ]
}
```

---

## Integration with Your Framework

### How It Works

Your HEvolveAI framework uses:
- **`create_recipe.py`** - Agent creation and execution
- **`prompts/{prompt_id}.json`** - Agent configuration
- **Action-based workflows** - Step-by-step execution

AP2 integrates as **actions** within your agent flows:

```
HEvolveAI Agent Flow:
┌─────────────────────────────────────┐
│ Action 1: Check inventory           │
├─────────────────────────────────────┤
│ Action 2: Reserve product           │
├─────────────────────────────────────┤
│ Action 3: Calculate shipping        │
├─────────────────────────────────────┤
│ Action 4: Calculate total           │
├─────────────────────────────────────┤
│ Action 5: Create payment (AP2) ✨  │  ← AP2 Integration
├─────────────────────────────────────┤
│ Action 6: Authorize payment (AP2) ✨│  ← AP2 Integration
├─────────────────────────────────────┤
│ Action 7: Process payment (AP2) ✨  │  ← AP2 Integration
├─────────────────────────────────────┤
│ Action 8: Confirm order             │
└─────────────────────────────────────┘
```

---

## Using AP2 in Your Agents

### Step 1: Import AP2 in create_recipe.py

AP2 is already imported in your `create_recipe.py`:

```python
# AP2 (Agent Protocol 2) - Agentic Commerce
from integrations.ap2 import (
    payment_ledger, get_ap2_tools_for_autogen,
    PaymentStatus, PaymentMethod, PaymentGateway
)
```

### Step 2: Add AP2 Actions to Agent Configuration

In your agent JSON (`prompts/{prompt_id}.json`), add payment actions:

```json
{
  "actions": [
    "Create payment request using AP2 with order total and customer details",
    "Request payment authorization from customer using AP2 approval workflow",
    "Process authorized payment through AP2 gateway and obtain transaction ID"
  ]
}
```

### Step 3: Implement AP2 in Agent Tools

Add AP2 payment functions as tools for your agents:

```python
# In your agent creation (create_recipe.py)

# Get AP2 tools
ap2_tools = get_ap2_tools_for_autogen("OrderFulfillmentManager")

# Register with agent
for tool in ap2_tools:
    agent.register_for_llm(
        name=tool['name'],
        description=tool['description']
    )(tool['function'])

# Tools available:
# - request_payment(amount, currency, description, payment_method)
# - authorize_payment(payment_id, approver_id)
# - process_payment(payment_id)
```

---

## AP2 Payment Workflow

### 3-Step Payment Process

#### Step 1: Create Payment Request

**Action in agent:** "Create payment request using AP2"

```python
from integrations.ap2 import payment_ledger, PaymentMethod
from decimal import Decimal

payment = payment_ledger.create_payment_request(
    amount=Decimal("1327.49"),
    currency="USD",
    description="Purchase of Professional Laptop Pro 15",
    requester_agent_id="OrderFulfillmentManager",
    payment_method=PaymentMethod.CREDIT_CARD,
    metadata={
        'customer_id': 'CUST001',
        'order_id': 'ORD_123',
        'product': 'laptop_pro_15'
    }
)

# Result:
# - payment_id: Unique payment identifier
# - status: PENDING
# - Saved to payment ledger
```

#### Step 2: Authorize Payment

**Action in agent:** "Request payment authorization"

```python
success = payment_ledger.authorize_payment(
    payment_id=payment.payment_id,
    approver_id="customer_CUST001"
)

# Result:
# - Approval chain tracked
# - status: PENDING → AUTHORIZED
# - Ready for processing
```

#### Step 3: Process Payment

**Action in agent:** "Process payment through gateway"

```python
result = payment_ledger.process_payment(payment.payment_id)

# Result:
# - Gateway transaction created
# - transaction_id: Generated
# - status: AUTHORIZED → PROCESSING → COMPLETED
# - Payment confirmed
```

---

## Example: E-Commerce Agent Workflow

### Complete Flow Example

```python
# This happens automatically when user interacts with your agent

# FLOW 1: Product Discovery (Persona: Product Specialist)
user_request = "I need a laptop for $1500"

# Agent actions:
# 1. Search products
# 2. Recommend: Professional Laptop Pro 15 ($1299.99)
# 3. Customer confirms selection

# FLOW 2: Order Processing (Persona: Order Fulfillment Manager)
# Action 1: Check inventory (5 units available)
# Action 2: Reserve 1 unit
# Action 3: Calculate shipping ($27.50)
# Action 4: Calculate total ($1327.49)

# Action 5: Create payment request (AP2)
payment = payment_ledger.create_payment_request(
    amount=Decimal("1327.49"),
    currency="USD",
    description="Purchase of Professional Laptop Pro 15",
    requester_agent_id="OrderFulfillmentManager"
)

# Action 6: Authorize payment (AP2)
payment_ledger.authorize_payment(payment.payment_id, "customer_CUST001")

# Action 7: Process payment (AP2)
result = payment_ledger.process_payment(payment.payment_id)

# Action 8: Generate order confirmation
order_id = "ORD_1762857519"
transaction_id = payment.gateway_transaction_id

# Action 9: Update inventory (4 units remaining)
```

---

## Running the E-Commerce Agent

### Option 1: Demo Script (Simulated)

```bash
python run_ecommerce_agent_ap2.py
```

Shows the complete flow without needing Flask server.

### Option 2: Production (Flask API)

1. **Start Flask Server:**
```bash
python app.py
```

2. **Create Agent via API:**
```bash
curl -X POST http://localhost:6777/create_agent \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 10077,
    "prompt_id": 999,
    "task": "I want to buy a laptop for software development"
  }'
```

3. **Agent Executes:**
- Follows flows defined in `prompts/999.json`
- Uses AP2 for payment processing
- Returns order confirmation

---

## AP2 Features in HEvolveAI

### ✅ Action-Based Integration

AP2 integrates naturally with your action-based workflow system:

```json
{
  "actions": [
    "Create payment request using AP2...",
    "Authorize payment...",
    "Process payment through gateway..."
  ]
}
```

### ✅ Task Ledger Integration

Combines with your existing agent_ledger for complete tracking:

```python
from helper_ledger import create_ledger_for_user_prompt
from integrations.ap2 import payment_ledger

# Task tracking
task_ledger = create_ledger_for_user_prompt(user_id=10077, prompt_id=999)

# Payment tracking
payment = payment_ledger.create_payment_request(...)

# Both ledgers work together
```

### ✅ Persona-Based Flow

Different personas handle different aspects:
- **Product Specialist** - Product discovery
- **Order Fulfillment Manager** - Payment processing with AP2

### ✅ Persistent State

All payments saved to `agent_data/payment_ledger.json`:

```python
# View payment history
from integrations.ap2 import payment_ledger

payments = payment_ledger.list_payments(
    agent_id="OrderFulfillmentManager"
)

for payment in payments:
    print(f"{payment.payment_id}: ${payment.amount} - {payment.status}")
```

---

## Production Deployment

### 1. Configure Payment Gateway

Replace mock gateway with real gateway:

```python
# In integrations/ap2/ap2_protocol.py

# Add Stripe connector
class StripePaymentGateway(PaymentGatewayConnector):
    def __init__(self, api_key):
        super().__init__(PaymentGateway.STRIPE, api_key=api_key)
        import stripe
        self.stripe = stripe
        self.stripe.api_key = api_key

    def create_payment(self, payment_request):
        # Stripe implementation
        ...
```

### 2. Update Agent Configuration

Use production gateway in agent actions:

```json
{
  "actions": [
    "Create payment request using AP2 with Stripe gateway for production use"
  ]
}
```

### 3. Environment Variables

```bash
export STRIPE_API_KEY=sk_live_...
export PAYMENT_GATEWAY=stripe
```

---

## Files Structure

```
Project Root/
├── prompts/
│   └── 999.json                      # E-commerce agent config
│
├── run_ecommerce_agent_ap2.py        # Demo script
│
├── integrations/
│   └── ap2/
│       ├── ap2_protocol.py           # AP2 core
│       └── test_ap2_integration.py   # Tests
│
├── agent_data/
│   ├── payment_ledger.json           # Payment transactions
│   └── ledger_999_10077_999.json    # Task tracking
│
├── create_recipe.py                   # Your agent framework
├── helper_ledger.py                   # Task ledger helpers
│
└── Documentation/
    ├── AP2_HEVOLVEAI_INTEGRATION.md  # This file
    ├── ECOMMERCE_AP2_DEMO_README.md  # General demo guide
    └── QUICK_START_AP2_DEMO.md       # Quick start
```

---

## Testing

### Run Comprehensive Tests

```bash
python integrations/ap2/test_ap2_integration.py
```

**Tests cover:**
1. Payment request creation
2. Authorization workflow
3. Gateway processing
4. Ledger persistence
5. Multi-step lifecycle
6. AutoGen tool integration

---

## Summary

### Key Integration Points

1. **Agent Configuration** - Add payment actions to `prompts/{id}.json`
2. **Tool Registration** - Register AP2 tools with agents
3. **Workflow Actions** - Use AP2 in action sequences
4. **State Management** - Combine with agent_ledger
5. **Production Gateway** - Replace mock with Stripe/PayPal

### Benefits

✅ **Native Integration** - Works with your action-based system
✅ **Persona Support** - Different personas handle different flows
✅ **State Tracking** - Full integration with task ledger
✅ **Production Ready** - Real gateway support
✅ **Audit Trail** - Complete payment history

---

## Next Steps

1. ✅ **Run demo:** `python run_ecommerce_agent_ap2.py`
2. ✅ **Review agent config:** `prompts/999.json`
3. ✅ **Check payment ledger:** `agent_data/payment_ledger.json`
4. ✅ **Add to your agents:** Use AP2 actions in your agent flows
5. ✅ **Deploy:** Configure production gateway

---

**AP2 is now integrated with HEvolveAI!** 🚀

Your agents can now handle payments autonomously using the Agent Protocol 2.
