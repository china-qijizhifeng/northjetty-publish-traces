#!/usr/bin/env python3
"""Publish a local file or directory through NorthJetty.

Subcommands
-----------
  start <path> [opts]   Serve a path and create a NorthJetty route.
                        Runs in the BACKGROUND by default (use --foreground to
                        stay attached). Prints the public URL once ready.
  stop <alias|all>      Stop a background publication and delete its route.
  list                  List running publications.
  logs <alias>          Show the log file for a background publication.

Backward compat: `nj-publish.py <path> [opts]` is treated as `start <path>`.

Examples
--------
  NORTHJETTY_API_KEY=sk-nj-xxx ./nj-publish.py ./report.html
  ./nj-publish.py ./dist --alias my-site --require-auth --share-days 1
  ./nj-publish.py list
  ./nj-publish.py stop my-site
  ./nj-publish.py stop all
"""

from __future__ import annotations

import argparse
import atexit
import getpass
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Allow `import njsite` even when invoked through a PATH symlink: resolve the
# real script location and add its directory to the import path.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import njsite  # noqa: E402  (import after the sys.path shim above)


DEFAULT_NORTHJETTY_ORIGIN = "https://north-jetty.xiaobei.top"
DEFAULT_KEY_ENV = "NORTHJETTY_API_KEY"
DEFAULT_AUTH_MODE = "authorization_bearer"
DEFAULT_CUSTOM_AUTH_HEADER = "X-NorthJetty-Route-Key"
DEFAULT_ALLOWED_CIDRS_ENV = "NORTHJETTY_ALLOWED_CIDRS"
ALIAS_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

STATE_HOME = Path(os.environ.get("NORTHJETTY_HOME", str(Path.home() / ".northjetty")))
STATE_DIR = STATE_HOME / "run"
LOG_DIR = STATE_HOME / "logs"
SUBCOMMANDS = {"start", "stop", "list", "ls", "logs", "status", "share", "admin-link"}

# In a private --site, these resource types are served to anyone (a shared page
# can't render without its CSS/JS/fonts/images); everything else — content pages,
# the index, directory listings — needs a valid share token or the admin cookie.
SAFE_ASSET_EXTS = {
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot",
}
ADMIN_COOKIE = "nj_admin"

# Shown at "/" of a private site to anyone without the admin cookie (and to the
# NorthJetty readiness probe). Deliberately reveals nothing: no file list, no links.
PRIVATE_STUB_HTML = (
    "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>私有站点</title><style>html,body{height:100%;margin:0}"
    "body{display:grid;place-items:center;background:#0d0f14;color:#9aa2b1;"
    "font:15px/1.6 ui-sans-serif,system-ui,\"Noto Sans SC\",sans-serif}"
    "div{text-align:center;padding:24px}b{color:#e7e9ef;font-weight:600}</style></head>"
    "<body><div><p><b>私有站点</b></p><p>此站点内容仅通过分享链接访问。</p></div></body></html>"
)

# Injected before </body> only when the request is admin-authorized: a floating
# "分享本页" button that mints a per-page share link via /__nj/share.
ADMIN_SHARE_WIDGET = """
<div id="nj-admin-share" style="position:fixed;right:16px;bottom:16px;z-index:2147483000;font:13px/1.4 ui-sans-serif,system-ui,'Noto Sans SC',sans-serif;">
  <button type="button" onclick="njAdminShare(this)" style="display:inline-flex;align-items:center;gap:6px;padding:8px 12px;border-radius:8px;border:1px solid #3157d5;background:#3157d5;color:#fff;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.28);">🔗 分享本页</button>
  <div id="nj-admin-share-out" style="display:none;margin-top:8px;max-width:340px;padding:9px 11px;border-radius:8px;background:#111827;color:#e5e7eb;word-break:break-all;box-shadow:0 2px 10px rgba(0,0,0,.32);"></div>
</div>
<script>
function njAdminShare(btn){
  var out=document.getElementById('nj-admin-share-out');
  btn.disabled=true;var old=btn.textContent;btn.textContent='生成中…';
  fetch('/__nj/share?path='+encodeURIComponent(location.pathname)+'&days=7',{credentials:'same-origin'})
   .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
   .then(function(d){
     var url=location.origin+d.share_path;
     out.style.display='block';
     var exp=d.expires_at?(' · 有效期至 '+new Date(d.expires_at*1000).toLocaleDateString()):' · 永久有效';
     out.textContent=url+exp;
     if(navigator.clipboard){navigator.clipboard.writeText(url).then(function(){btn.textContent='🔗 已复制';},function(){btn.textContent=old;});}
     else{btn.textContent=old;}
   })
   .catch(function(e){out.style.display='block';out.textContent='生成失败: '+e.message;btn.textContent=old;})
   .finally(function(){btn.disabled=false;});
}
</script>
"""


class ShutdownRequested(Exception):
    pass


def log(message: str, *, stream: Any = sys.stdout) -> None:
    try:
        print(message, file=stream, flush=True)
    except BrokenPipeError:
        pass


