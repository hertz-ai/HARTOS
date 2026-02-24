"""
Interactive agent creation script.
Handles the conversational flow with the gather_info process.
"""
import requests
import json

BASE_URL = "http://localhost:6777"
USER_ID = 10077
PROMPT_ID = 8888  # Changed to new ID for fresh start

# Read requirements
with open("coding_agent_requirements.txt", "r") as f:
    requirements = f.read()

# Predefined answers to common questions
ANSWERS = {
    "broadcast": "no",
    "schedule": "no",
    "time_based": "no",
    "scheduled_tasks": "no",
    "web_search": "yes",
    "computer_use": "yes",  # For VLM agent integration
    "code_execution": "yes",
    "file_operations": "yes"
}

def send_message(prompt_text):
    """Send a message to the /chat endpoint"""
    payload = {
        "user_id": USER_ID,
        "prompt_id": PROMPT_ID,
        "create_agent": True,
        "prompt": prompt_text
    }

    response = requests.post(
        f"{BASE_URL}/chat",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=300
    )

    return response.json()

def find_answer(question):
    """Find an appropriate answer based on the question"""
    question_lower = question.lower()

    if "how many" in question_lower and ("flow" in question_lower or "persona" in question_lower):
        return "1 - Single comprehensive flow that handles all SDLC phases"
    elif "broadcast" in question_lower:
        return "no"
    elif "schedule" in question_lower or "time-based" in question_lower or "scheduled_tasks" in question_lower:
        return "no"
    elif "search" in question_lower or "web" in question_lower:
        return "yes"
    elif "computer" in question_lower or "vlm" in question_lower or "visual" in question_lower:
        return "yes - for UI/UX validation using VLM agent"
    elif "code" in question_lower or "execution" in question_lower:
        return "yes"
    elif "file" in question_lower:
        return "yes"
    elif "separate" in question_lower and "flow" in question_lower:
        return "no - Keep it as one comprehensive flow handling all phases"
    else:
        # Default to describing what the agent does
        return "This is a professional software development agent with a single comprehensive flow that handles all SDLC phases using nested task ledger."

print("=" * 80)
print("INTERACTIVE PROFESSIONAL CODING AGENT CREATION")
print("=" * 80)

# Step 1: Initial request
print("\n[1] Sending initial request with requirements...")
initial_message = f"""I want to create a new agent recipe:

{requirements}
"""

result = send_message(initial_message)
print(f"\nAgent Status: {result.get('Agent_status', 'Unknown')}")
print(f"Response: {result['response']}")

# Step 2: Continue the conversation
conversation_count = 1
max_turns = 20

while result.get('Agent_status') == 'Creation Mode' and conversation_count < max_turns:
    conversation_count += 1
    question = result['response']

    print(f"\n[{conversation_count}] Question from system:")
    print(f"  {question}")

    # Try to find an automatic answer
    answer = find_answer(question)

    print(f"\n  Auto-answer: {answer}")

    # Send the answer
    result = send_message(answer)

    if 'response' in result:
        print(f"\n  System response: {result['response'][:200]}...")

    # Check if we're done
    if result.get('Agent_status') != 'Creation Mode':
        print("\n" + "=" * 80)
        print("AGENT CREATION COMPLETED!")
        print("=" * 80)
        break

# Check for created files
print("\n" + "-" * 80)
print("Checking for created recipe files...")
print("-" * 80)

import os
import glob

recipe_files = glob.glob(f"prompts/{PROMPT_ID}*.json")
agent_files = glob.glob(f"agent_data/{USER_ID}_{PROMPT_ID}.json")

print(f"\nRecipe files: {len(recipe_files)}")
for f in recipe_files:
    size = os.path.getsize(f)
    print(f"  - {f} ({size} bytes)")

print(f"\nAgent data files: {len(agent_files)}")
for f in agent_files:
    size = os.path.getsize(f)
    print(f"  - {f} ({size} bytes)")

if recipe_files or agent_files:
    print("\n" + "=" * 80)
    print("SUCCESS: Professional Coding Agent recipe created!")
    print("=" * 80)
    print(f"\nPrompt ID: {PROMPT_ID}")
    print(f"User ID: {USER_ID}")
    print("\nNext: Test with reuse_recipe.py")
else:
    print("\nWARNING: Agent creation may still be in progress")
    print(f"Final status: {result.get('Agent_status')}")
    print(f"Final response: {result.get('response', 'No response')}")
