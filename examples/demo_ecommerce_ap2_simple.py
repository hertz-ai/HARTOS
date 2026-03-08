"""
Simple E-Commerce Demo with AP2 (No LLM Required)

This is a simplified demonstration of AP2 (Agent Protocol 2) in an e-commerce workflow.
It runs without requiring OpenAI API keys - perfect for testing the payment flow.

Scenario:
Customer purchases a laptop through a scripted workflow that demonstrates:
1. Product search and selection
2. Inventory reservation
3. Payment processing with AP2
4. Shipping calculation
5. Order confirmation

All using the AP2 payment protocol for agentic commerce.
"""

import os
import sys
import json
from decimal import Decimal
import time

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Agent Ledger imports
from helper_ledger import create_ledger_for_user_prompt
from agent_ledger import Task, TaskType, TaskStatus

# AP2 imports
from integrations.ap2 import (
    payment_ledger,
    PaymentStatus,
    PaymentMethod,
    PaymentGateway
)


# E-commerce product catalog
PRODUCTS = {
    "laptop_pro_15": {
        "name": "Professional Laptop Pro 15",
        "price": Decimal("1299.99"),
        "stock": 5,
        "description": "High-performance laptop with 16GB RAM, 512GB SSD"
    }
}

# Inventory
inventory = {"laptop_pro_15": {"stock": 5}}


def print_section(title):
    """Print a section header"""
    print("\n" + "="*80)
    print(f"  {title}")
    print("="*80 + "\n")


def print_step(step_num, description):
    """Print a step header"""
    print(f"\n{'-'*80}")
    print(f"  STEP {step_num}: {description}")
    print(f"{'-'*80}\n")


