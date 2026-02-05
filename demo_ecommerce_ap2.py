"""
E-Commerce Demo with AP2 (Agent Protocol 2) Integration

This demo showcases a realistic e-commerce workflow using:
- AutoGen agents for customer service, inventory management, and payment processing
- AP2 (Agent Protocol 2) for agentic commerce and payment handling
- Agent Ledger for task tracking and state management
- Agent Lightning for training and optimization (optional)

Scenario:
Customer wants to purchase a laptop. The system coordinates multiple agents:
1. Customer Service Agent - Handles customer inquiries
2. Inventory Agent - Checks stock and reserves items
3. Payment Agent - Processes payment using AP2
4. Shipping Agent - Arranges delivery

The workflow demonstrates:
- Multi-agent coordination
- Payment request/authorization/processing (AP2)
- Task state management (Agent Ledger)
- Realistic ecommerce flow
"""

import os
import sys
import json
from decimal import Decimal
from typing import List, Dict, Any
import time

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# AutoGen imports
import autogen
from autogen import ConversableAgent, AssistantAgent, UserProxyAgent

# Agent Ledger imports
from helper_ledger import create_ledger_for_user_prompt
from agent_ledger import Task, TaskType, TaskStatus

# AP2 imports
from integrations.ap2 import (
    payment_ledger,
    get_ap2_tools_for_autogen,
    PaymentStatus,
    PaymentMethod,
    PaymentGateway
)

# Configuration
LLM_CONFIG = {
    "model": "gpt-4",
    "api_key": os.getenv("OPENAI_API_KEY"),
    "temperature": 0.7,
}

# E-commerce product catalog
PRODUCT_CATALOG = {
    "laptop_pro_15": {
        "name": "Professional Laptop Pro 15",
        "price": Decimal("1299.99"),
        "stock": 5,
        "description": "High-performance laptop with 16GB RAM, 512GB SSD"
    },
    "laptop_air_13": {
        "name": "Ultrabook Air 13",
        "price": Decimal("899.99"),
        "stock": 8,
        "description": "Lightweight laptop with 8GB RAM, 256GB SSD"
    },
    "laptop_gaming": {
        "name": "Gaming Beast X1",
        "price": Decimal("1899.99"),
        "stock": 3,
        "description": "Gaming laptop with RTX 4080, 32GB RAM, 1TB SSD"
    }
}

# Mock inventory database
inventory_db = PRODUCT_CATALOG.copy()
reserved_items = {}


# ========== Tool Functions ==========

def search_products(query: str) -> str:
    """
    Search for products in the catalog

    Args:
        query: Search query (e.g., "laptop", "gaming", "professional")

    Returns:
        JSON string with matching products
    """
    results = []
    query_lower = query.lower()

    for product_id, product in PRODUCT_CATALOG.items():
        if (query_lower in product['name'].lower() or
            query_lower in product['description'].lower()):
            results.append({
                'product_id': product_id,
                'name': product['name'],
                'price': str(product['price']),
                'stock': inventory_db[product_id]['stock'],
                'description': product['description']
            })

    return json.dumps({'products': results}, indent=2)


def check_inventory(product_id: str) -> str:
    """
    Check inventory for a specific product

    Args:
        product_id: Product identifier

    Returns:
        JSON string with inventory status
    """
    if product_id not in inventory_db:
        return json.dumps({
            'success': False,
            'error': 'Product not found'
        })

    product = inventory_db[product_id]
    return json.dumps({
        'success': True,
        'product_id': product_id,
        'name': product['name'],
        'available_stock': product['stock'],
        'price': str(product['price']),
        'in_stock': product['stock'] > 0
    }, indent=2)


def reserve_inventory(product_id: str, quantity: int, customer_id: str) -> str:
    """
    Reserve inventory for a customer

    Args:
        product_id: Product to reserve
        quantity: Quantity to reserve
        customer_id: Customer making the reservation

    Returns:
        JSON string with reservation status
    """
    if product_id not in inventory_db:
        return json.dumps({
            'success': False,
            'error': 'Product not found'
        })

    product = inventory_db[product_id]

    if product['stock'] < quantity:
        return json.dumps({
            'success': False,
            'error': f'Insufficient stock. Available: {product["stock"]}, Requested: {quantity}'
        })

    # Reserve the item
    reservation_id = f"RES_{product_id}_{customer_id}_{int(time.time())}"
    reserved_items[reservation_id] = {
        'product_id': product_id,
        'quantity': quantity,
        'customer_id': customer_id,
        'timestamp': time.time()
    }

    # Reduce available stock
    inventory_db[product_id]['stock'] -= quantity

    return json.dumps({
        'success': True,
        'reservation_id': reservation_id,
        'product_id': product_id,
        'quantity': quantity,
        'price_per_unit': str(product['price']),
        'total_price': str(product['price'] * quantity),
        'message': f'Reserved {quantity}x {product["name"]}'
    }, indent=2)


