#!/usr/bin/env python3
#!/usr/bin/env python3
"""Programmatic database initializer for use in scripts or tests.

Import and call `initialize_db()` from other code. No CLI or interactive prompts.
"""
from dotenv import load_dotenv
from typing import Optional, Tuple

load_dotenv()

from dynamov2.database.create_db import create_db
from dynamov2.logger.logger import CustomLogger

if __name__ == "__main__":
    create_db()
