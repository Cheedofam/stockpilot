"""PythonAnywhere WSGI entry point.

Point the web app's WSGI file at this module, e.g.:
    import sys
    sys.path.insert(0, '/home/BrendenThambipillai/stockpilot')
    from wsgi import application
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.app import app as application  # noqa: E402,F401