def env_flag(name: str, *, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def state_path(alias: str) -> Path:
    return STATE_DIR / f"{alias}.json"


def log_path(alias: str) -> Path:
    return LOG_DIR / f"{alias}.log"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_state(alias: str, data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = state_path(alias)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(target)


def update_state(alias: str, **changes: Any) -> dict[str, Any]:
    current: dict[str, Any] = {}
    target = state_path(alias)
    if target.exists():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            current = {}
    current.update(changes)
    write_state(alias, current)
    return current


def read_state(target: Path) -> dict[str, Any] | None:
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def all_states() -> list[dict[str, Any]]:
    if not STATE_DIR.exists():
        return []
    states = []
    for target in sorted(STATE_DIR.glob("*.json")):
        data = read_state(target)
        if data:
            states.append(data)
    return states


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------


def add_start_options(parser: argparse.ArgumentParser) -> None:
    require_auth_default = env_flag("NORTHJETTY_REQUIRE_AUTH", default=False)
    parser.add_argument("path", help="Local file or directory to publish.")
    parser.add_argument(
        "--alias",
        help="Route alias (1-32 chars: a-z, 0-9, -). Default: name + random suffix.",
    )
    parser.add_argument("--port", type=int, default=0, help="Local port. Default: 0 (auto).")
    parser.add_argument("--bind", default="0.0.0.0", help="Local bind address. Default: 0.0.0.0.")
    parser.add_argument(
        "--target-host",
        help="Host/IP NorthJetty uses to reach this pod. Default: auto-detected.",
    )
    parser.add_argument(
        "--no-listing",
        action="store_true",
        help="Disable directory listing when publishing a directory.",
    )
    parser.add_argument(
        "--northjetty-origin",
        default=os.environ.get("NORTHJETTY_ORIGIN", DEFAULT_NORTHJETTY_ORIGIN),
        help=f"NorthJetty origin. Default: {DEFAULT_NORTHJETTY_ORIGIN}.",
    )
    parser.add_argument("--api-key-env", default=DEFAULT_KEY_ENV, help="Env var with the API key.")
    parser.add_argument("--wait-timeout-secs", type=int, default=120, help="Readiness wait. Default: 120.")
    parser.add_argument(
        "--site",
        action="store_true",
        help="Serve a directory as a dynamic doc site: the index is built from a "
        "live filesystem listing on every request and markdown is rendered "
        "on the fly. No static index.html needed; edits show up on refresh.",
    )
    parser.add_argument("--site-title", default="文档", help="Title for the --site index page.")
    parser.add_argument("--site-tagline", default="", help="Subtitle for the --site index page.")
    parser.add_argument(
        "--private",
        action="store_true",
        help="Private --site: the index and every content page require the admin "
        "cookie or a per-page share token (`nj-publish share <page>`). Static "
        "assets stay open so shared pages can render.",
    )
    parser.add_argument(
        "--require-auth",
        action="store_true",
        default=require_auth_default,
        help="Require clients to present the route access token.",
    )
    parser.add_argument(
        "--no-require-auth", action="store_false", dest="require_auth", help="Disable route auth."
    )
    parser.add_argument("--auth-header", help="Use a custom auth header instead of Authorization Bearer.")
    parser.add_argument("--auth-mode", help=f"Override auth_mode. Default: {DEFAULT_AUTH_MODE}.")
    parser.add_argument(
        "--allowed-cidr",
        dest="allowed_cidrs",
        action="append",
        default=[],
        help="Public source IP/CIDR allowed (repeatable, comma-separated ok).",
    )
    parser.add_argument("--rate-limit", type=int, help="rate_limit_per_minute (optional).")
    parser.add_argument("--concurrency", type=int, help="concurrency_limit (optional).")
    parser.add_argument("--share-days", type=int, help="Also create a share link valid N days.")
    parser.add_argument("--local-only", action="store_true", help="Only serve locally; no route.")
    parser.add_argument("--verbose", action="store_true", help="Print local server request logs.")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Stay attached in the foreground instead of daemonizing.",
    )
    parser.add_argument(
        "--skip-site-lint",
        action="store_true",
        help="Skip a repo-local site_tools/lint_html.py validation gate.",
    )


def validate_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"Path does not exist: {path}")
    if not (path.is_file() or path.is_dir()):
        raise SystemExit(f"Path is neither a file nor a directory: {path}")
    return path


def default_alias_for(path: Path) -> str:
    name = path.name if path.is_dir() else path.stem
    base = re.sub(r"[^a-z0-9-]+", "-", name.lower())
    base = re.sub(r"-+", "-", base).strip("-") or "file"
    suffix = secrets.token_hex(3)
    max_base_len = 32 - len(suffix) - 1
    base = base[:max_base_len].strip("-") or "file"
    return f"{base}-{suffix}"


def validate_alias(alias: str) -> str:
    if not ALIAS_RE.fullmatch(alias):
        raise SystemExit(
            "Invalid alias. Use 1-32 lowercase letters, digits, or hyphens; "
            "it cannot start or end with a hyphen."
        )
    return alias


def parse_allowed_cidrs(cli_values: list[str]) -> list[str]:
    values: list[str] = []
    env_value = os.environ.get(DEFAULT_ALLOWED_CIDRS_ENV, "")
    if env_value:
        values.append(env_value)
    values.extend(cli_values)

    cidrs: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in re.split(r"[\s,]+", value.strip()):
            if not part:
                continue
            normalized = normalize_cidr(part)
            if normalized not in seen:
                seen.add(normalized)
                cidrs.append(normalized)
    return cidrs


def normalize_cidr(value: str) -> str:
    try:
        if "/" in value:
            return str(ipaddress.ip_network(value, strict=False))
        ip = ipaddress.ip_address(value)
        prefix = 32 if ip.version == 4 else 128
        return f"{ip}/{prefix}"
    except ValueError as exc:
        raise SystemExit(f"Invalid allowed CIDR/IP: {value}") from exc


def detect_target_host() -> str:
    for probe in ("1.1.1.1", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 80))
            host = sock.getsockname()[0]
            if host and not host.startswith("127."):
                return host
        except OSError:
            pass
        finally:
            sock.close()
    try:
        for host in socket.gethostbyname_ex(socket.gethostname())[2]:
            if host and not host.startswith("127."):
                return host
    except OSError:
        pass
    raise SystemExit(
        "Could not auto-detect this pod's reachable IP. Pass --target-host, "
        "for example: --target-host 10.233.75.62"
    )