def run_simple_ecommerce_demo():
    """Run a simple scripted e-commerce demo with AP2"""

    print_section("E-COMMERCE DEMO WITH AP2 (AGENT PROTOCOL 2)")

    print("Scenario: Customer purchasing Professional Laptop Pro 15")
    print("Customer ID: CUST001")
    print("Location: USA")
    print()

    # Setup
    user_id = 12345
    prompt_id = 67890

    # Create ledger
    print("[*] Initializing system...")
    ledger = create_ledger_for_user_prompt(user_id, prompt_id)
    print(f"   [OK] Task ledger created: {ledger}")
    print(f"   [OK] Payment system ready")

    # Create workflow tasks
    print("\n[*] Creating workflow tasks...")
    tasks = [
        Task("search", "Search for laptop", TaskType.PRE_ASSIGNED),
        Task("check_stock", "Check inventory", TaskType.PRE_ASSIGNED),
        Task("reserve", "Reserve product", TaskType.PRE_ASSIGNED),
        Task("payment", "Process payment via AP2", TaskType.PRE_ASSIGNED),
        Task("shipping", "Calculate shipping", TaskType.PRE_ASSIGNED),
        Task("confirm", "Confirm order", TaskType.PRE_ASSIGNED),
    ]

    for task in tasks:
        ledger.add_task(task)
    print(f"   [OK] Created {len(tasks)} workflow tasks")

    # ===== STEP 1: Product Search =====
    print_step(1, "Customer Service - Product Search")

    ledger.update_task_status("search", TaskStatus.IN_PROGRESS)

    print("[SEARCH] Customer: Looking for a high-performance laptop for $1500 budget")
    print()
    print("[AGENT] Customer Service Agent: Searching catalog...")

    product_id = "laptop_pro_15"
    product = PRODUCTS[product_id]

    print(f"\n   [OK] Found: {product['name']}")
    print(f"   [OK] Price: ${product['price']}")
    print(f"   [OK] Description: {product['description']}")

    ledger.update_task_status("search", TaskStatus.COMPLETED)

    # ===== STEP 2: Check Inventory =====
    print_step(2, "Inventory Manager - Stock Check")

    ledger.update_task_status("check_stock", TaskStatus.IN_PROGRESS)

    print("[INVENTORY] Inventory Agent: Checking stock availability...")

    available_stock = inventory[product_id]["stock"]
    print(f"\n   [OK] Product: {product['name']}")
    print(f"   [OK] Available stock: {available_stock} units")
    print(f"   [OK] Status: IN STOCK")

    ledger.update_task_status("check_stock", TaskStatus.COMPLETED)

    # ===== STEP 3: Reserve Inventory =====
    print_step(3, "Inventory Manager - Reserve Product")

    ledger.update_task_status("reserve", TaskStatus.IN_PROGRESS)

    quantity = 1
    customer_id = "CUST001"

    print(f"[LOCK] Reserving {quantity}x {product['name']} for customer {customer_id}...")

    if inventory[product_id]["stock"] >= quantity:
        inventory[product_id]["stock"] -= quantity
        reservation_id = f"RES_{product_id}_{customer_id}_{int(time.time())}"

        print(f"\n   [OK] Reservation ID: {reservation_id}")
        print(f"   [OK] Reserved: {quantity}x {product['name']}")
        print(f"   [OK] Remaining stock: {inventory[product_id]['stock']} units")

        ledger.update_task_status("reserve", TaskStatus.COMPLETED)

    # ===== STEP 4: Calculate Shipping =====
    print_step(4, "Shipping Coordinator - Calculate Shipping")

    ledger.update_task_status("shipping", TaskStatus.IN_PROGRESS)

    destination = "USA"
    weight_kg = 2.5

    print(f"[SHIP] Calculating shipping to {destination}...")
    print(f"   Package weight: {weight_kg} kg")

    base_rate = Decimal("15.00")
    weight_cost = Decimal(str(weight_kg)) * Decimal("5.00")
    shipping_cost = base_rate + weight_cost

    print(f"\n   [OK] Base rate: ${base_rate}")
    print(f"   [OK] Weight cost: ${weight_cost}")
    print(f"   [OK] Total shipping: ${shipping_cost}")
    print(f"   [OK] Estimated delivery: 3-5 business days")

    ledger.update_task_status("shipping", TaskStatus.COMPLETED)

    # ===== STEP 5: Process Payment with AP2 =====
    print_step(5, "Payment Processor - AP2 Payment Flow")

    ledger.update_task_status("payment", TaskStatus.IN_PROGRESS)

    # Calculate total
    subtotal = product['price'] * quantity
    total_amount = subtotal + shipping_cost

    print(f"[MONEY] Order Summary:")
    print(f"   Subtotal: ${subtotal}")
    print(f"   Shipping: ${shipping_cost}")
    print(f"   {'-'*40}")
    print(f"   TOTAL: ${total_amount}")
    print()

    # Step 5a: Create Payment Request
    print("[CARD] Payment Agent: Creating payment request using AP2...")

    payment = payment_ledger.create_payment_request(
        amount=total_amount,
        currency="USD",
        description=f"Purchase of {product['name']} + shipping",
        requester_agent_id="PaymentProcessor",
        payment_method=PaymentMethod.CREDIT_CARD,
        gateway=PaymentGateway.MOCK,
        metadata={
            'customer_id': customer_id,
            'product_id': product_id,
            'quantity': quantity,
            'reservation_id': reservation_id,
            'shipping_destination': destination
        }
    )

    print(f"\n   [OK] Payment Request Created")
    print(f"   [OK] Payment ID: {payment.payment_id}")
    print(f"   [OK] Amount: ${payment.amount} {payment.currency}")
    print(f"   [OK] Status: {payment.status.value}")
    print(f"   [OK] Method: {payment.payment_method.value}")

    # Step 5b: Authorize Payment
    print("\n[AUTH] Payment Agent: Requesting payment authorization...")

    success = payment_ledger.authorize_payment(payment.payment_id, "customer_CUST001")

    if success:
        payment = payment_ledger.get_payment(payment.payment_id)
        print(f"\n   [OK] Payment Authorized")
        print(f"   [OK] Status: {payment.status.value}")
        print(f"   [OK] Authorized by: customer_CUST001")

    # Step 5c: Process Payment through Gateway
    print("\n[PROCESS]  Payment Agent: Processing payment through gateway...")

    result = payment_ledger.process_payment(payment.payment_id)

    if result['success']:
        payment = payment_ledger.get_payment(payment.payment_id)

        print(f"\n   [OK] Payment Processed Successfully!")
        print(f"   [OK] Status: {payment.status.value}")
        print(f"   [OK] Gateway: {payment.gateway.value}")
        print(f"   [OK] Transaction ID: {payment.gateway_transaction_id}")
        print(f"   [OK] Amount charged: ${payment.amount} {payment.currency}")

        ledger.update_task_status("payment", TaskStatus.COMPLETED)

    # ===== STEP 6: Confirm Order =====
    print_step(6, "Customer Service - Order Confirmation")

    ledger.update_task_status("confirm", TaskStatus.IN_PROGRESS)

    print("[EMAIL] Sending order confirmation to customer...")
    print()
    print("="*60)
    print("          ORDER CONFIRMATION")
    print("="*60)
    print(f"Order ID: ORD_{int(time.time())}")
    print(f"Customer: {customer_id}")
    print(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("Items:")
    print(f"  {quantity}x {product['name']}")
    print(f"  Price: ${product['price']} each")
    print()
    print(f"Subtotal: ${subtotal}")
    print(f"Shipping ({destination}): ${shipping_cost}")
    print("-"*60)
    print(f"TOTAL PAID: ${total_amount}")
    print()
    print(f"Payment Method: {payment.payment_method.value}")
    print(f"Transaction ID: {payment.gateway_transaction_id}")
    print()
    print(f"Shipping to: {destination}")
    print("Estimated delivery: 3-5 business days")
    print("="*60)
    print()

    ledger.update_task_status("confirm", TaskStatus.COMPLETED)

    # ===== FINAL SUMMARY =====
    print_section("WORKFLOW SUMMARY")

    # Task Summary
    print("[TASKS] Task Status:")
    summary = ledger.get_progress_summary()
    print(f"   Total tasks: {summary['total']}")
    print(f"   Completed: {summary['completed']}")
    print(f"   Progress: {summary['progress']}")
    print()

    # Payment Summary
    print("[CARD] Payment Details:")
    print(f"   Payment ID: {payment.payment_id}")
    print(f"   Amount: ${payment.amount} {payment.currency}")
    print(f"   Status: {payment.status.value}")
    print(f"   Gateway: {payment.gateway.value}")
    print(f"   Transaction: {payment.gateway_transaction_id}")
    print()

    # Inventory Summary
    print("[INVENTORY] Inventory Update:")
    print(f"   {product['name']}")
    print(f"   Previous stock: {PRODUCTS[product_id]['stock']}")
    print(f"   Sold: {quantity}")
    print(f"   Remaining: {inventory[product_id]['stock']}")
    print()

    # AP2 Features Demonstrated
    print("[STAR] AP2 Features Demonstrated:")
    print("   [OK] Payment request creation")
    print("   [OK] Payment authorization workflow")
    print("   [OK] Payment gateway processing")
    print("   [OK] Transaction ledger persistence")
    print("   [OK] Payment metadata tracking")
    print("   [OK] Multi-step payment lifecycle")
    print()

    print_section("DEMO COMPLETE")

    print("[PARTY] Successfully demonstrated AP2 in e-commerce workflow!")
    print()
    print("Next steps to explore:")
    print("  1. Check payment_ledger.json in agent_data/ for persisted transactions")
    print(f"  2. Run test_ap2_integration.py for comprehensive AP2 tests")
    print("  3. Try demo_ecommerce_ap2.py for full multi-agent LLM version")
    print()


if __name__ == "__main__":
    run_simple_ecommerce_demo()
