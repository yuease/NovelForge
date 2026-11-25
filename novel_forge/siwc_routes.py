from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .openai_siwc import COOKIE, PREFIX, TTL, SiwcError, allowed_return_origin, is_loopback, listen_port


_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _local(request, *, canonical=False):
    server = request.scope.get("server")
    port = listen_port(server)
    peer = request.client
    host = request.headers.get("host", "")
    if not peer or not is_loopback(peer.host):
        raise SiwcError("SIWC_LOCAL_ONLY", 403, "ChatGPT 登录仅限本机访问。")
    if canonical:
        accepted = {f"127.0.0.1:{port}"} | ({"127.0.0.1"} if port == 80 else set())
        if host not in accepted:
            raise SiwcError("SIWC_LOCAL_ONLY", 403, "请使用本机 127.0.0.1 登录回调地址。")
    else:
        allowed_return_origin("http://" + host, port)
    return port


def _arg(request, name):
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise SiwcError("SIWC_TRANSACTION", 400, "登录回调包含重复参数。")
    return values[0] if values else ""


def _return(attempt, state):
    response = RedirectResponse(f"{attempt.origin}/#/models?siwc={state}", status_code=303, headers=_HEADERS)
    response.delete_cookie(COOKIE, path=PREFIX, httponly=True, samesite="lax")
    return response


def siwc_router(job_payload):
    router = APIRouter(prefix=PREFIX)

    @router.get("/status")
    async def status(request: Request):
        _local(request)
        return JSONResponse(request.app.state.store.openai_siwc.status(), headers=_HEADERS)

    @router.post("/start")
    async def start(request: Request):
        port = _local(request)
        payload = await job_payload(request)
        browser_origin = request.headers.get("origin") or "http://" + request.headers["host"]
        origin = allowed_return_origin(payload.get("returnOrigin", browser_origin), port)
        if origin != browser_origin:
            raise SiwcError("SIWC_RETURN_ORIGIN", 400, "登录返回地址须匹配当前工作台。")
        mode = payload.get("mode", "login")
        if mode not in ("login", "consent", "switch"):
            raise SiwcError("SIWC_TRANSACTION", 400, "登录操作无效。")
        service = request.app.state.store.openai_siwc
        return JSONResponse({"url": service.start(origin, port, mode)}, headers=_HEADERS)

    @router.get("/begin")
    async def begin(request: Request):
        port = _local(request, canonical=True)
        service = request.app.state.store.openai_siwc
        attempt, url = await service.begin(_arg(request, "ticket"), port)
        if url is None:
            return _return(attempt, "unavailable")
        response = RedirectResponse(url, status_code=302, headers=_HEADERS)
        response.set_cookie(COOKIE, attempt.cookie, max_age=TTL, path=PREFIX, httponly=True, samesite="lax")
        return response

    @router.get("/callback")
    async def callback(request: Request):
        port = _local(request, canonical=True)
        service = request.app.state.store.openai_siwc
        attempt = service.consume(_arg(request, "state"), request.cookies.get(COOKIE), port)
        try:
            if _arg(request, "error"):
                return _return(attempt, "denied")
            status = await service.exchange(attempt, _arg(request, "code"), _arg(request, "client_id"))
        except SiwcError:
            return _return(attempt, "failed")
        return _return(attempt, status["state"])

    @router.post("/sign-out")
    async def sign_out(request: Request):
        _local(request)
        service = request.app.state.store.openai_siwc
        confirmed = await service.sign_out()
        return JSONResponse({**service.status(), "revocationConfirmed": confirmed}, headers=_HEADERS)

    return router
