import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir + mock provider for every test run (set before vigil is imported).
os.environ["VIGIL_DATA_DIR"] = tempfile.mkdtemp(prefix="vigil-test-")
os.environ["VIGIL_PROVIDER"] = "mock"
os.environ.pop("DETECTIF_API_KEY", None)
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_AUTH_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
