"""Central settings. Override any value with an environment variable of the same name."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))

# Read-only reference data built offline by ingest/*: documents, trials, providers, plans.
REFERENCE_DB = Path(os.getenv("REFERENCE_DB", DATA_DIR / "reference.duckdb"))
# App state (profiles, threads, memory). sqlite for local dev, a Postgres URL in deployment.
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
# Persistent HTTP response cache for PubMed / openFDA / RxNav.
CACHE_DB = Path(os.getenv("CACHE_DB", DATA_DIR / "http_cache.sqlite"))

# Which LLM backend get_client() builds: "anthropic" (default) or "nebius" (Nebius Token Factory).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
NEBIUS_API_KEY = os.getenv("NEBIUS_API_KEY")
NEBIUS_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.eu-north1.nebius.com/v1/")
# Optional: run the eval judge on a different provider than the app (so a model doesn't grade itself).
JUDGE_PROVIDER = os.getenv("JUDGE_PROVIDER")

# Fast model: safety check, summaries, directory extraction. Main model: answers.
# With LLM_PROVIDER=nebius use exact Token Factory IDs, e.g. "Qwen/Qwen3-30B-A3B-Instruct-2507".
MODEL_MAIN = os.getenv("MODEL_MAIN", "claude-sonnet-5-5")
MODEL_FAST = os.getenv("MODEL_FAST", "claude-haiku-5-5")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1500"))

MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "3"))
MAX_REVISIONS = int(os.getenv("MAX_REVISIONS", "1"))              # evaluator-optimizer retries (0 disables)
REVISION_TOOL_ROUNDS = int(os.getenv("REVISION_TOOL_ROUNDS", "1"))  # tool rounds allowed during a revision
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "6"))        # recent messages sent verbatim
SUMMARIZE_AFTER = int(os.getenv("SUMMARIZE_AFTER", "12"))   # older ones get folded into a summary
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "America/Los_Angeles")  # California users
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))    # chats deleted this long after last use; pins kept
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "90"))  # meal + dose logs; med list kept until removed
REMINDER_WINDOW_MIN = int(os.getenv("REMINDER_WINDOW_MIN", "10"))  # a dose is "due" up to this many minutes late
REMINDER_WEBHOOK_URL = os.getenv("REMINDER_WEBHOOK_URL")       # where due reminders are POSTed (email/SMS/push relay)
REMINDER_WEBHOOK_SECRET = os.getenv("REMINDER_WEBHOOK_SECRET")  # sent as x-relay-secret; the relay must check it
FDC_API_KEY = os.getenv("FDC_API_KEY", "DEMO_KEY")              # USDA FoodData Central; DEMO_KEY is heavily rate-limited
SAFETY_LLM_CHECK = os.getenv("SAFETY_LLM_CHECK", "1") == "1"  # rules always run; LLM check optional

NCBI_API_KEY = os.getenv("NCBI_API_KEY")        # optional; raises PubMed limit to 10 req/s
OPENFDA_API_KEY = os.getenv("OPENFDA_API_KEY")  # optional
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "team@example.com")  # NCBI asks for a contact

TTL = {  # cache lifetimes (seconds), matched to how often each source changes
    "pubmed": 3 * 86400,
    "openfda": 7 * 86400,
    "rxnav": 30 * 86400,
    "fdc": 90 * 86400,
}
