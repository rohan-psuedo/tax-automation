import os
import tempfile
from pathlib import Path

# Point the app at a throwaway database before any app module is imported.
_TMP = Path(tempfile.mkdtemp(prefix="tax-automaton-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-with-enough-length-for-hs256"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["BACKUP_DIR"] = str(_TMP / "backups")
os.environ["LOG_DIR"] = str(_TMP / "logs")
os.environ["RUN_WORKER_IN_PROCESS"] = "false"  # tests drive the worker explicitly

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.models  # noqa: E402,F401
from app.config import get_settings  # noqa: E402
from app.connectors.tally import TallyConnector  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.devtools.mock_tally import DEMO_COMPANY, MockTally, httpx_transport  # noqa: E402
from app.main import app  # noqa: E402
from app.services import app_settings, login_guard  # noqa: E402
from app.services.connectors import get_connector_factory  # noqa: E402


def _inside_tmp(path: str | Path | None) -> bool:
    return path is not None and Path(path).resolve().is_relative_to(_TMP.resolve())


# The environment above only takes effect if no app module was imported earlier in this
# process (e.g. a script that imports app code and then calls pytest.main()). In that case
# the engine already points at the real database and every test teardown would drop its
# tables, so refuse to run at all.
_settings = get_settings()
if not all(
    _inside_tmp(p)
    for p in (engine.url.database, _settings.storage_dir, _settings.backup_dir, _settings.log_dir)
):
    pytest.exit(
        "Refusing to run tests: the app is configured for the real database or storage "
        f"({engine.url.database}, {_settings.storage_dir}, {_settings.backup_dir}). "
        "Run pytest in a fresh "
        "process; do not import app modules before pytest.main().",
        returncode=3,
    )


@pytest.fixture(autouse=True)
def _fresh_db():
    app_settings.invalidate()
    login_guard.reset()
    assert _inside_tmp(engine.url.database), "tests must never touch the real database"
    Base.metadata.create_all(engine)
    yield
    assert _inside_tmp(engine.url.database), "tests must never touch the real database"
    Base.metadata.drop_all(engine)
    app_settings.invalidate()
    login_guard.reset()


@pytest.fixture
def mock_tally() -> MockTally:
    return MockTally()


@pytest.fixture
def connector(mock_tally: MockTally) -> TallyConnector:
    return TallyConnector("http://tally.test:9000", transport=httpx_transport(mock_tally))


@pytest.fixture
def client(mock_tally: MockTally):
    transport = httpx_transport(mock_tally)
    app.dependency_overrides[get_connector_factory] = lambda: (
        lambda url: TallyConnector(url, transport=transport)
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    resp = client.post(
        "/api/auth/setup",
        json={"email": "admin@ca.test", "full_name": "Admin", "password": "s3cret-pass"},
    )
    assert resp.status_code == 201, resp.text
    return client


@pytest.fixture
def company_id(admin_client: TestClient) -> int:
    resp = admin_client.post(
        "/api/companies",
        json={
            "name": "Demo Traders",
            "external_company_name": DEMO_COMPANY,
            "gstin": "29AAACD1234A1ZD",
            "state": "Karnataka",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]
