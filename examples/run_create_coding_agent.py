"""
Automated script to create Professional Coding Agent using create_recipe flow.
This script interacts with the Flask server to create the agent recipe.
"""
import os
import requests
import json
import time

# Configuration
BASE_URL = "http://localhost:6777"
USER_ID = 10077
PROMPT_ID = 9999  # Use unique ID for coding agent

# Read the requirements
with open(os.path.join(os.path.dirname(__file__), "coding_agent_requirements.txt"), "r") as f:
    requirements = f.read()

print("=" * 80)
print("CREATING PROFESSIONAL CODING AGENT")
print("=" * 80)
print(f"\nUser ID: {USER_ID}")
print(f"Prompt ID: {PROMPT_ID}")
print(f"\nRequirements loaded ({len(requirements)} characters)")

# Step 1: Initiate create_recipe flow
print("\n" + "-" * 80)
print("STEP 1: Initiating create_recipe flow...")
print("-" * 80)

user_message = f"""I want to create a new agent recipe with the following specifications:

{requirements}

Please help me create this Professional Coding Agent that uses the nested task ledger system with deterministic auto-resume and event-driven architecture.
"""

payload = {
    "user_id": USER_ID,
    "prompt_id": PROMPT_ID,
    "create_agent": True,  # Flag to trigger agent creation
    "prompt": user_message  # Use 'prompt' not 'message'
}

print(f"\nSending request to {BASE_URL}/chat...")
print(f"Mode: create")
print(f"Message length: {len(user_message)} characters")

try:
    response = requests.post(
        f"{BASE_URL}/chat",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=300  # 5 minutes timeout
    )

    print(f"\nResponse status: {response.status_code}")

    if response.status_code == 200:
        result = response.json()
        print("\n" + "=" * 80)
        print("SUCCESS: Agent creation initiated")
        print("=" * 80)
        print(f"\nResponse:\n{json.dumps(result, indent=2)}")

        # Check if recipe was created
        print("\n" + "-" * 80)
        print("STEP 2: Checking created recipe files...")
        print("-" * 80)

        import os
        import glob

        # Check for created files
        recipe_files = glob.glob(f"prompts/{PROMPT_ID}*.json")
        agent_files = glob.glob(f"agent_data/{USER_ID}_{PROMPT_ID}.json")

        print(f"\nRecipe files found: {len(recipe_files)}")
        for f in recipe_files:
            size = os.path.getsize(f)
            print(f"  - {f} ({size} bytes)")

        print(f"\nAgent data files found: {len(agent_files)}")
        for f in agent_files:
            size = os.path.getsize(f)
            print(f"  - {f} ({size} bytes)")

        if recipe_files or agent_files:
            print("\n" + "=" * 80)
            print("AGENT RECIPE CREATED SUCCESSFULLY!")
            print("=" * 80)
            print("\nNext step: Test the agent using reuse_recipe.py")
            print(f"  User ID: {USER_ID}")
            print(f"  Prompt ID: {PROMPT_ID}")
        else:
            print("\nWARNING: No recipe files found yet. Agent may still be processing.")
            print("Check the Flask server logs for details.")
    else:
        print(f"\nERROR: Request failed with status {response.status_code}")
        print(f"Response: {response.text}")

except requests.exceptions.ConnectionError:
    print("\nERROR: Cannot connect to Flask server at " + BASE_URL)
    print("Make sure the server is running: python langchain_gpt_api.py")
except requests.exceptions.Timeout:
    print("\nERROR: Request timed out after 5 minutes")
    print("The agent creation may still be processing in the background.")
except Exception as e:
    print(f"\nERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 80)
print("Script completed")
print("=" * 80)
