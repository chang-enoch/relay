"""Keep tests away from the developer's live database.

relay_backend.main builds a default app at import time, which opens RELAY_DB.
Point it at a throwaway file before anything imports the package.
"""

import os
import tempfile

os.environ["RELAY_DB"] = os.path.join(tempfile.mkdtemp(prefix="relay-tests-"), "import.db")
