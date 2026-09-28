"""FastAPI transport. No broker mutation routes or cross-origin access."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from crypto_agent.api.scheduler import ControlError, SchedulerControl
from crypto_agent.api.service import Monitor
from crypto_agent.models import dumps


class SafeJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return dumps(content).encode("utf-8")


def local_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


class SchedulerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop"]


def create_app(*, demo_mode=False, config_dir=Path("config"), root=None, monitor=None, scheduler=None):
    monitor = monitor or Monitor(demo_mode=demo_mode, config_dir=config_dir, root=root)

    scheduler = scheduler or SchedulerControl(monitor)

    @asynccontextmanager
    async def lifespan(app):
        yield
        monitor.close()

    app = FastAPI(
        title="Crypto Agent · Paper Monitor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        default_response_class=SafeJSONResponse,
    )
    app.state.monitor = monitor
    app.state.scheduler = scheduler
    dist = monitor.root / "frontend" / "dist"

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        if not local_origin("http://" + request.headers.get("host", "")):
            return JSONResponse({"detail": "Loopback Host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and not local_origin(origin):
            return JSONResponse({"detail": "Local origin required"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site access disabled"}, status_code=403)
        control = request.method == "POST" and request.url.path == "/api/scheduler"
        if control:
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if origin != expected_origin or request.headers.get("x-crypto-agent-control") != "1":
                return JSONResponse({"detail": "Same-origin control request required"}, status_code=403)
        elif request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "Read-only monitor"}, status_code=405)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    def check_scenario(scenario):
        if not monitor.demo_mode and scenario != "normal":
            raise HTTPException(400, "演示场景仅允许在显式 --demo 模式使用。")

    @app.get("/api/dashboard")
    def dashboard(
        refresh: bool = False,
        scenario: Literal["normal", "stale", "partial", "empty", "disconnected"] = "normal",
    ):
        check_scenario(scenario)
        return monitor.dashboard(refresh=refresh, scenario=scenario)

    @app.get("/api/market")
    def market(
        timeframe: Literal["1Min", "5Min", "1Hour"] = "1Min",
        symbol: Literal["BTC/USD", "XRP/USD"] = "BTC/USD",
        refresh: bool = False,
        scenario: Literal["normal", "stale", "partial", "empty", "disconnected"] = "normal",
    ):
        check_scenario(scenario)
        return monitor.market(timeframe=timeframe, symbol=symbol, refresh=refresh, scenario=scenario)

    @app.get("/api/scheduler")
    def scheduler_status():
        return scheduler.status()

    @app.post("/api/scheduler")
    def scheduler_control(body: SchedulerAction):
        try:
            return scheduler.apply(body.action)
        except ControlError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "mode": "demo" if monitor.demo_mode else "paper",
            "read_only": not scheduler.supported(),
        }

    @app.get("/{path:path}")
    def static(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404)
        target = (dist / path).resolve()
        if not target.is_relative_to(dist.resolve()):
            raise HTTPException(404)
        if target.is_file():
            return FileResponse(target)
        if Path(path).suffix:
            raise HTTPException(404)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return Response("Frontend not built. Run npm ci && npm run build in frontend/.", status_code=503)

    return app