def get_api_key(env_name: str) -> str:
    api_key = os.environ.get(env_name)
    if api_key:
        return api_key
    if sys.stdin.isatty():
        api_key = getpass.getpass(f"{env_name}: ").strip()
        if api_key:
            return api_key
    raise SystemExit(f"Set {env_name} to your NorthJetty API key before running.")


# ---------------------------------------------------------------------------
# Local static server
# ---------------------------------------------------------------------------


class StaticHandler(SimpleHTTPRequestHandler):
    verbose = False
    allow_listing = False

    def list_directory(self, path: str):  # type: ignore[override]
        if self.allow_listing:
            return super().list_directory(path)
        self.send_error(404, "Directory listing disabled")
        return None

    def log_message(self, format: str, *args: Any) -> None:
        if self.verbose:
            super().log_message(format, *args)


class SiteHandler(StaticHandler):
    """Dynamic doc-site handler: the index is rebuilt from a live filesystem
    listing per request, and markdown files are rendered on the fly."""

    site_root = "."
    site_title = "文档"
    site_tagline = ""
    private = False

    # Per-request auth flags (reset at the top of every do_GET/do_HEAD).
    _admin = False
    _standalone = False
    _share_token = ""

    def guess_type(self, path: str) -> str:
        """Declare UTF-8 for raw/self-contained HTML served from the site."""
        content_type = super().guess_type(path)
        if content_type.lower() == "text/html":
            return "text/html; charset=utf-8"
        return content_type

    def _inject_admin_widget(self, page: str) -> str:
        idx = page.lower().rfind("</body>")
        if idx == -1:
            return page + ADMIN_SHARE_WIDGET
        return page[:idx] + ADMIN_SHARE_WIDGET + page[idx:]

    def _send_html(self, page: str) -> None:
        if self._admin and not self._is_index():
            page = self._inject_admin_widget(page)
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # always fresh
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _is_index(self) -> bool:
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "", "/index.html"):
            return True
        return Path(self.translate_path(self.path)).is_dir()

    def _is_internal_path(self) -> bool:
        request_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        parts = Path(request_path).parts
        return "site_tools" in parts or Path(request_path).name == "AGENTS.md"

    # ── 私有站访问控制 ────────────────────────────────────────────
    def _decoded_path(self) -> str:
        return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

    def _query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _cookies(self) -> dict[str, str]:
        import http.cookies

        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", "") or "")
        except http.cookies.CookieError:
            return {}
        return {key: morsel.value for key, morsel in jar.items()}

    def _asset_ok(self, path: str) -> bool:
        return Path(path).suffix.lower() in SAFE_ASSET_EXTS

    def _authorize(self) -> str | None:
        """Classify a private-site request.

        Returns 'admin' | 'share' | 'asset', 'handled' (a redirect was already
        written), or None (deny). Sets self._admin / self._standalone /
        self._share_token as a side effect."""
        if self._is_internal_path():
            return None
        path = self._decoded_path()
        query = self._query()

        admin_q = (query.get("admin") or [""])[0]
        if admin_q and njsite.verify_admin_token(admin_q):
            self._set_admin_cookie_and_redirect(admin_q)
            return "handled"

        # A valid per-page share token wins over the admin cookie: opening a
        # share link (even in the owner's own logged-in browser) must always
        # preview exactly what the recipient sees — the isolated, chrome-less
        # page — not the full admin view.
        token = (query.get("k") or [""])[0]
        if token and njsite.verify_share_token(path, token):
            self._standalone = True
            self._share_token = token
            return "share"

        if njsite.verify_admin_token(self._cookies().get(ADMIN_COOKIE, "")):
            self._admin = True
            return "admin"

        if self._asset_ok(path):
            return "asset"
        return None

    def _set_admin_cookie_and_redirect(self, token: str) -> None:
        split = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query)
        query.pop("admin", None)
        clean = urllib.parse.urlunsplit(
            ("", "", split.path, urllib.parse.urlencode(query, doseq=True), split.fragment)
        ) or "/"
        self.send_response(302)
        self.send_header("Location", clean)
        # 30 days, site-wide, not readable from page scripts.
        self.send_header(
            "Set-Cookie",
            f"{ADMIN_COOKIE}={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_share_api(self) -> None:
        if not self._admin:
            self.send_error(404)
            return
        query = self._query()
        page = (query.get("path") or [""])[0]
        try:
            days = int((query.get("days") or ["7"])[0])
        except ValueError:
            days = 7
        page = urllib.parse.unquote(page)
        if not page.startswith("/"):
            page = "/" + page
        expires_at = 0 if days <= 0 else int(time.time()) + days * 86400
        token = njsite.mint_share_token(page, expires_at)
        share_path = urllib.parse.quote(page) + "?k=" + token
        payload = json.dumps(
            {"share_path": share_path, "expires_at": expires_at}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _reset_auth(self) -> None:
        self._admin = False
        self._standalone = False
        self._share_token = ""

    def _private_gate(self) -> bool:
        """Run the private-site gate. Returns True if the request may proceed to
        normal dispatch, False if the response is already complete."""
        decision = self._authorize()
        if decision == "handled":
            return False
        if decision is None:
            if self._is_index():
                # Readiness probes and stray visitors get an empty stub, not the index.
                self._send_html(PRIVATE_STUB_HTML)
            else:
                self.send_error(404)
            return False
        return True

    def do_HEAD(self) -> None:  # noqa: N802
        self._reset_auth()
        if self.private:
            decision = self._authorize()
            if decision == "handled":
                return
            if decision is None:
                if self._is_index():
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                self.send_error(404)
                return
        if self._is_internal_path():
            self.send_error(404)
            return
        if self._is_index():
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        super().do_HEAD()

    def do_GET(self) -> None:  # noqa: N802
        self._reset_auth()

        if self.private:
            if not self._private_gate():
                return
            if urllib.parse.urlsplit(self.path).path == "/__nj/share":
                self._handle_share_api()
                return

        if self._is_internal_path():
            self.send_error(404)
            return

        root = Path(self.site_root)
        if self._is_index():
            try:
                files = njsite.list_files(root, exclude=frozenset({"index.html"}))
                page = njsite.build_index_html(self.site_title, self.site_tagline, files)
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, f"index render error: {exc}")
                return
            self._send_html(page)
            return

        split = urllib.parse.urlsplit(self.path)
        wants_raw = "raw" in urllib.parse.parse_qs(split.query)
        fs_path = Path(self.translate_path(self.path))
        ext = fs_path.suffix.lower()
        if not wants_raw and fs_path.is_file() and ext in njsite.MD_EXTS:
            try:
                text = fs_path.read_text(encoding="utf-8")
                body = njsite.render_markdown(njsite.strip_leading_title(text))
                title = njsite.markdown_title(fs_path)
                date = time.strftime("%b %d, %Y", time.localtime(fs_path.stat().st_mtime))
                rel = os.path.relpath(fs_path, str(root)).replace(os.sep, "/")
                page = njsite.build_md_page(
                    title, rel, date, body, base_href="", back_href="/",
                    standalone=self._standalone,
                )
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, f"markdown render error: {exc}")
                return
            self._send_html(page)
            return

        # HTML uses an explicit doc-shell-v1 contract. `artifact-v1` keeps the
        # authored page intact and injects CSS-isolated shared site chrome;
        # `isolated` puts complex/scripted HTML behind that chrome in an iframe.
        # Legacy styled pages get the artifact treatment for compatibility.
        # The untouched source is always available with ?raw=1.
        #
        # A shared page (self._standalone) is served bare — the raw authored
        # document with no injected chrome — the same clean view as ?raw=1, which
        # is exactly what a share recipient should get. Falls through to the
        # static file handler below.
        if not wants_raw and not self._standalone and fs_path.is_file() and ext in njsite.HTML_EXTS:
            try:
                text = fs_path.read_text(encoding="utf-8")
                metadata = njsite.html_metadata(text)
                layout = njsite.html_layout(text)
                if (root / "site_tools" / "DOC_SHELL_V1.md").is_file():
                    required = ("nj-category", "nj-updated", "description")
                    missing = [name for name in required if not metadata.get(name)]
                    if not layout:
                        missing.insert(0, "valid nj-layout")
                    if missing:
                        self.send_error(
                            422,
                            "doc-shell-v1 contract missing: " + ", ".join(dict.fromkeys(missing)),
                        )
                        return
                title = njsite.extract_title(fs_path)
                rel = os.path.relpath(fs_path, str(root)).replace(os.sep, "/")
                raw_url = split.path + "?raw=1"
                # A shared page loads its own raw source (iframe / 查看原稿); carry
                # the token so those sub-requests clear the private gate too.
                if self._standalone and self._share_token:
                    raw_url += "&k=" + self._share_token
                updated = metadata.get("nj-updated") or time.strftime(
                    "%Y-%m-%d", time.localtime(fs_path.stat().st_mtime)
                )
                category = metadata.get("nj-category") or metadata.get("category") or njsite.categorize(
                    rel, True, text[:16384]
                )
                if layout == "isolated":
                    page = njsite.build_isolated_page(
                        title=title,
                        category=category,
                        updated=updated,
                        source=raw_url,
                        raw_url=raw_url,
                        site_title=self.site_title,
                        standalone=self._standalone,
                    )
                    self._send_html(page)
                    return
                if layout == "artifact-v1" or njsite.is_self_contained_html(text):
                    page = njsite.inject_site_chrome(
                        text,
                        title=title,
                        category=category,
                        updated=updated,
                        source=raw_url,
                        site_title=self.site_title,
                        standalone=self._standalone,
                    )
                    self._send_html(page)
                    return
                body = njsite.html_to_reader_body(text)
                date = time.strftime("%b %d, %Y", time.localtime(fs_path.stat().st_mtime))
                page = njsite.build_md_page(
                    title, rel, date, body, base_href="", back_href="/",
                    standalone=self._standalone,
                )
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, f"html render error: {exc}")
                return
            self._send_html(page)
            return

        super().do_GET()


class StaticServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass
class LocalServer:
    serve_root: Path
    url_path: str
    allow_listing: bool
    bind: str
    requested_port: int
    verbose: bool
    site: bool = False
    site_title: str = "文档"
    site_tagline: str = ""
    private: bool = False
    httpd: StaticServer | None = None
    thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self.httpd is None:
            raise RuntimeError("Local server has not been started.")
        return int(self.httpd.server_address[1])

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.url_path}"

    def target_url(self, target_host: str) -> str:
        return f"http://{target_host}:{self.port}"

    def start(self) -> None:
        root = str(self.serve_root)
        outer = self
        base = SiteHandler if self.site else StaticHandler

        class Handler(base):  # type: ignore[valid-type, misc]
            verbose = outer.verbose
            allow_listing = outer.allow_listing
            site_root = root
            site_title = outer.site_title
            site_tagline = outer.site_tagline
            private = outer.private

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=root, **kwargs)

        self.httpd = StaticServer((self.bind, self.requested_port), Handler)
        # Daemon thread: never let a lingering serve loop (e.g. a keep-alive
        # connection) block process exit after cleanup, which previously made
        # `stop` hang until the SIGKILL fallback.
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="northjetty-static-server", daemon=True
        )
        self.thread.start()
        self.verify()

    def verify(self) -> None:
        request = urllib.request.Request(self.local_url, method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"Local server returned HTTP {response.status}")

    def stop(self) -> None:
        if self.httpd is None:
            return
        httpd = self.httpd
        self.httpd = None
        httpd.shutdown()
        httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


