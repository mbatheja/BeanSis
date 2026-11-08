"""Run the retention purge by hand or from cron:  python -m app.retention"""
from .appstate import Store

if __name__ == "__main__":
    print(f"deleted {Store().purge_expired()} expired chat(s)")
