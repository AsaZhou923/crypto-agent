import pytest
from fastapi.testclient import TestClient

from crypto_agent.api.app import create_app

ORIGIN = "https://optiplex.tail8fb77a.ts.net:8447"


class Scheduler:
    def supported(self):
        return True

    def apply(self, action):
        return {"action": action}

    def health(self):
        return {"status": "degraded", "reason": "paused"}


def test_tailnet_requires_explicit_host_and_same_origin_control(tmp_path):
    app = create_app(demo_mode=True, root=tmp_path, external_origin=ORIGIN, scheduler=Scheduler())
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health", headers={"host": "evil.ts.net:8447"}).status_code == 403
        assert client.get("/api/health", headers={"origin": "https://evil.ts.net"}).status_code == 403
        assert client.post("/api/scheduler", json={"action": "start"}).status_code == 403
        headers = {"origin": ORIGIN, "x-crypto-agent-control": "1"}
        assert client.post("/api/scheduler", headers=headers, json={"action": "start"}).status_code == 200
        headers["origin"] = "http://localhost"
        assert client.post("/api/scheduler", headers=headers, json={"action": "start"}).status_code == 403
        assert client.get("/api/health/scheduler").status_code == 503


def test_tailnet_disabled_by_default(tmp_path):
    with TestClient(create_app(demo_mode=True, root=tmp_path), base_url=ORIGIN) as client:
        assert client.get("/api/health").status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "http://optiplex.tail8fb77a.ts.net:8447",
        "https://public.example.com",
        ORIGIN + "/",
        ORIGIN + "/path",
        ORIGIN + "?x=y",
        ORIGIN + "#fragment",
        "https://user@optiplex.tail8fb77a.ts.net",
        "https://optiplex.tail8fb77a.ts.net:bad",
    ],
)
def test_invalid_external_origins_fail_at_startup(tmp_path, origin):
    with pytest.raises(ValueError):
        create_app(demo_mode=True, root=tmp_path, external_origin=origin)
