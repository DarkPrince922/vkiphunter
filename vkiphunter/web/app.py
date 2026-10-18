"""One-process aiohttp service. Cloud operations reuse Hunter and CloudProxy."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from pydantic import ValidationError

from ..cloud import CloudError, CloudProxy, build_backend
from ..config import Settings
from ..hunter import Hunter
from ..logs import setup_logging, tail
from ..openrc import parse_openrc
from ..secrets import CloudCredentials, SecretStore
from ..state import HuntSettings, MODE_FLOATING_IP, Store
from .models import (
    AttachIP, ConfirmInput, ConnectionInput, CreateVM, HuntInput, LoginInput,
)

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
COOKIE = "vkh_session"
SESSION_TTL = 12 * 3600


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.state_file)
        self.store.load()
        self.secrets = SecretStore(settings.credentials_file)
        self.secrets.load()
        self.cloud = CloudProxy(self.credentials)
        self.hunter = Hunter(self.cloud, self.store)
        self.mutation = asyncio.Lock()
        self.sessions: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.failures: OrderedDict[str, list[float]] = OrderedDict()
        self.lock_file = None

    def credentials(self):
        stored = self.secrets.secrets.cloud
        return stored if stored.configured else self.settings.env_credentials()

    def idle(self):
        if self.hunter.running:
            raise web.HTTPConflict(text="Сначала остановите поиск и дождитесь завершения попытки")

    def redact(self, value: str):
        for cred in (self.credentials(), self.settings.env_credentials()):
            for secret in (cred.password, cred.app_cred_secret):
                if secret:
                    value = value.replace(secret, "[скрыто]")
        return value


RUNTIME = web.AppKey("runtime", Runtime)


@web.middleware
async def boundary(request, handler):
    try:
        response = await handler(request)
    except ValidationError as exc:
        # Never include input values (passwords / OpenRC) in validation errors.
        response = web.json_response({"error": "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in exc.errors(include_input=False, include_context=False)
        )}, status=400)
    except (json.JSONDecodeError, UnicodeDecodeError):
        response = web.json_response({"error": "Ожидается JSON-объект"}, status=400)
    except CloudError as exc:
        response = web.json_response({"error": request.app[RUNTIME].redact(str(exc))}, status=502)
    except web.HTTPException as exc:
        response = web.json_response({"error": exc.text}, status=exc.status)
    except Exception:
        log.exception("HTTP request failed: %s", request.path)
        response = web.json_response({"error": "Ошибка сервера. Проверьте журнал службы."}, status=500)
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                   "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })
    return response


@web.middleware
async def security(request, handler):
    rt = request.app[RUNTIME]
    if request.method not in ("GET", "HEAD"):
        if request.headers.get("X-VKH-Request") != "1":
            raise web.HTTPForbidden(text="Отсутствует защитный заголовок")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Нужен application/json")
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text="Запрос с другого сайта запрещён")
    if request.path.startswith("/api/") and request.path != "/api/login":
        session = rt.sessions.get(request.cookies.get(COOKIE, ""))
        if not session or session[0] < time.time():
            raise web.HTTPUnauthorized(text="Войдите в панель")
        if request.method not in ("GET", "HEAD") and not secrets.compare_digest(
            request.headers.get("X-CSRF-Token", ""), session[1]
        ):
            raise web.HTTPForbidden(text="Неверный CSRF-токен")
    return await handler(request)


@web.middleware
async def serialize_writes(request, handler):
    if request.method == "POST" and request.path not in ("/api/login", "/api/logout"):
        rt = request.app[RUNTIME]
        if rt.mutation.locked():
            raise web.HTTPConflict(text="Операция уже выполняется. Дождитесь её завершения.")
        async with rt.mutation:
            return await handler(request)
    return await handler(request)


async def payload(request, model):
    return model.model_validate(await request.json())


async def login(request):
    rt = request.app[RUNTIME]
    data = await payload(request, LoginInput)
    now = time.time()
    remote = request.remote or "unknown"  # Never trust client-supplied forwarding headers.
    failures = [t for t in rt.failures.pop(remote, []) if t > now - 60]
    rt.failures[remote] = failures
    while len(rt.failures) > 1024:
        rt.failures.popitem(last=False)
    if len(failures) >= 10:
        raise web.HTTPTooManyRequests(text="Слишком много попыток. Подождите минуту.")
    if not secrets.compare_digest(data.password.encode(), rt.settings.web_password.encode()):
        failures.append(now)
        raise web.HTTPUnauthorized(text="Неверный пароль")
    rt.failures.pop(remote, None)
    for token, session in list(rt.sessions.items()):
        if session[0] < now:
            rt.sessions.pop(token, None)
    rt.sessions.pop(request.cookies.get(COOKIE, ""), None)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    rt.sessions[token] = (now + SESSION_TTL, csrf)
    while len(rt.sessions) > 32:
        rt.sessions.popitem(last=False)
    response = web.json_response({"csrf": csrf})
    response.set_cookie(COOKIE, token, httponly=True, samesite="Strict",
                        secure=rt.settings.web_secure_cookie, max_age=SESSION_TTL, path="/")
    return response


async def logout(request):
    request.app[RUNTIME].sessions.pop(request.cookies.get(COOKIE, ""), None)
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE, path="/")
    return response


async def status(request):
    rt = request.app[RUNTIME]
    session = rt.sessions[request.cookies[COOKIE]]
    state = rt.store.state.to_dict()
    state.pop("subscribers", None)
    state.pop("live_message", None)
    state.update(status=rt.hunter.status.value, running=rt.hunter.running,
                 next_attempt_at=rt.hunter.next_attempt_at, current_attempt=rt.hunter.current_attempt,
                 last_error=rt.hunter.last_error, account=rt.hunter.account,
                 connected=rt.cloud.connected, cloud=rt.credentials().describe(), csrf=session[1],
                 busy=rt.mutation.locked())
    def safe(value):
        if isinstance(value, str):
            return rt.redact(value)
        if isinstance(value, list):
            return [safe(item) for item in value]
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        return value
    return web.json_response(safe(state))


async def save_settings(request):
    rt = request.app[RUNTIME]
    rt.idle()
    data = await payload(request, HuntInput)
    values = data.model_dump()
    if data.network != rt.store.state.settings.network:
        values["subnets"] = []
    rt.store.state.settings = HuntSettings(**values)
    await rt.store.save()
    return web.json_response({"ok": True})


async def connect(request):
    rt = request.app[RUNTIME]
    rt.idle()
    data = await payload(request, ConnectionInput)
    values = data.model_dump(exclude={"openrc"})
    if data.openrc:
        raw = parse_openrc(data.openrc)
        mapping = {"OS_AUTH_URL": "auth_url", "OS_REGION_NAME": "region", "OS_USERNAME": "username",
                   "OS_PROJECT_ID": "project_id", "OS_PROJECT_NAME": "project_name",
                   "OS_USER_DOMAIN_NAME": "user_domain_name", "OS_PASSWORD": "password",
                   "OS_APPLICATION_CREDENTIAL_ID": "app_cred_id",
                   "OS_APPLICATION_CREDENTIAL_SECRET": "app_cred_secret"}
        for env, field in mapping.items():
            if env in raw:
                values[field] = raw[env]
        # A manually supplied secret overrides the usual $OS_PASSWORD placeholder.
        if data.password:
            values["password"] = data.password
        if data.app_cred_secret:
            values["app_cred_secret"] = data.app_cred_secret
    credentials = CloudCredentials(**values)
    if not credentials.configured:
        raise web.HTTPBadRequest(text="Заполните доступы к облаку")
    if not credentials.simulate:
        url = urlsplit(credentials.auth_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise web.HTTPBadRequest(text="Auth URL должен быть HTTPS-адресом Keystone без логина и пароля в URL")
    candidate = build_backend(credentials)
    try:
        await candidate.check(rt.store.state.settings.network)
    except CloudError:
        # Backend responses might contain supplied secrets; do not echo them.
        raise web.HTTPBadRequest(text="Проверка облака не прошла. Проверьте Auth URL, регион, проект и доступы.") from None
    finally:
        await candidate.close()
    previous = rt.secrets.secrets.cloud
    rt.secrets.secrets.cloud = credentials
    try:
        rt.secrets.save()
    except Exception:
        rt.secrets.secrets.cloud = previous
        raise
    await rt.cloud.reset()
    return web.json_response({"ok": True, "cloud": credentials.describe()})


async def disconnect(request):
    rt = request.app[RUNTIME]
    rt.idle()
    await payload(request, ConfirmInput)
    if rt.settings.env_credentials().configured:
        raise web.HTTPConflict(text="Сначала уберите OS_* или SIMULATE из .env и перезапустите службу: они служат запасным подключением")
    if rt.store.state.found:
        # Found addresses stay protected in the persistent state.
        log.info("Disconnecting cloud; found resources are retained")
    rt.secrets.secrets.cloud = CloudCredentials()
    rt.secrets.save()
    await rt.cloud.reset()
    return web.json_response({"ok": True})


async def start(request):
    rt = request.app[RUNTIME]
    if rt.hunter.running:
        raise web.HTTPConflict(text="Поиск уже запущен")
    await rt.cloud.check(rt.store.state.settings.network)
    await rt.hunter.start()
    return web.json_response({"ok": True})


async def stop(request):
    await request.app[RUNTIME].hunter.stop(wait=False)
    return web.json_response({"ok": True})


async def cloud_read(request):
    rt = request.app[RUNTIME]
    name = request.match_info["name"]
    network = rt.store.state.settings.network
    calls = {
        "check": lambda: rt.cloud.check(network),
        "networks": rt.cloud.list_networks,
        "subnets": lambda: rt.cloud.list_subnets(network),
        "availability": lambda: rt.cloud.ip_availability(network),
        "ports": lambda: rt.cloud.list_ports(network),
        "floatingips": rt.cloud.list_floating_ips,
        "servers": rt.cloud.list_servers,
        "flavors": rt.cloud.list_flavors,
        "images": rt.cloud.list_images,
        "keypairs": rt.cloud.list_keypairs,
    }
    if name not in calls:
        raise web.HTTPNotFound(text="Неизвестный ресурс")
    result = await calls[name]()
    if name == "keypairs":
        return web.json_response(result)
    if isinstance(result, list):
        result = [dict(asdict(item), **({"free": item.free} if name == "availability" else {})) for item in result]
    else:
        result = asdict(result)
    return web.json_response(result)


async def cleanup(request):
    rt = request.app[RUNTIME]
    rt.idle()
    await payload(request, ConfirmInput)
    settings = rt.store.state.settings
    protected = {f.port_id for f in rt.store.state.found}
    floating = settings.mode == MODE_FLOATING_IP
    if floating:
        items = [(f.id, f.description, f.attached) for f in await rt.cloud.list_floating_ips()]
    else:
        items = [(p.id, p.name, bool(p.device_id)) for p in await rt.cloud.list_ports(settings.network)]
    deleted, failed = [], []
    for resource_id, label, attached in items:
        if not label.startswith(settings.prefix + "-") or resource_id in protected or attached:
            continue
        try:
            if floating:
                await rt.cloud.delete_floating_ip(resource_id)
            else:
                await rt.cloud.delete_port(resource_id)
            deleted.append(resource_id)
        except CloudError:
            failed.append(resource_id)
    return web.json_response({"deleted": deleted, "failed": failed})


def found_resource(rt, resource_id, kind):
    rt.idle()
    found = next((f for f in rt.store.state.found if f.port_id == resource_id and f.kind == kind), None)
    if not found or found.server_id:
        raise web.HTTPConflict(text="Нужен найденный и ещё не подключённый ресурс подходящего типа")
    return found


async def create_vm(request):
    rt = request.app[RUNTIME]
    data = await payload(request, CreateVM)
    found = found_resource(rt, data.port_id, "port")
    port = next((p for p in await rt.cloud.list_ports(found.network) if p.id == found.port_id), None)
    if port is None or port.device_id:
        raise web.HTTPConflict(text="Порт отсутствует или уже подключён в облаке")
    settings = rt.store.state.settings
    server = await rt.cloud.create_server(
        name=data.name, flavor_id=data.flavor_id, image_id=data.image_id,
        port_id=found.port_id, key_name=data.key_name or None,
        volume_size=settings.volume_size, volume_type=settings.volume_type, timeout=settings.server_timeout,
    )
    found.server_id, found.server_name = server.id, server.name
    await rt.store.save()
    return web.json_response(asdict(server))


async def attach(request):
    rt = request.app[RUNTIME]
    data = await payload(request, AttachIP)
    found = found_resource(rt, data.port_id, MODE_FLOATING_IP)
    fip = next((f for f in await rt.cloud.list_floating_ips() if f.id == found.port_id), None)
    if fip is None or fip.attached:
        raise web.HTTPConflict(text="Floating IP отсутствует или уже подключён в облаке")
    servers = await rt.cloud.list_servers()
    server = next((s for s in servers if s.id == data.server_id), None)
    if server is None:
        raise web.HTTPBadRequest(text="ВМ не найдена")
    await rt.cloud.attach_floating_ip(found.port_id, server.id)
    found.server_id, found.server_name = server.id, server.name
    await rt.store.save()
    return web.json_response({"ok": True})


async def logs(request):
    rt = request.app[RUNTIME]
    return web.json_response({"text": rt.redact(tail(rt.settings.log_file, 150))})


async def health(request):
    return web.json_response({"ok": True})


async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def asset(request):
    # A fixed allowlist prevents arbitrary filesystem access.
    name = request.match_info["name"]
    if name not in ("app.js", "style.css"):
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / name)


async def lifecycle(app):
    rt = app[RUNTIME]
    lock_path = rt.settings.state_file.with_suffix(".web.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    rt.lock_file = lock_path.open("a")
    try:
        fcntl.flock(rt.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        rt.lock_file.close()
        raise RuntimeError("Другой веб-процесс уже использует STATE_FILE") from None
    # Reload after acquiring the lock in case another process just exited.
    rt.store.load()
    rt.secrets.load()
    try:
        yield
    finally:
        try:
            await rt.hunter.stop(wait=True)
            await rt.store.save()
            await rt.cloud.close()
        finally:
            rt.lock_file.close()


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    if len(settings.web_password) < 16:
        raise ValueError("Задайте WEB_PASSWORD длиной не менее 16 символов в .env")
    app = web.Application(middlewares=[boundary, security, serialize_writes], client_max_size=64 * 1024)
    app[RUNTIME] = Runtime(settings)
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", asset)
    app.router.add_get("/healthz", health)
    for path, handler in {
        "login": login, "logout": logout, "settings": save_settings,
        "connect": connect, "disconnect": disconnect, "hunt/start": start,
        "hunt/stop": stop, "cleanup": cleanup, "vm": create_vm, "attach": attach,
    }.items():
        app.router.add_post("/api/" + path, handler)
    app.router.add_get("/api/state", status)
    app.router.add_get("/api/cloud/{name}", cloud_read)
    app.router.add_get("/api/logs", logs)
    return app


def main():
    settings = Settings()
    setup_logging(settings.log_level, settings.log_file)
    web.run_app(create_app(settings), host=settings.web_host, port=settings.web_port,
                access_log=None, shutdown_timeout=1900)
