import os
from dotenv import load_dotenv

# Load from project root .env
load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "apache/airflow")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "apache")
GITHUB_REPO_NAME = os.getenv("GITHUB_REPO_NAME", "airflow")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#autobot-alerts")
HF_TOKEN = os.getenv("HF_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SCORER_ENDPOINT = os.getenv("SCORER_ENDPOINT", "")
REASONER_ENDPOINT = os.getenv("REASONER_ENDPOINT", "")
SCORER_THRESHOLD = float(os.getenv("SCORER_THRESHOLD", "0.8"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "1800"))

# Validation
if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN missing from .env")
