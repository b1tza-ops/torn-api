#!/usr/bin/env python3
import os
from pathlib import Path

from tornbot.migrate import migrate_database

# Upgrade databases made by older bot versions without deleting portfolio/history.
migrate_database(Path(os.getenv("TORN_DB", "torn_deals.sqlite3")))

from tornbot.core import main

if __name__ == "__main__":
    main()