def calculate_shipping(destination: str, weight_kg: float) -> str:
    """
    Calculate shipping cost

    Args:
        destination: Shipping destination (e.g., "USA", "EU", "UK")
        weight_kg: Package weight in kilograms

    Returns:
        JSON string with shipping details
    """
    # Simple shipping cost calculation
    base_rates = {
        "USA": Decimal("15.00"),
        "EU": Decimal("25.00"),
        "UK": Decimal("20.00"),
        "ASIA": Decimal("30.00")
    }

    destination_upper = destination.upper()
    base_rate = base_rates.get(destination_upper, Decimal("35.00"))

    # Add $5 per kg
    weight_cost = Decimal(str(weight_kg)) * Decimal("5.00")
    total_cost = base_rate + weight_cost

    # Estimate delivery time
    delivery_days = {
        "USA": "3-5 business days",
        "EU": "5-7 business days",
        "UK": "4-6 business days",
        "ASIA": "7-10 business days"
    }

    return json.dumps({
        'destination': destination_upper,
        'weight_kg': weight_kg,
        'base_rate': str(base_rate),
        'weight_cost': str(weight_cost),
        'total_shipping_cost': str(total_cost),
        'estimated_delivery': delivery_days.get(destination_upper, "10-14 business days"),
        'currency': 'USD'
    }, indent=2)


def get_customer_info(customer_id: str) -> str:
    """
    Get customer information

    Args:
        customer_id: Customer identifier

    Returns:
        JSON string with customer details
    """
    # Mock customer database
    customers = {
        "CUST001": {
            "name": "John Doe",
            "email": "john.doe@example.com",
            "shipping_address": "123 Main St, New York, NY 10001, USA",
            "country": "USA",
            "payment_method": "credit_card",
            "vip_status": False
        }
    }

    customer = customers.get(customer_id)
    if not customer:
        return json.dumps({
            'success': False,
            'error': 'Customer not found'
        })

    return json.dumps({
        'success': True,
        'customer_id': customer_id,
        **customer
    }, indent=2)


# ========== Agent Creation ==========

