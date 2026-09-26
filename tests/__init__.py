import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "hri_manager"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import logging
import os

# the manager's warnings (refused peers, failed jobs) are expected in the tests; HRI_MGR_TEST_LOG=1 shows them
if not os.environ.get("HRI_MGR_TEST_LOG"):
    logging.getLogger("hrimgr").setLevel(logging.CRITICAL)
