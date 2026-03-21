import os
import snowflake.connector
from dotenv import load_dotenv

load_dotenv()

SF_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT")
SF_USER = os.getenv("SNOWFLAKE_USER")
SF_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD")
SF_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
SF_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "ICEBERG_ML")
SF_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "ADHOC")

print(f"Attempting to connect to Snowflake account: {SF_ACCOUNT} as user: {SF_USER}")

try:
    conn = snowflake.connector.connect(
        account=SF_ACCOUNT,
        user=SF_USER,
        password=SF_PASSWORD,
        warehouse=SF_WAREHOUSE,
        database=SF_DATABASE,
        schema=SF_SCHEMA,
    )
    print("Successfully connected to Snowflake!")
    cur = conn.cursor()
    cur.execute("SELECT CURRENT_REGION(), CURRENT_ACCOUNT()")
    region, account = cur.fetchone()
    print(f"Region: {region}, Account: {account}")
    conn.close()
except Exception as e:
    print(f"Failed to connect to Snowflake: {e}")