def create_ecommerce_agents(user_id: int, prompt_id: int):
    """
    Create the ecommerce agent ecosystem

    Returns:
        Tuple of (customer_service_agent, inventory_agent, payment_agent, shipping_agent, user_proxy)
    """

    # Create ledger for task tracking
    ledger = create_ledger_for_user_prompt(user_id, prompt_id)

    print(f"\n{'='*80}")
    print(f"INITIALIZING E-COMMERCE AGENT SYSTEM")
    print(f"{'='*80}")
    print(f"User ID: {user_id}")
    print(f"Prompt ID: {prompt_id}")
    print(f"Ledger: {ledger}")
    print(f"{'='*80}\n")

    # 1. Customer Service Agent
    customer_service_agent = AssistantAgent(
        name="CustomerService",
        system_message="""You are a friendly customer service representative for an online electronics store.

Your responsibilities:
1. Greet customers and understand their needs
2. Help them find products using the search_products function
3. Provide product information and recommendations
4. Coordinate with other agents (Inventory, Payment, Shipping) to complete orders
5. Ensure customer satisfaction throughout the process

Be helpful, professional, and proactive. Always confirm details with the customer before proceeding.""",
        llm_config=LLM_CONFIG,
        human_input_mode="NEVER"
    )

    # Register search and customer info tools
    customer_service_agent.register_for_llm(
        name="search_products",
        description="Search for products in the catalog"
    )(search_products)

    customer_service_agent.register_for_llm(
        name="get_customer_info",
        description="Get customer information by customer ID"
    )(get_customer_info)

    # 2. Inventory Agent
    inventory_agent = AssistantAgent(
        name="InventoryManager",
        system_message="""You are the inventory management specialist.

Your responsibilities:
1. Check product availability using check_inventory
2. Reserve items for customers using reserve_inventory
3. Provide accurate stock information
4. Ensure inventory is properly managed

Be accurate and efficient. Always verify stock before reserving items.""",
        llm_config=LLM_CONFIG,
        human_input_mode="NEVER"
    )

    # Register inventory tools
    inventory_agent.register_for_llm(
        name="check_inventory",
        description="Check inventory for a specific product"
    )(check_inventory)

    inventory_agent.register_for_llm(
        name="reserve_inventory",
        description="Reserve inventory for a customer"
    )(reserve_inventory)

    # 3. Payment Agent (with AP2 integration)
    payment_agent = AssistantAgent(
        name="PaymentProcessor",
        system_message="""You are the payment processing specialist using AP2 (Agent Protocol 2).

Your responsibilities:
1. Request payment using request_payment function
2. Wait for payment authorization
3. Process authorized payments using process_payment
4. Handle payment confirmations and receipts

IMPORTANT: Follow this exact workflow:
Step 1: Use request_payment(amount, currency, description, payment_method) to create payment request
Step 2: Use authorize_payment(payment_id, approver_id) to authorize the payment (use "customer" as approver_id for demo)
Step 3: Use process_payment(payment_id) to complete the payment

Always provide clear payment confirmations and transaction IDs to customers.""",
        llm_config=LLM_CONFIG,
        human_input_mode="NEVER"
    )

    # Register AP2 payment tools
    ap2_tools = get_ap2_tools_for_autogen("PaymentProcessor")
    for tool in ap2_tools:
        payment_agent.register_for_llm(
            name=tool['name'],
            description=tool['description']
        )(tool['function'])

    # 4. Shipping Agent
    shipping_agent = AssistantAgent(
        name="ShippingCoordinator",
        system_message="""You are the shipping and logistics coordinator.

Your responsibilities:
1. Calculate shipping costs using calculate_shipping
2. Provide delivery estimates
3. Arrange shipping for completed orders
4. Provide tracking information

Be clear about shipping costs and delivery times. Assume laptop weight is 2.5 kg.""",
        llm_config=LLM_CONFIG,
        human_input_mode="NEVER"
    )

    # Register shipping tool
    shipping_agent.register_for_llm(
        name="calculate_shipping",
        description="Calculate shipping cost based on destination and weight"
    )(calculate_shipping)

    # 5. User Proxy (simulates customer)
    user_proxy = UserProxyAgent(
        name="Customer",
        system_message="You are a customer looking to purchase a laptop.",
        human_input_mode="TERMINATE",
        max_consecutive_auto_reply=0,
        code_execution_config=False
    )

    # Register tool executors
    autogen.register_function(
        search_products,
        caller=customer_service_agent,
        executor=user_proxy,
        name="search_products",
        description="Search for products in the catalog"
    )

    autogen.register_function(
        get_customer_info,
        caller=customer_service_agent,
        executor=user_proxy,
        name="get_customer_info",
        description="Get customer information"
    )

    autogen.register_function(
        check_inventory,
        caller=inventory_agent,
        executor=user_proxy,
        name="check_inventory",
        description="Check inventory for a product"
    )

    autogen.register_function(
        reserve_inventory,
        caller=inventory_agent,
        executor=user_proxy,
        name="reserve_inventory",
        description="Reserve inventory for a customer"
    )

    autogen.register_function(
        calculate_shipping,
        caller=shipping_agent,
        executor=user_proxy,
        name="calculate_shipping",
        description="Calculate shipping cost"
    )

    # Register AP2 tools for execution
    for tool in ap2_tools:
        autogen.register_function(
            tool['function'],
            caller=payment_agent,
            executor=user_proxy,
            name=tool['name'],
            description=tool['description']
        )

    print("✓ All agents initialized successfully")
    print("  - Customer Service Agent")
    print("  - Inventory Manager Agent")
    print("  - Payment Processor Agent (AP2 enabled)")
    print("  - Shipping Coordinator Agent")
    print("  - Customer (User Proxy)")
    print()

    return customer_service_agent, inventory_agent, payment_agent, shipping_agent, user_proxy, ledger


# ========== Main Demo ==========

