import os
import sys

sys.path.append(os.path.dirname(__file__))
from mcp_server import get_top_risk_items

print("====================================")
print("Testing MCP Server logic directly...")
print("====================================")
try:
    result = get_top_risk_items(top_n=5)
    print("\n--- Result from get_top_risk_items ---")
    print(result)
    print("--------------------------------------")
except Exception as e:
    print(f"Error occurred during test: {e}")
    raise