# ---------------------------------------------------------------------------
# NorthJetty client
# ---------------------------------------------------------------------------


class NorthJettyClient:
    def __init__(self, origin: str, api_key: str) -> None:
        self.origin = origin.rstrip("/")
        self.api_key = api_key

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: int = 30
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json", "X-NorthJetty-API-Key": self.api_key}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            message = body
            try:
                parsed = json.loads(body)
                message = parsed.get("error") or parsed.get("message") or body
            except json.JSONDecodeError:
                pass
            raise RuntimeError(
                f"NorthJetty {method} {path} failed with HTTP {error.code}: {message}"
            ) from error

    def create_route(self, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        return self.request("POST", "/api/v1/tunnel/routes", payload, timeout=timeout)

    def delete_route(self, route_id: str) -> None:
        quoted = urllib.parse.quote(route_id, safe="")
        self.request("DELETE", f"/api/v1/tunnel/routes/{quoted}", timeout=15)

    def list_routes(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/api/v1/tunnel/routes", timeout=15)
        routes = response.get("routes", [])
        if not isinstance(routes, list):
            return []
        return [route for route in routes if isinstance(route, dict)]

    def create_share_link(
        self, route_id: str, *, name: str, expires_in_secs: int, next_path: str
    ) -> dict[str, Any]:
        quoted = urllib.parse.quote(route_id, safe="")
        payload = {"name": name, "expires_in_secs": expires_in_secs, "next": next_path}
        return self.request(
            "POST", f"/api/v1/tunnel/routes/{quoted}/share-links", payload, timeout=30
        )


# ---------------------------------------------------------------------------
# Publisher (foreground serving loop)
# ---------------------------------------------------------------------------


class Publisher:
    def __init__(self) -> None:
        self.local_server: LocalServer | None = None
        self.client: NorthJettyClient | None = None
        self.route_id: str | None = None
        self.route_alias: str | None = None
        self.state_alias: str | None = None
        self.can_delete_by_alias = False
        self.exit_code = 0
        self.stop_signal: int | None = None
        self._cleanup_started = False
        self._lock = threading.Lock()

    @property
    def cleanup_started(self) -> bool:
        return self._cleanup_started

    def cleanup(self) -> None:
        with self._lock:
            if self._cleanup_started:
                return
            self._cleanup_started = True

        if self.local_server is not None:
            log("Stopping local server...")
            try:
                self.local_server.stop()
            except Exception as exc:  # noqa: BLE001
                log(f"Warning: failed to stop local server: {exc}", stream=sys.stderr)

        if self.client is not None and self.route_id is not None:
            log("Deleting NorthJetty route...")
            try:
                self.client.delete_route(self.route_id)
                log("NorthJetty route deleted.")
            except Exception as exc:  # noqa: BLE001
                log(
                    "Warning: failed to delete NorthJetty route. "
                    f"Manual route id: {self.route_id}. Error: {exc}",
                    stream=sys.stderr,
                )
        elif (
            self.client is not None
            and self.route_alias is not None
            and self.can_delete_by_alias
        ):
            log("Checking for an interrupted NorthJetty route creation...")
            try:
                prefix = f"{self.route_alias}-"
                for route in self.client.list_routes():
                    route_id = str(route.get("id") or "")
                    route_key = str(route.get("route_key") or "")
                    public_host = str(route.get("public_host") or "")
                    alias = str(route.get("alias") or "")
                    if not route_id:
                        continue
                    if alias == self.route_alias or route_key.startswith(prefix) or public_host.startswith(prefix):
                        self.client.delete_route(route_id)
                        log("Interrupted NorthJetty route deleted.")
                        break
            except Exception as exc:  # noqa: BLE001
                log(
                    f"Warning: could not check for an interrupted route. "
                    f"Alias: {self.route_alias}. Error: {exc}",
                    stream=sys.stderr,
                )

        if self.state_alias is not None:
            try:
                state_path(self.state_alias).unlink(missing_ok=True)
            except OSError:
                pass


def public_url(public_host: str, path: str) -> str:
    if public_host.startswith(("http://", "https://")):
        return public_host.rstrip("/") + path
    return f"https://{public_host}{path}"


def shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def print_auth_instructions(url: str, auth_mode: str, auth_header: str, route: dict[str, Any]) -> None:
    token = route.get("route_auth_token")
    if not token:
        log("Route auth is enabled, but NorthJetty did not return route_auth_token.")
        log("If this route already existed, rotate its token in the console/API.")
        return
    token = str(token)
    is_bearer = auth_mode == "authorization_bearer"
    header_name = "Authorization" if is_bearer else (route.get("auth_header") or auth_header)
    header_value = f"Bearer {token}" if is_bearer else token
    log("Route access token (shown only once — save it):")
    log(token)
    log("")
    log("Programmatic access header:")
    log(f"{header_name}: {header_value}")
    log("")
    log("CLI fetch:")
    log(f"curl -fsSL -H {shell_single_quote(f'{header_name}: {header_value}')} {shell_single_quote(url)}")
    log("")
    log("Browser: open the Public URL; on the access-code page paste the token above.")
    log("")


def print_access_policy(require_auth: bool, allowed_cidrs: list[str]) -> None:
    if allowed_cidrs:
        log("Source IP whitelist:")
        for cidr in allowed_cidrs:
            log(f"- {cidr}")
        if not require_auth:
            log("The URL is directly reachable only from these source IP ranges.")
        log("")
    elif not require_auth:
        log("Access policy: public URL has no token or source-IP restriction.")
        log("")


def log_stop_reason(publisher: "Publisher") -> None:
    if publisher.stop_signal:
        try:
            name = signal.Signals(publisher.stop_signal).name
        except ValueError:
            name = str(publisher.stop_signal)
        log(f"Received {name}; cleaning up...")


def run_publish(args: argparse.Namespace, *, path: Path, alias: str, alias_was_generated: bool) -> int:
    is_dir = path.is_dir()
    site = bool(getattr(args, "site", False)) and is_dir
    if is_dir:
        serve_root, url_path, allow_listing = path, "/", not args.no_listing
    else:
        serve_root = path.parent
        url_path = "/" + urllib.parse.quote(path.name)
        allow_listing = False

    allowed_cidrs = parse_allowed_cidrs(args.allowed_cidrs or [])
    target_host = args.target_host or detect_target_host()
    api_key = None if args.local_only else get_api_key(args.api_key_env)

    if args.auth_header:
        auth_mode = args.auth_mode or "custom_header"
        auth_header = args.auth_header
    else:
        auth_mode = args.auth_mode or DEFAULT_AUTH_MODE
        auth_header = DEFAULT_CUSTOM_AUTH_HEADER

    publisher = Publisher()
    publisher.state_alias = alias
    stop_event = threading.Event()

    write_state(
        alias,
        {
            "alias": alias,
            "pid": os.getpid(),
            "path": str(path),
            "status": "starting",
            "require_auth": bool(args.require_auth),
            "private": bool(getattr(args, "private", False)) and site,
            "local_only": bool(args.local_only),
            "log": str(log_path(alias)),
            "started_at": time.time(),
        },
    )

    # Signal handlers must stay minimal: only flip a flag and wake the main
    # thread. Doing I/O (print) or raising from a handler can deadlock against a
    # lock the main thread already holds, which previously made `stop` hang
    # until the SIGKILL fallback. The main loop logs and cleans up.
    def handle_signal(signum: int, _frame: Any) -> None:
        publisher.exit_code = 128 + signum
        publisher.stop_signal = signum
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, handle_signal)
    atexit.register(publisher.cleanup)

    try:
        if site and not args.skip_site_lint:
            linter = path / "site_tools" / "lint_html.py"
            if linter.is_file():
                lint = subprocess.run(
                    [sys.executable, str(linter), "--strict", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if lint.returncode != 0:
                    details = (lint.stdout + lint.stderr).strip()
                    raise RuntimeError("doc-shell-v1 validation failed:\n" + details)
                log("doc-shell-v1 validation: passed")

        local_server = LocalServer(
            serve_root=serve_root,
            url_path=url_path,
            allow_listing=allow_listing,
            bind=args.bind,
            requested_port=args.port,
            verbose=args.verbose,
            site=site,
            site_title=getattr(args, "site_title", "文档"),
            site_tagline=getattr(args, "site_tagline", ""),
            private=bool(getattr(args, "private", False)) and site,
        )
        local_server.start()
        publisher.local_server = local_server

        log(f"Serving: {path}" + ("/  (dynamic site)" if site else ("/  (directory)" if is_dir else "")))
        log(f"Local URL: {local_server.local_url}")
        log(f"NorthJetty target URL: {local_server.target_url(target_host)}")
        update_state(
            alias,
            port=local_server.port,
            target_url=local_server.target_url(target_host),
            local_url=local_server.local_url,
        )

        if args.local_only:
            update_state(alias, status="running")
            log("Local-only mode. Use `stop` or Ctrl+C to stop the server.")
            stop_event.wait()
            log_stop_reason(publisher)
            return publisher.exit_code

        if api_key is None:
            raise RuntimeError("NorthJetty API key is unavailable.")
        client = NorthJettyClient(args.northjetty_origin, api_key)
        publisher.client = client
        publisher.route_alias = alias
        publisher.can_delete_by_alias = alias_was_generated

        payload: dict[str, Any] = {
            "alias": alias,
            "target_url": local_server.target_url(target_host),
            "enabled": True,
            "require_auth": args.require_auth,
            "wait_ready": env_flag("NORTHJETTY_WAIT_READY", default=True),
            "probe_path": url_path,
            "wait_timeout_secs": args.wait_timeout_secs,
        }
        if allowed_cidrs:
            payload["allowed_cidrs"] = allowed_cidrs
        if args.rate_limit is not None:
            payload["rate_limit_per_minute"] = args.rate_limit
        if args.concurrency is not None:
            payload["concurrency_limit"] = args.concurrency
        if args.require_auth:
            payload["auth_mode"] = auth_mode
            if auth_mode != "authorization_bearer":
                payload["auth_header"] = auth_header

        log(f"Creating NorthJetty route alias: {alias}")
        route = client.create_route(payload, timeout=args.wait_timeout_secs + 30)

        publisher.route_id = str(route.get("id") or "")
        if not publisher.route_id:
            raise RuntimeError(f"NorthJetty response did not include a route id: {route}")
        host = route.get("public_host")
        if not host:
            raise RuntimeError(f"NorthJetty response did not include public_host: {route}")

        url = public_url(str(host), url_path)
        update_state(
            alias,
            status="running",
            route_id=publisher.route_id,
            public_host=str(host),
            public_url=url,
            auth_mode=auth_mode if args.require_auth else None,
        )
        log("")
        log("Public URL:")
        log(url)
        log("")
        print_access_policy(args.require_auth, allowed_cidrs)
        if args.require_auth:
            print_auth_instructions(url, auth_mode, auth_header, route)

        if args.share_days is not None:
            try:
                share = client.create_share_link(
                    publisher.route_id,
                    name=f"{alias}-share",
                    expires_in_secs=args.share_days * 86400,
                    next_path=url_path,
                )
                share_url = share.get("share_url")
                if share_url:
                    log(f"Share link (valid {args.share_days}d, no account needed):")
                    log(str(share_url))
                    if share.get("expires_at"):
                        log(f"Expires at: {share['expires_at']}")
                    log("")
            except Exception as exc:  # noqa: BLE001
                log(f"Warning: failed to create share link: {exc}", stream=sys.stderr)

        log("Publication is live. Manage it with:")
        log(f"  nj-publish.py list")
        log(f"  nj-publish.py stop {alias}")
        stop_event.wait()
        log_stop_reason(publisher)
    except ShutdownRequested:
        pass
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        publisher.exit_code = 1
        log(f"Error: {exc}", stream=sys.stderr)
        try:
            update_state(alias, status="error", error=str(exc))
        except OSError:
            pass
    finally:
        publisher.cleanup()

    return publisher.exit_code


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_start(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="nj-publish.py start", description="Publish a path.")
    add_start_options(parser)
    args = parser.parse_args(rest)

    path = validate_path(args.path)
    alias_was_generated = args.alias is None
    alias = validate_alias(args.alias or default_alias_for(path))

    # Reject duplicate alias with a live daemon.
    existing = read_state(state_path(alias))
    if existing and pid_alive(int(existing.get("pid", 0))):
        raise SystemExit(
            f"Alias '{alias}' already has a running publication (pid {existing['pid']}). "
            f"Stop it first: nj-publish.py stop {alias}"
        )

    if args.foreground:
        return run_publish(args, path=path, alias=alias, alias_was_generated=alias_was_generated)

    # Daemon mode: resolve the API key in the parent (may prompt), then spawn a
    # detached --foreground child whose stdout/stderr go to the log file.
    child_env = dict(os.environ)
    if not args.local_only:
        child_env[args.api_key_env] = get_api_key(args.api_key_env)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logfile = log_path(alias)

    # `rest` already contains the positional path; only append the overrides.
    child_argv = (
        [sys.executable, os.path.abspath(__file__), "start"]
        + rest
        + ["--foreground", "--alias", alias]
    )

    with open(logfile, "ab", buffering=0) as log_fh:
        proc = subprocess.Popen(
            child_argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            env=child_env,
            close_fds=True,
        )

    # Poll the state file until the child reports running / error / dies.
    deadline = time.time() + args.wait_timeout_secs + 45
    log(f"Starting in background (pid {proc.pid}); waiting for the route to come up...")
    while time.time() < deadline:
        state = read_state(state_path(alias))
        if state:
            status = state.get("status")
            if status == "running":
                log("")
                if args.local_only:
                    log(f"Local-only server running. Local URL: {state.get('local_url')}")
                else:
                    log("Public URL:")
                    log(str(state.get("public_url")))
                log("")
                if state.get("require_auth"):
                    log(f"Route auth is ON. Get the access token from the log: nj-publish.py logs {alias}")
                log(f"Manage: nj-publish.py list | nj-publish.py stop {alias}")
                log(f"Logs:   {logfile}")
                return 0
            if status == "error":
                log(f"Failed to publish: {state.get('error')}", stream=sys.stderr)
                state_path(alias).unlink(missing_ok=True)
                return 1
        if proc.poll() is not None:
            log("Background process exited before becoming ready. Recent log:", stream=sys.stderr)
            _print_log_tail(logfile, 30)
            state_path(alias).unlink(missing_ok=True)
            return 1
        time.sleep(0.3)

    log("Timed out waiting for the publication to become ready. Recent log:", stream=sys.stderr)
    _print_log_tail(logfile, 30)
    return 1


def _print_log_tail(logfile: Path, lines: int) -> None:
    try:
        content = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in content[-lines:]:
        log(f"  {line}", stream=sys.stderr)


def cmd_stop(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="nj-publish.py stop")
    parser.add_argument("targets", nargs="+", help="Alias(es) to stop, or 'all'.")
    parser.add_argument("--timeout", type=int, default=20, help="Seconds to wait per stop.")
    args = parser.parse_args(rest)

    if args.targets == ["all"]:
        aliases = [s.get("alias") for s in all_states() if s.get("alias")]
        if not aliases:
            log("No running publications.")
            return 0
    else:
        aliases = args.targets

    exit_code = 0
    for alias in aliases:
        state = read_state(state_path(alias))
        if not state:
            log(f"{alias}: no such publication.", stream=sys.stderr)
            exit_code = 1
            continue
        pid = int(state.get("pid", 0))
        if not pid_alive(pid):
            log(f"{alias}: process not running; cleaning up state file.")
            state_path(alias).unlink(missing_ok=True)
            continue
        log(f"{alias}: stopping pid {pid} (deleting route)...")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            log(f"{alias}: failed to signal pid {pid}: {exc}", stream=sys.stderr)
            exit_code = 1
            continue
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not state_path(alias).exists() or not pid_alive(pid):
                break
            time.sleep(0.2)
        if pid_alive(pid):
            log(f"{alias}: still alive after {args.timeout}s; sending SIGKILL.", stream=sys.stderr)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            state_path(alias).unlink(missing_ok=True)
            log(f"{alias}: killed (route may need manual cleanup).", stream=sys.stderr)
            exit_code = 1
        else:
            log(f"{alias}: stopped.")
    return exit_code


def cmd_list(_rest: list[str]) -> int:
    states = all_states()
    if not states:
        log("No publications.")
        return 0
    rows = [("ALIAS", "PID", "STATUS", "PUBLIC URL / PATH")]
    cleaned = []
    for state in states:
        alias = str(state.get("alias", "?"))
        pid = int(state.get("pid", 0))
        alive = pid_alive(pid)
        status = str(state.get("status", "?"))
        if not alive and status != "error":
            status = "dead"
        target = state.get("public_url") or state.get("local_url") or state.get("path") or ""
        rows.append((alias, str(pid), status, str(target)))
        cleaned.append((alias, alive))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        log("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    dead = [a for a, alive in cleaned if not alive]
    if dead:
        log("")
        log(f"Note: {len(dead)} dead entr{'y' if len(dead) == 1 else 'ies'}; "
            f"clean up with: nj-publish.py stop {' '.join(dead)}")
    return 0


def cmd_logs(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="nj-publish.py logs")
    parser.add_argument("alias", help="Publication alias.")
    parser.add_argument("-n", "--lines", type=int, default=40, help="Lines to show. Default: 40.")
    parser.add_argument("-f", "--follow", action="store_true", help="Follow the log (tail -f).")
    args = parser.parse_args(rest)

    state = read_state(state_path(args.alias))
    logfile = Path(state["log"]) if state and state.get("log") else log_path(args.alias)
    if not logfile.exists():
        raise SystemExit(f"No log file for alias '{args.alias}' at {logfile}")
    log(f"# {logfile}")
    if args.follow:
        os.execvp("tail", ["tail", "-n", str(args.lines), "-f", str(logfile)])
    _print_log_tail(logfile, args.lines)
    return 0


def _site_state_or_die(alias: str) -> dict[str, Any]:
    state = read_state(state_path(alias))
    if not state:
        raise SystemExit(
            f"No running publication with alias '{alias}'. Start the site first "
            f"(nj-publish-html), or pass --base-url explicitly."
        )
    return state


def _resolve_share_target(page: str, site_root: str | None) -> str:
    """Turn a CLI page argument into the site URL path the server will match.

    Accepts a URL path ("/notes/x.html"), a bare relative path ("notes/x.html"),
    or a local file under the served directory (resolved to its site-relative URL)."""
    if site_root:
        try:
            root = Path(site_root).resolve()
            resolved = Path(page).expanduser().resolve()
            if resolved.is_file() and (resolved == root or root in resolved.parents):
                return "/" + resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            pass
    return page if page.startswith("/") else "/" + page


def cmd_share(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="nj-publish.py share",
        description="Mint a per-page share link for a private --site. The link "
        "grants access to that one page only — not the index or any other page.",
    )
    parser.add_argument(
        "page", help="URL path (/notes/x.html) or a local file under the site root."
    )
    parser.add_argument("--alias", default="html", help="Site publication alias. Default: html.")
    parser.add_argument(
        "--days", type=int, default=7, help="Validity in days; 0 = no expiry. Default: 7."
    )
    parser.add_argument("--base-url", help="Override the public base URL (else read from state).")
    args = parser.parse_args(rest)

    site_root: str | None = None
    base = args.base_url
    if not base:
        state = _site_state_or_die(args.alias)
        base = state.get("public_url") or state.get("local_url")
        site_root = state.get("path")
        if not state.get("private"):
            log(
                f"Note: alias '{args.alias}' is not running in --private mode; the "
                f"whole site is public, so this link does not actually restrict access.",
                stream=sys.stderr,
            )
    if not base:
        raise SystemExit("Could not determine the public URL; pass --base-url.")

    url_path = urllib.parse.unquote(_resolve_share_target(args.page, site_root))
    expires_at = 0 if args.days <= 0 else int(time.time()) + args.days * 86400
    token = njsite.mint_share_token(url_path, expires_at)
    share_url = base.rstrip("/") + urllib.parse.quote(url_path) + "?k=" + token
    log(share_url)
    if expires_at:
        log(f"# 有效期 {args.days} 天 · 至 {time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}")
    else:
        log("# 永久有效")
    return 0


def cmd_admin_link(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="nj-publish.py admin-link",
        description="Print the owner unlock link for a private --site (sets a 30-day "
        "admin cookie). Open it yourself; do not share it.",
    )
    parser.add_argument("--alias", default="html", help="Site publication alias. Default: html.")
    parser.add_argument("--base-url", help="Override the public base URL (else read from state).")
    args = parser.parse_args(rest)

    base = args.base_url
    if not base:
        base = _site_state_or_die(args.alias).get("public_url") or _site_state_or_die(
            args.alias
        ).get("local_url")
    if not base:
        raise SystemExit("Could not determine the public URL; pass --base-url.")
    log(base.rstrip("/") + "/?admin=" + njsite.admin_token())
    log("# 用你自己的浏览器打开一次即可解锁整站（写入 30 天 cookie）；请勿外发。")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in {"-h", "--help"}:
        log(__doc__ or "")
        return 0
    # Backward compat: a bare path means `start <path>`.
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["start"] + argv
    if not argv:
        log(__doc__ or "")
        return 2

    cmd, rest = argv[0], argv[1:]
    if cmd == "start":
        return cmd_start(rest)
    if cmd == "stop":
        return cmd_stop(rest)
    if cmd in ("list", "ls", "status"):
        return cmd_list(rest)
    if cmd == "logs":
        return cmd_logs(rest)
    if cmd == "share":
        return cmd_share(rest)
    if cmd == "admin-link":
        return cmd_admin_link(rest)
    log(f"Unknown command: {cmd}", stream=sys.stderr)
    log(__doc__ or "", stream=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
