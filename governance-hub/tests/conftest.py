import importlib
from pathlib import Path
import socket
import sys
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio


@pytest.fixture
def hub(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "hub.db"))
    # Run this service's tests in their own process: agents also import app.
    for name in ("app", "models", "compliance"):
        sys.modules.pop(name, None)
    attempts = []
    def blocked(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Unexpected network access")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    app = importlib.import_module("app")
    monkeypatch.setattr(app.compliance, "fetch_repo_structure", AsyncMock(return_value={"files": []}))
    yield app
    sys.modules["models"].engine.dispose()
    for name in ("app", "models", "compliance"):
        sys.modules.pop(name, None)
    assert not attempts


@pytest_asyncio.fixture
async def client(hub):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hub.app), base_url="http://test") as client:
        yield client
