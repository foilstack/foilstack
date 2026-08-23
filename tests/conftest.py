import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_TMP = tempfile.mkdtemp(prefix="foilstack-tests-")
os.environ.setdefault("FOILSTACK_DATA_DIR", _TMP)


def _load_dotenv() -> None:
    """Read .env so tests reach the same database the stack is using.

    Without this, `tests/test_isolation.py` falls back to the default
    credentials, cannot connect, and *skips* — which it is designed to do on a
    laptop with no Docker, and which turns a rotated database password into
    twelve silently disabled security tests. That happened once, immediately
    after rotating it, and the suite still reported green.
    """
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

if "FOILSTACK_TEST_DATABASE_URL" not in os.environ and "POSTGRES_PASSWORD" in os.environ:
    os.environ["FOILSTACK_TEST_DATABASE_URL"] = (
        "postgresql+psycopg://{user}:{pw}@localhost:{port}/{db}".format(
            user=os.environ.get("POSTGRES_USER", "foilstack"),
            pw=os.environ["POSTGRES_PASSWORD"],
            port=os.environ.get("POSTGRES_PORT", "5434"),
            db=os.environ.get("POSTGRES_DB", "foilstack"),
        )
    )

# The tests drive the app themselves and must not inherit the deployment's
# account mode from .env.
os.environ.pop("FOILSTACK_MULTI_USER", None)
os.environ.pop("FOILSTACK_DATA_DIR", None)
os.environ["FOILSTACK_DATA_DIR"] = _TMP
