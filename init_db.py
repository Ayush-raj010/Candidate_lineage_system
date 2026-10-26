"""Create or update data/candidates.db from candidates.json.

Usage: python init_db.py [--dry-run] [--json PATH] [--db PATH]

Exit codes: 0 = success, 1 = imported but some records were invalid,
2 = could not run (missing file, bad JSON, old database schema).
"""

import sys

from app.importer import main

if __name__ == "__main__":
    sys.exit(main())
