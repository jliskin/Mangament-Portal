from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .security import NotAuthenticated
from .web import auth, bank, charges, dashboard, expenses, leases, reports, settings_page, tenants, units
from .web.common import redirect

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, docs_url="/api/docs" if s.is_dev else None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="cp_session",
                       max_age=s.session_max_age_s, same_site="lax", https_only=s.session_cookie_secure)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if s.session_cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.exception_handler(NotAuthenticated)
    async def _login_redirect(request: Request, exc: NotAuthenticated):
        if request.headers.get("hx-request"):
            return PlainTextResponse("", headers={"HX-Redirect": "/login"})
        return redirect("/login")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    for module in (auth, dashboard, units, tenants, leases, charges, bank, expenses, reports, settings_page):
        app.include_router(module.router)
    app.include_router(bank.api)  # machine-to-machine: HMAC-authenticated, no session/CSRF
    return app


app = create_app()