def run_ecommerce_demo():
    """Run the complete e-commerce demo"""

    print("\n" + "="*80)
    print("E-COMMERCE DEMO WITH AP2 (AGENT PROTOCOL 2)")
    print("="*80)
    print("\nScenario: Customer purchasing a laptop")
    print("Agents: CustomerService, Inventory, Payment (AP2), Shipping")
    print("="*80 + "\n")

    # Create agents
    user_id = 12345
    prompt_id = 67890

    customer_service, inventory, payment, shipping, customer, ledger = create_ecommerce_agents(
        user_id, prompt_id
    )

    # Create tasks in ledger
    print("Creating workflow tasks in ledger...")
    tasks = [
        Task("search_product", "Search for laptop products", TaskType.PRE_ASSIGNED),
        Task("check_stock", "Check inventory availability", TaskType.PRE_ASSIGNED),
        Task("reserve_item", "Reserve selected product", TaskType.PRE_ASSIGNED),
        Task("process_payment", "Process payment using AP2", TaskType.PRE_ASSIGNED),
        Task("calculate_shipping", "Calculate shipping cost", TaskType.PRE_ASSIGNED),
        Task("confirm_order", "Confirm order completion", TaskType.PRE_ASSIGNED),
    ]

    for task in tasks:
        ledger.add_task(task)

    print(f"✓ Created {len(tasks)} workflow tasks")
    print()

    # Create group chat
    groupchat = autogen.GroupChat(
        agents=[customer_service, inventory, payment, shipping, customer],
        messages=[],
        max_round=30,
        speaker_selection_method="auto",
        allow_repeat_speaker=False
    )

    manager = autogen.GroupChatManager(
        groupchat=groupchat,
        llm_config=LLM_CONFIG
    )

    # Start the conversation
    initial_message = """Hello! I'm looking to buy a high-performance laptop for software development.
My budget is around $1500. I'm located in the USA.
My customer ID is CUST001.

Can you help me find something suitable and complete the purchase?"""

    print("="*80)
    print("STARTING E-COMMERCE WORKFLOW")
    print("="*80 + "\n")

    # Update task status
    ledger.update_task_status("search_product", TaskStatus.IN_PROGRESS)

    try:
        # Initiate the chat
        customer.initiate_chat(
            manager,
            message=initial_message
        )

        print("\n" + "="*80)
        print("WORKFLOW COMPLETED")
        print("="*80 + "\n")

        # Mark all tasks as completed
        for task in tasks:
            if ledger.get_task(task.task_id).status != TaskStatus.COMPLETED:
                ledger.update_task_status(task.task_id, TaskStatus.COMPLETED)

    except Exception as e:
        print(f"\n❌ Error during workflow: {e}")
        import traceback
        traceback.print_exc()

    # Print summary
    print("\n" + "="*80)
    print("DEMO SUMMARY")
    print("="*80)

    # Task summary
    print("\n📋 Task Status:")
    summary = ledger.get_progress_summary()
    print(f"  Total tasks: {summary['total']}")
    print(f"  Completed: {summary['completed']}")
    print(f"  Progress: {summary['progress']}")

    # Payment summary
    print("\n💳 Payment Summary:")
    payments = payment_ledger.list_payments(agent_id="PaymentProcessor")
    if payments:
        for payment in payments[:3]:  # Show last 3 payments
            print(f"  Payment ID: {payment.payment_id}")
            print(f"  Amount: ${payment.amount} {payment.currency}")
            print(f"  Status: {payment.status.value}")
            print(f"  Description: {payment.description}")
            if payment.gateway_transaction_id:
                print(f"  Transaction: {payment.gateway_transaction_id}")
            print()
    else:
        print("  No payments processed")

    # Inventory summary
    print("\n📦 Inventory Status:")
    for product_id, product in inventory_db.items():
        print(f"  {product['name']}: {product['stock']} units")

    print("\n" + "="*80)
    print("DEMO COMPLETE")
    print("="*80 + "\n")


if __name__ == "__main__":
    # Check for OpenAI API key
    if not os.getenv("OPENAI_API_KEY"):
        print("❌ Error: OPENAI_API_KEY environment variable not set")
        print("\nPlease set your OpenAI API key:")
        print("  Windows: set OPENAI_API_KEY=your_key_here")
        print("  Linux/Mac: export OPENAI_API_KEY=your_key_here")
        sys.exit(1)

    run_ecommerce_demo()
