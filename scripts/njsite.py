"""Shared rendering core for the doc site.

Used by:
  - gen-html-index.py  (static export: writes index.html + _md/*.html)
  - nj-publish.py --site (dynamic: renders the index and markdown per request)

Keeping it here means both the static and the live server share one design.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import time
from pathlib import Path


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
DESC_RE = re.compile(
    r"""<meta[^>]+name=["']description["'][^>]+content=["']([^"']*)["']""", re.IGNORECASE
)
H1_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", re.MULTILINE)
HTML_EXTS = {".html", ".htm"}
MD_EXTS = {".md", ".markdown"}
SKIP_EXTS = {
    ".tmp", ".swp", ".lock", ".py", ".pyc", ".js", ".css", ".map", ".json",
    ".tmpl", ".ds_store", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
}
SKIP_DIRS = {"assets", "site_tools"}
SKIP_NAMES = {"AGENTS.md"}
RENDER_DIR = "_md"
SITE_CHROME_VERSION = "3"


# ── 分享令牌 / 私有站访问控制 ──────────────────────────────────────
# 整站转私有后，每个页面用一枚 HMAC 签名的分享令牌单独授权。令牌绑定
# 「解码后的 URL 路径 + 过期时间」，签名用一把服务端密钥；改一个字符或
# 换一个页面路径都会验签失败。密钥优先取环境变量 NJSITE_SHARE_SECRET，
# 否则落到 ~/.northjetty/site-secret（0600，首次自动生成），保证「签发
# 端(share CLI)」和「验签端(站点服务)」用的是同一把钥匙。
_SECRET_FILE = Path(
    os.environ.get("NORTHJETTY_HOME", str(Path.home() / ".northjetty"))
) / "site-secret"


def share_secret() -> bytes:
    """Return the HMAC secret, generating and persisting one on first use."""
    env = os.environ.get("NJSITE_SHARE_SECRET")
    if env:
        return env.encode("utf-8")
    try:
        if _SECRET_FILE.exists():
            data = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if data:
                return data.encode("utf-8")
    except OSError:
        pass
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(secret, encoding="utf-8")
        os.chmod(_SECRET_FILE, 0o600)
    except OSError:
        pass
    return secret.encode("utf-8")


def _sign(message: str) -> str:
    digest = hmac.new(share_secret(), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def mint_share_token(path: str, expires_at: int) -> str:
    """Sign a token for one page. `path` is the decoded URL path (e.g.
    "/notes/foo.html"); `expires_at` is a unix timestamp, or 0 for no expiry."""
    return f"{int(expires_at)}.{_sign(f'share|{path}|{int(expires_at)}')}"


def verify_share_token(path: str, token: str) -> bool:
    if not token or "." not in token:
        return False
    exp_str, _, sig = token.partition(".")
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if exp != 0 and time.time() > exp:
        return False
    return hmac.compare_digest(sig, _sign(f"share|{path}|{exp}"))


def admin_token() -> str:
    """A single non-expiring token that unlocks the whole private site (owner)."""
    return _sign("admin|full-access")


def verify_admin_token(token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, admin_token())

META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
META_ATTR_RE = re.compile(
    r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)


def html_metadata(text: str) -> dict[str, str]:
    """Return lower-cased <meta name=... content=...> values."""
    metadata: dict[str, str] = {}
    for tag in META_TAG_RE.findall(text):
        attrs: dict[str, str] = {}
        for match in META_ATTR_RE.finditer(tag):
            value = next((part for part in match.groups()[1:] if part is not None), "")
            attrs[match.group(1).lower()] = html.unescape(value)
        name = attrs.get("name", "").strip().lower()
        if name:
            metadata[name] = attrs.get("content", "").strip()
    return metadata


def read_head(path: Path, size: int = 16384) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(size)
    except OSError:
        return ""


def extract_title(path: Path) -> str:
    match = TITLE_RE.search(read_head(path))
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        if title:
            return title
    return path.stem


def markdown_title(path: Path) -> str:
    match = H1_RE.search(read_head(path))
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip() or path.stem
    return path.stem


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def html_excerpt(path: Path, limit: int = 160) -> str:
    head = read_head(path)
    meta = DESC_RE.search(head)
    if meta and meta.group(1).strip():
        return _clip(html.unescape(meta.group(1)), limit)
    body = head
    cut = re.search(r"<body[^>]*>", body, re.IGNORECASE)
    if cut:
        body = body[cut.end():]
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return _clip(html.unescape(body), limit)


def markdown_excerpt(text: str, limit: int = 160) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    out, in_code = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or s.startswith("#") or s.startswith("![") or s.startswith("|"):
            continue
        if not s:
            if out:
                break
            continue
        out.append(s)
        if sum(len(x) for x in out) > limit:
            break
    para = " ".join(out)
    para = re.sub(r"`([^`]*)`", r"\1", para)
    para = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", para)
    para = re.sub(r"[*_>~]", "", para)
    return _clip(para, limit)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}GB"


def strip_leading_title(text: str) -> str:
    """Drop leading YAML front matter and the first H1 (shown as the page title)
    so the rendered body doesn't repeat the heading."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if re.match(r"^\s{0,3}#\s+\S", line):
            del lines[i]
        break
    return "\n".join(lines)


def github_slug(text: str, seen: dict) -> str:
    """GitHub-style heading slug: lowercase, strip punctuation (keep CJK/word/
    space/hyphen), spaces->hyphens, dedupe with -1/-2 suffixes. Matches the
    `[标题](#slug)` anchors people hand-write in their TOCs."""
    s = re.sub(r"<[^>]+>", "", text).strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-") or "section"
    n = seen.get(s, 0)
    seen[s] = n + 1
    return s if n == 0 else f"{s}-{n}"


def render_markdown(text: str) -> str:
    try:
        from markdown_it import MarkdownIt

        md = MarkdownIt("commonmark", {"html": True})
        for rule in ("table", "strikethrough"):
            try:
                md.enable(rule)
            except Exception:  # noqa: BLE001
                pass
        env: dict = {}
        tokens = md.parse(text, env)
        seen: dict = {}
        for i, tok in enumerate(tokens):
            if tok.type == "heading_open" and i + 1 < len(tokens):
                tok.attrSet("id", github_slug(tokens[i + 1].content, seen))
        return md.renderer.render(tokens, md.options, env)
    except Exception:  # noqa: BLE001
        return "<pre>" + html.escape(text) + "</pre>"


# ── 主题分类（首页按主题分组/过滤用）───────────────────────────────
# 显式声明优先：md 前置元数据 `category: <key>` 或 html `<meta name="category" content="<key>">`；
# 否则查 CATEGORY_SEED（现有笔记的种子归类）；都没有则归入 "misc"。
# 新增笔记推荐加一行 category 前置元数据即可自动归类。
CATEGORIES = (
    ("paper", "论文精读"),
    ("attn", "注意力 & 基础"),
    ("arch", "架构 & 系统"),
    ("eng", "工程 & 排查"),
    ("misc", "其他"),
)
CATEGORY_LABELS = dict(CATEGORIES)

CATEGORY_SEED = {
    "dspark-speculative-decoding.md": "paper",
    "frontier_inference_simulation_study.md": "paper",
    "flash-attention-bilibili-study-guide.md": "attn",
    "flash-attention-learning.html": "attn",
    "flash-attention-study.md": "attn",
    "deepseek-v4.html": "attn",
    "transformer_architecture_learning_notes_20260702.html": "arch",
    "7分钟速通MoE_学习文档.md": "arch",
    "dgx_spark_model_architecture.md": "arch",
    "understanding-mamba-state.md": "arch",
    "dsv4-flash-heterogeneous-decode-plan.md": "eng",
    "expert-imbalance-debug-process.html": "eng",
    "p_only_prefill_benchmark_record_20260625.md": "eng",
}

_CAT_FM_RE = re.compile(r"(?m)^category:\s*([A-Za-z]+)\s*$")
_CAT_META_RE = re.compile(
    r"""<meta[^>]+name=["']category["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE
)


def categorize(rel: str, is_html: bool, head: str) -> str:
    if is_html:
        metadata = html_metadata(head or "")
        category = (metadata.get("nj-category") or metadata.get("category") or "").lower()
        if category in CATEGORY_LABELS:
            return category
    else:
        m = _CAT_FM_RE.search(head or "")
        if m and m.group(1).strip().lower() in CATEGORY_LABELS:
            return m.group(1).strip().lower()
    return CATEGORY_SEED.get(rel, "misc")


def list_files(root: Path, exclude: frozenset[str] = frozenset(), only_html: bool = False) -> list[dict]:
    """Scan `root` recursively and return file records (newest first).

    `exclude` is a set of root-relative posix paths to skip (e.g. {"index.html"}).
    Each record's `href` defaults to its own path; the static exporter rewrites
    markdown hrefs to the pre-rendered _md/ page.
    """
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith(".") or path.name in SKIP_NAMES:
            continue
        rel = path.relative_to(root)
        if RENDER_DIR in rel.parts or any(part in SKIP_DIRS for part in rel.parts):
            continue
        relposix = rel.as_posix()
        if relposix in exclude:
            continue
        ext = path.suffix.lower()
        if ext in SKIP_EXTS:
            continue
        is_html = ext in HTML_EXTS
        is_md = ext in MD_EXTS
        if only_html and not is_html:
            continue
        parent = rel.parent.as_posix()
        try:
            stat = path.stat()
        except OSError:
            continue
        if is_html:
            title, excerpt = extract_title(path), html_excerpt(path)
        elif is_md:
            title, excerpt = markdown_title(path), markdown_excerpt(read_head(path))
        else:
            title, excerpt = path.stem, ""
        cat = categorize(relposix, is_html, read_head(path))
        files.append(
            {
                "title": title,
                "excerpt": excerpt,
                "path": relposix,
                "href": relposix,
                "dir": "" if parent == "." else parent,
                "ext": ext.lstrip(".") or "file",
                "is_md": is_md,
                "size": human_size(stat.st_size),
                "mtime": stat.st_mtime,
                "date": time.strftime("%b %d, %Y", time.localtime(stat.st_mtime)),
                "cat": cat,
            }
        )
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files


# Applied before paint to avoid a flash of the wrong theme.
THEME_BOOT = (
    "<script>(function(){try{var t=localStorage.getItem('nj-theme')||"
    "(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');"
    "document.documentElement.dataset.theme=t;}catch(e){}})();</script>"
)

THEME_CSS = r"""
/* Computing Life — Signal redesign */

:root {
  --font-display: "IBM Plex Sans", "Noto Sans SC", system-ui, sans-serif;
  --font-body: "IBM Plex Sans", "Noto Sans SC", system-ui, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
  --font-cjk: "Noto Sans SC", "IBM Plex Sans", sans-serif;

  --r-card: 8px;
  --r-chip: 5px;
  --r-btn: 6px;
  --maxw-prose: 680px;
  --maxw-feed: 960px;
  --maxw-wide: 1140px;

  --paper: #f8f9fb;
  --paper-2: #eef0f4;
  --paper-3: #e5e8ee;
  --ink: #14161c;
  --ink-soft: #545b67;
  --ink-faint: #878e9c;
  --rule: #e1e4ea;
  --rule-soft: #edeff3;
  --accent: #2547e0;
  --accent-ink: #1f3bc4;
  --accent-wash: #e3e7fb;
  --shadow-soft: 0 1px 2px rgba(20,22,30,.04), 0 8px 30px rgba(20,22,30,.05);
  --shadow-lift: 0 2px 6px rgba(20,22,30,.07), 0 18px 50px rgba(20,22,30,.09);
}

[data-theme="dark"] {
  --paper: #0d0f14;
  --paper-2: #161922;
  --paper-3: #1d212b;
  --ink: #e7e9ef;
  --ink-soft: #9aa2b1;
  --ink-faint: #6a7280;
  --rule: #242936;
  --rule-soft: #191d27;
  --accent: #8298ff;
  --accent-ink: #93a5ff;
  --accent-wash: #171b2e;
  --shadow-soft: 0 1px 2px rgba(0,0,0,.3), 0 10px 34px rgba(0,0,0,.4);
  --shadow-lift: 0 2px 8px rgba(0,0,0,.4), 0 22px 60px rgba(0,0,0,.5);
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--font-body);
  font-size: 18px;
  line-height: 1.72;
  font-feature-settings: "kern" 1, "liga" 1, "onum" 1;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  transition: background-color .45s ease, color .45s ease;
}
::selection { background: var(--accent); color: #fff; }
a { color: var(--accent-ink); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 3px; text-decoration-thickness: 1px; }
a:focus-visible, button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
img, video, iframe { max-width: 100%; }
svg { display: block; }

.skip-link { position: fixed; left: 16px; top: 12px; z-index: 100; transform: translateY(-160%); background: var(--accent); color: #fff; padding: 8px 12px; border-radius: var(--r-btn); font-family: var(--font-mono); font-size: 12px; }
.skip-link:focus { transform: none; }
.site { min-height: 100vh; display: flex; flex-direction: column; }
.wrap { width: 100%; max-width: var(--maxw-wide); margin: 0 auto; padding: 0 40px; }
.wrap.prose { max-width: calc(var(--maxw-prose) + 80px); }

.masthead { border-bottom: 1px solid var(--rule); background: color-mix(in srgb, var(--paper) 86%, transparent); position: sticky; top: 0; z-index: 40; backdrop-filter: saturate(1.1) blur(10px); }
.masthead-inner { display: flex; align-items: center; gap: 24px; padding-top: 15px; padding-bottom: 15px; }
.brand, .f-brand { display: flex; align-items: center; gap: 13px; flex: 0 0 auto; color: var(--ink); }
.brand:hover { text-decoration: none; }
.seal { width: 38px; height: 38px; flex: 0 0 auto; border-radius: 9px; border: 1.5px solid var(--accent); color: var(--accent); display: grid; place-items: center; position: relative; transition: background .25s, color .25s; overflow: hidden; }
.seal svg { width: 32px; height: 32px; }
.seal svg > rect:first-child { fill: var(--accent-wash); }
.duck-face { fill: #fff7cf; }
.duck-bill { fill: #d99a24; }
.duck-eye { fill: var(--ink); }
.duck-water { fill: var(--accent); opacity: .78; }
.brand:hover .seal, .f-brand:hover .seal { background: var(--accent-wash); }
.seal::after { content: ""; position: absolute; inset: 4px; border-radius: 5px; border: 1px solid color-mix(in oklab, var(--accent) 30%, transparent); pointer-events: none; }
.wordmark { display: flex; flex-direction: column; line-height: 1; }
.wordmark .ttl, .f-brand .ttl { font-family: var(--font-display); font-weight: 600; font-size: 19px; letter-spacing: -.3px; color: var(--ink); }
.wordmark .ttl em, .f-brand em { font-style: normal; color: var(--accent-ink); }
.wordmark .sub { font-family: var(--font-mono); font-size: 9.5px; letter-spacing: .2em; text-transform: uppercase; color: var(--ink-faint); margin-top: 5px; }

.nav { margin-left: auto; display: flex; align-items: center; gap: 2px; }
.navlink { font-family: var(--font-mono); font-size: 11.5px; letter-spacing: .04em; color: var(--ink-soft); padding: 8px 11px; border-radius: var(--r-btn); white-space: nowrap; transition: color .18s, background .18s; display: inline-flex; align-items: center; gap: 5px; }
.navlink:hover { color: var(--ink); background: var(--paper-2); text-decoration: none; }
.navlink.active { color: var(--accent-ink); }
.ext { font-size: 10px; opacity: .55; }
.nav-sep { width: 1px; height: 22px; background: var(--rule); margin: 0 8px; flex: 0 0 auto; }
.icon-btn { min-width: 44px; min-height: 44px; display: grid; place-items: center; border-radius: var(--r-btn); border: 1px solid var(--rule); background: var(--paper-2); color: var(--ink-soft); cursor: pointer; transition: all .18s; }
.icon-btn:hover { color: var(--ink); border-color: var(--ink-faint); text-decoration: none; }
.icon-btn svg { width: 17px; height: 17px; }
#theme-toggle { position: relative; overflow: hidden; }
#theme-toggle::before, #theme-toggle::after { content: ""; position: absolute; left: 50%; top: 50%; transition: all .18s; }
#theme-toggle::before { width: 16px; height: 16px; border-radius: 50%; background: currentColor; transform: translate(-50%, -50%); }
#theme-toggle::after { width: 16px; height: 16px; border-radius: 50%; background: var(--paper-2); transform: translate(calc(-50% + 5px), calc(-50% - 1px)); }
[data-theme="dark"] #theme-toggle::before { width: 10px; height: 10px; border: 1.5px solid currentColor; background: transparent; }
[data-theme="dark"] #theme-toggle::after { width: 2px; height: 2px; border-radius: 50%; background: transparent; transform: translate(-50%, -50%); box-shadow: 0 -9px 0 currentColor, 0 9px 0 currentColor, 9px 0 0 currentColor, -9px 0 0 currentColor, 6px 6px 0 currentColor, -6px 6px 0 currentColor, 6px -6px 0 currentColor, -6px -6px 0 currentColor; }
.langswitch { display: inline-flex; border: 1px solid var(--rule); border-radius: var(--r-btn); overflow: hidden; background: var(--paper-2); }
.langswitch a { font-family: var(--font-mono); font-size: 11px; letter-spacing: .04em; padding: 10px 11px; color: var(--ink-faint); transition: all .18s; }
.langswitch a:hover { color: var(--ink); text-decoration: none; background: var(--paper-3); }
.langswitch a.on { background: var(--accent); color: #fff; }
.nav-toggle { display: none; flex-direction: column; align-items: center; justify-content: center; gap: 3px; padding: 0; }
.nav-toggle span { width: 16px; height: 1.5px; background: currentColor; display: block; margin: 0; }

.lede { padding: 64px 0 44px; border-bottom: 1px solid var(--rule); max-width: 780px; }
.lede-compact { padding: 34px 0 26px; max-width: var(--maxw-feed); }
.kicker, .lede .kicker, .arch-head .kicker, .article-head .cat-line { font-family: var(--font-mono); font-size: 12px; letter-spacing: .14em; text-transform: uppercase; color: var(--accent-ink); margin: 0 0 18px; display: flex; align-items: center; gap: 10px; }
.kicker::before, .lede .kicker::before, .arch-head .kicker::before, .article-head .cat-line::before { content: ""; width: 22px; height: 1.5px; background: var(--accent); display: inline-block; flex: 0 0 auto; }
.lede h1 { font-family: var(--font-display); font-weight: 600; font-size: clamp(32px, 4.8vw, 50px); line-height: 1.08; letter-spacing: -1.2px; margin: 0 0 20px; color: var(--ink); }
.lede-compact h1 { font-size: clamp(24px, 3vw, 34px); line-height: 1.18; letter-spacing: -.7px; margin-bottom: 12px; max-width: 760px; }
.lede h1 em { font-style: normal; color: var(--accent-ink); }
.lede p { font-size: 18px; color: var(--ink-soft); margin: 0; max-width: 62ch; line-height: 1.6; }
.lede-compact p { font-size: 16.5px; max-width: 72ch; line-height: 1.55; }
.feedhead { display: flex; align-items: baseline; justify-content: space-between; gap: 20px; margin: 42px 0 4px; max-width: var(--maxw-feed); }
.lede-compact + .feedhead { margin-top: 28px; }
.feedhead h2 { font-family: var(--font-mono); font-size: 11.5px; letter-spacing: .16em; text-transform: uppercase; color: var(--ink-faint); font-weight: 500; margin: 0; }
.posts { list-style: none; margin: 0; padding: 0; max-width: var(--maxw-feed); }
.post { display: grid; grid-template-columns: 32px 132px 1fr; gap: 26px; padding: 39px 0; border-bottom: 1px solid var(--rule); align-items: start; }
.post:hover .post-title a { color: var(--accent-ink); }
.post-idx { font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); padding-top: 9px; }
.post-meta { font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); line-height: 1.5; padding-top: 8px; }
.post-meta .date { color: var(--ink-soft); display: block; }
.post-meta .cat { display: inline-block; margin-top: 8px; color: var(--accent-ink); letter-spacing: .03em; }
.post-title { font-family: var(--font-display); font-weight: 600; font-size: 25px; line-height: 1.25; letter-spacing: -.5px; margin: 0 0 10px; }
.post-title a { color: var(--ink); transition: color .18s; }
.post-title a:hover { text-decoration: none; }
.post-summary { color: var(--ink-soft); font-size: 16.5px; line-height: 1.6; margin: 0 0 14px; max-width: 66ch; }
.post-summary p { margin: 0; }
.post-foot { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; }
.tag { font-family: var(--font-mono); font-size: 11px; letter-spacing: .02em; color: var(--ink-soft); padding: 3px 8px; border: 1px solid var(--rule); border-radius: var(--r-chip); background: var(--paper-2); transition: all .15s; }
.tag:hover { border-color: var(--accent); color: var(--accent-ink); text-decoration: none; }
.tag::before { content: "#"; opacity: .45; }
.bilingual-flag, .mini-flag { font-family: var(--font-mono); font-size: 10px; letter-spacing: .06em; color: var(--accent-ink); white-space: nowrap; border: 1px solid color-mix(in oklab, var(--accent) 35%, transparent); border-radius: var(--r-chip); padding: 2px 6px; background: var(--accent-wash); }

.reading { display: grid; grid-template-columns: 1fr; gap: 0; }
.article-head { padding: 56px 0 0; }
.crumb { font-family: var(--font-mono); font-size: 12px; letter-spacing: .04em; color: var(--ink-faint); margin-bottom: 22px; display: flex; gap: 9px; align-items: center; flex-wrap: wrap; }
.crumb a { color: var(--accent-ink); }
.article-head h1 { font-family: var(--font-display); font-weight: 600; font-size: clamp(31px, 4.4vw, 44px); line-height: 1.12; letter-spacing: -1px; margin: 0 0 22px; color: var(--ink); text-wrap: balance; }
.byline { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; font-family: var(--font-mono); font-size: 12.5px; color: var(--ink-soft); padding-bottom: 26px; border-bottom: 1px solid var(--rule); }
.byline .dot { color: var(--rule); }
.lang-toggle-row { display: flex; align-items: center; gap: 12px; margin: 24px 0 0; font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); flex-wrap: wrap; }
.prose-body { font-size: 18.5px; line-height: 1.78; color: var(--ink); padding: 36px 0 10px; overflow-wrap: break-word; }
.prose-body.cjk { font-family: var(--font-cjk); line-height: 1.95; }
.prose-body p { margin: 0 0 1.45em; }
.prose-body h1, .prose-body h2, .prose-body h3, .prose-body h4 { font-family: var(--font-display); font-weight: 600; color: var(--ink); letter-spacing: -.4px; line-height: 1.25; }
.prose-body h2 { font-size: 25px; margin: 2em 0 .7em; padding-top: .3em; }
.prose-body h3 { font-size: 19px; margin: 1.6em 0 .5em; }
.prose-body strong { font-weight: 700; color: var(--ink); }
.prose-body a { text-decoration: underline; text-underline-offset: 3px; text-decoration-color: color-mix(in oklab, var(--accent) 45%, transparent); }
.prose-body ul, .prose-body ol { margin: 0 0 1.45em; padding-left: 1.4em; }
.prose-body li { margin: 0 0 .6em; padding-left: .3em; }
.prose-body li::marker { color: var(--accent); font-family: var(--font-mono); font-size: .85em; }
.prose-body blockquote { margin: 1.8em 0; padding: 4px 0 4px 26px; border-left: 2px solid var(--accent); color: var(--ink-soft); font-size: 1.02em; }
.prose-body code { font-family: var(--font-mono); font-size: .82em; background: var(--paper-2); border: 1px solid var(--rule); border-radius: 5px; padding: 1px 6px; color: var(--accent-ink); white-space: normal; }
.prose-body pre { background: var(--paper-2); border: 1px solid var(--rule); border-radius: var(--r-card); padding: 18px 20px; overflow-x: auto; font-family: var(--font-mono); font-size: 14px; line-height: 1.6; margin: 1.6em 0; }
.prose-body pre code { background: none; border: none; padding: 0; color: var(--ink); white-space: pre; }
.prose-body table { display: block; width: max-content; max-width: 100%; overflow-x: auto; border-collapse: collapse; margin: 1.6em 0; font-size: .92em; }
.prose-body th, .prose-body td { border: 1px solid var(--rule); padding: .55em .7em; text-align: left; }
.prose-body th { background: var(--paper-2); font-weight: 600; }
.prose-body img { border-radius: var(--r-card); height: auto; }
.article-foot { margin-top: 46px; padding-top: 26px; border-top: 1px solid var(--rule); }
.prevnext { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 34px 0; }
.prevnext a { display: block; padding: 18px 20px; border: 1px solid var(--rule); border-radius: var(--r-card); background: var(--paper); transition: all .18s; }
.prevnext a:hover { border-color: var(--accent); text-decoration: none; transform: translateY(-2px); box-shadow: var(--shadow-soft); }
.prevnext .lbl { font-family: var(--font-mono); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-faint); }
.prevnext .t { font-family: var(--font-display); font-weight: 600; font-size: 17px; color: var(--ink); margin-top: 8px; line-height: 1.3; letter-spacing: -.3px; display: block; }
.prevnext .next { text-align: right; }
.comments { margin-top: 40px; }
.comments h2 { font-family: var(--font-mono); font-size: 12px; letter-spacing: .14em; text-transform: uppercase; color: var(--ink-faint); }
.progress { position: fixed; top: 0; left: 0; height: 2px; background: var(--accent); z-index: 60; width: 0; transition: width .1s linear; }

.arch-head { padding: 56px 0 26px; border-bottom: 1px solid var(--rule); }
.arch-head h1 { font-family: var(--font-display); font-weight: 600; font-size: clamp(28px,3.8vw,42px); margin: 0; letter-spacing: -1px; }
.arch-head p:not(.kicker) { color: var(--ink-soft); margin: 13px 0 0; max-width: 58ch; font-size: 16px; }
.arch-filters, .tag-cloud-page { display: flex; gap: 7px; flex-wrap: wrap; padding: 24px 0; border-bottom: 1px solid var(--rule); }
.filter-chip { font-family: var(--font-mono); font-size: 12px; letter-spacing: .02em; padding: 7px 13px; border-radius: var(--r-chip); border: 1px solid var(--rule); background: var(--paper-2); color: var(--ink-soft); transition: all .15s; }
.filter-chip:hover { border-color: var(--ink-faint); color: var(--ink); text-decoration: none; }
.filter-chip.on { background: var(--accent); color: #fff; border-color: var(--accent); }
.filter-chip span { color: var(--ink-faint); margin-left: 4px; }
.year-group { padding: 28px 0; border-bottom: 1px solid var(--rule); display: grid; grid-template-columns: 120px 1fr; gap: 32px; }
.yr { font-family: var(--font-mono); font-size: 34px; font-weight: 300; color: var(--ink-faint); line-height: 1; position: sticky; top: 96px; }
.year-list { list-style: none; margin: 0; padding: 0; }
.arch-item { display: flex; align-items: baseline; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--rule-soft); }
.arch-item:last-child { border-bottom: none; }
.arch-date { font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); flex: 0 0 52px; }
.arch-t { font-family: var(--font-display); font-weight: 500; font-size: 17px; color: var(--ink); flex: 1; line-height: 1.35; transition: color .18s; letter-spacing: -.3px; }
.arch-item:hover .arch-t { color: var(--accent-ink); text-decoration: none; }
.arch-cat { font-family: var(--font-mono); font-size: 11px; color: var(--accent-ink); letter-spacing: .03em; }

.footer { margin-top: auto; border-top: 1px solid var(--rule); background: var(--paper-2); padding: 46px 0 40px; }
.footer-inner { display: flex; gap: 48px; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; }
.footer-about { max-width: 380px; }
.f-meta, .footer address { font-family: var(--font-mono); font-size: 12px; color: var(--ink-faint); line-height: 1.8; max-width: 42ch; font-style: normal; }
.f-links { display: flex; gap: 44px; flex-wrap: wrap; }
.f-col h5 { font-family: var(--font-mono); font-size: 10.5px; letter-spacing: .12em; text-transform: uppercase; color: var(--ink-faint); margin: 0 0 12px; }
.f-col a { display: block; font-family: var(--font-mono); font-size: 13px; color: var(--ink-soft); margin-bottom: 9px; }
.f-col a:hover { color: var(--accent-ink); }
.pager { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 44px 0; font-family: var(--font-mono); max-width: var(--maxw-feed); }
.pager a, .pager .cur { font-size: 13px; width: 38px; height: 38px; display: grid; place-items: center; border: 1px solid var(--rule); border-radius: var(--r-btn); background: var(--paper); color: var(--ink-soft); transition: all .18s; }
.pager a:hover { border-color: var(--accent); color: var(--accent-ink); text-decoration: none; }
.pager .cur { background: var(--accent); color: #fff; border-color: var(--accent); }
.pager-total { font-size: 12px; color: var(--ink-faint); }

/* Kit embeds are content-owned. The theme only prevents layout overflow. */
.prose-body .formkit-form, .prose-body [data-uid] { max-width: 100% !important; margin: 2rem 0 !important; }

@media (min-width: 1100px) {
  .reading { grid-template-columns: 1fr minmax(0, var(--maxw-prose)) 1fr; }
  .reading > .prose-col { grid-column: 2; }
  .article-head-col { grid-column: 2; }
}

@media (max-width: 980px) {
  .masthead-inner { gap: 12px; }
  .nav-toggle { display: flex; min-width: 38px; min-height: 38px; margin: 6px 4px 6px auto; }
  .nav { display: none; position: absolute; left: 20px; right: 20px; top: calc(100% + 8px); flex-direction: column; align-items: stretch; gap: 6px; padding: 14px; border: 1px solid var(--rule); border-radius: var(--r-card); background: var(--paper); box-shadow: var(--shadow-lift); }
  .nav.open { display: flex; }
  .nav-sep { display: none; }
  .nav .navlink, .nav .icon-btn, .nav .langswitch { width: 100%; justify-content: center; }
  .langswitch a { flex: 1; text-align: center; }
}

@media (max-width: 760px) {
  body { font-size: 17px; }
  .wrap { padding: 0 20px; }
  .wordmark .sub { display: none; }
  .lede { padding: 44px 0 34px; }
  .lede-compact { padding: 28px 0 22px; }
  .lede-compact h1 { font-size: clamp(22px, 7vw, 30px); }
  .lede-compact p { font-size: 15.5px; }
  .feedhead { align-items: flex-start; flex-direction: column; gap: 7px; }
  .post { grid-template-columns: 1fr; gap: 8px; padding: 30px 0; }
  .post-idx { display: none; }
  .post-meta { display: flex; gap: 14px; padding-top: 0; flex-wrap: wrap; }
  .post-meta .date { display: inline; }
  .post-meta .cat { margin-top: 0; }
  .article-head { padding-top: 42px; }
  .prose-body { font-size: 17px; line-height: 1.75; }
  .prose-body.cjk { line-height: 1.9; }
  .year-group { grid-template-columns: 1fr; gap: 8px; }
  .yr { position: static; font-size: 24px; }
  .arch-item { flex-wrap: wrap; gap: 8px 12px; }
  .arch-date { flex-basis: auto; }
  .arch-t { flex-basis: 100%; order: 2; }
  .prevnext { grid-template-columns: 1fr; }
  .prevnext .next { text-align: left; }
  .footer-inner, .f-links { gap: 28px; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; transition-duration: .001ms !important; scroll-behavior: auto !important; }
}

"""

_INDEX_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet">
__BOOT__
<style>
__STYLE__
  #searchwrap{ max-width:var(--maxw-feed); margin:18px 0 0; }
  #searchwrap input{ width:100%; padding:12px 16px; font:inherit; font-size:16px; color:var(--ink);
    background:var(--paper-2); border:1px solid var(--rule); border-radius:var(--r-btn); outline:none; }
  #searchwrap input:focus{ border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-wash); }
  #searchwrap[hidden]{ display:none; }
</style>
</head>
<body>
<a class="skip-link" href="#main-content">跳到内容</a>
<div class="site">
  <header class="masthead"><div class="wrap masthead-inner">
    <a class="brand" href="./" aria-label="home">
      <span class="seal" aria-hidden="true"><svg viewBox="0 0 64 64" shape-rendering="crispEdges"><rect x="8" y="8" width="48" height="48" rx="8"></rect><rect x="20" y="20" width="24" height="4" rx="1" fill="var(--accent)"></rect><rect x="20" y="30" width="24" height="4" rx="1" fill="var(--accent)" opacity=".72"></rect><rect x="20" y="40" width="15" height="4" rx="1" fill="var(--accent)" opacity=".5"></rect></svg></span>
      <span class="wordmark"><span class="ttl">__PAGE_TITLE__</span><span class="sub">AI · Systems · Notes</span></span>
    </a>
    <nav class="nav" id="site-nav" aria-label="Primary">
      <a class="navlink active" href="./">首页</a>
      <span class="nav-sep" aria-hidden="true"></span>
      <button class="icon-btn always" id="search-btn" type="button" title="搜索" aria-label="搜索"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3"></path></svg></button>
      <button class="icon-btn" id="theme-toggle" type="button" title="切换主题" aria-label="切换明暗主题"></button>
    </nav>
  </div></header>
  <main id="main-content"><section class="wrap">
    <header class="lede lede-compact">
      <p class="kicker">__PAGE_TITLE__</p>
      <h1>__TAGLINE__</h1>
      <p id="lede-sub"></p>
    </header>
    <div id="searchwrap" hidden><input id="q" type="search" placeholder="搜索标题、摘要或文件名…（按 / 聚焦）" autocomplete="off"></div>
    <div class="feedhead"><h2 id="feed-active">LATEST</h2><h2 id="count"></h2></div>
    <ul class="posts" id="posts"></ul>
  </section></main>
  <footer class="footer"><div class="wrap footer-inner">
    <div class="footer-about">
      <div class="f-brand"><span class="seal" aria-hidden="true"><svg viewBox="0 0 64 64" shape-rendering="crispEdges"><rect x="8" y="8" width="48" height="48" rx="8"></rect><rect x="20" y="20" width="24" height="4" rx="1" fill="var(--accent)"></rect><rect x="20" y="30" width="24" height="4" rx="1" fill="var(--accent)" opacity=".72"></rect><rect x="20" y="40" width="15" height="4" rx="1" fill="var(--accent)" opacity=".5"></rect></svg></span><span class="ttl">__PAGE_TITLE__</span></div>
      <p class="f-meta">__FOOTER__</p>
    </div>
    <div class="f-links" id="f-cats"></div>
  </div></footer>
</div>
<script>
const FILES = __DATA__;
const CATS_ARR = __CATS__;
const CATS = {}; CATS_ARR.forEach(c => CATS[c.k] = c);
const postsEl = document.getElementById('posts');
const countEl = document.getElementById('count');
const feedActive = document.getElementById('feed-active');
const subEl = document.getElementById('lede-sub');
let state = { cat:'all', q:'' };

const themeBtn = document.getElementById('theme-toggle');
themeBtn.addEventListener('click', () => {
  const n = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = n;
  try { localStorage.setItem('nj-theme', n); } catch(e){}
});

const searchBtn = document.getElementById('search-btn');
const searchWrap = document.getElementById('searchwrap');
const qEl = document.getElementById('q');
searchBtn.addEventListener('click', () => {
  if (searchWrap.hasAttribute('hidden')) { searchWrap.removeAttribute('hidden'); qEl.focus(); }
  else { searchWrap.setAttribute('hidden',''); qEl.value=''; state.q=''; render(); }
});
qEl.addEventListener('input', () => { state.q = qEl.value; render(); });
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== qEl) { e.preventDefault(); searchWrap.removeAttribute('hidden'); qEl.focus(); }
  if (e.key === 'Escape' && document.activeElement === qEl) { qEl.value=''; state.q=''; searchWrap.setAttribute('hidden',''); render(); }
});

function esc(s){ return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function url(p){ return encodeURI(p); }
function catLabel(k){ return CATS[k] ? CATS[k].label : k; }

function filtered(){
  const q = state.q.trim().toLowerCase();
  return FILES.filter(f => (state.cat==='all' || f.cat===state.cat) &&
    (!q || f.title.toLowerCase().includes(q) || (f.excerpt||'').toLowerCase().includes(q) || f.path.toLowerCase().includes(q)));
}
function render(){
  const items = filtered();
  countEl.textContent = items.length + ' POSTS';
  feedActive.textContent = state.cat==='all' ? (state.q ? 'RESULTS' : 'LATEST') : catLabel(state.cat).toUpperCase();
  if (!items.length){ postsEl.innerHTML = '<li style="padding:44px 0;color:var(--ink-soft)">没有匹配的笔记。</li>'; return; }
  postsEl.innerHTML = items.map((f,i) => {
    const idx = String(i+1).padStart(2,'0');
    const fmt = (f.ext||'').toUpperCase();
    return '<li class="post">'
      + '<div class="post-idx">' + idx + '</div>'
      + '<div class="post-meta"><time class="date">' + esc(f.date) + '</time>'
        + '<a class="cat" href="#" data-cat="' + esc(f.cat) + '">' + esc(catLabel(f.cat)) + '</a></div>'
      + '<article class="post-main">'
        + '<h3 class="post-title"><a href="' + url(f.href||f.path) + '">' + esc(f.title) + '</a></h3>'
        + (f.excerpt ? '<div class="post-summary"><p>' + esc(f.excerpt) + '</p></div>' : '')
        + '<div class="post-foot"><div class="tags">'
          + '<a class="tag" href="#" data-cat="' + esc(f.cat) + '">' + esc(catLabel(f.cat)) + '</a>'
          + (f.dir ? '<span class="tag">' + esc(f.dir) + '</span>' : '')
        + '</div><span class="bilingual-flag">' + esc(fmt) + '</span></div>'
      + '</article></li>';
  }).join('');
  postsEl.querySelectorAll('[data-cat]').forEach(el => el.addEventListener('click', e => {
    e.preventDefault();
    const c = el.getAttribute('data-cat');
    state.cat = (state.cat===c) ? 'all' : c;
    window.scrollTo({ top:0, behavior:'smooth' });
    render();
  }));
}
subEl.textContent = '共 ' + FILES.length + ' 篇 · 最近更新 ' + (FILES.length ? FILES[0].date : '—');
document.getElementById('f-cats').innerHTML = '<div class="f-col"><h5>主题</h5>' +
  CATS_ARR.map(c => '<a href="#" data-cat="' + c.k + '">' + esc(c.label) + ' · ' + c.n + '</a>').join('') + '</div>';
document.getElementById('f-cats').querySelectorAll('[data-cat]').forEach(el => el.addEventListener('click', e => {
  e.preventDefault(); state.cat = el.getAttribute('data-cat');
  window.scrollTo({ top:0, behavior:'smooth' }); render();
}));
render();
</script>
</body>
</html>
"""
PAGE_TEMPLATE = _INDEX_TEMPLATE.replace("__BOOT__", THEME_BOOT).replace("__STYLE__", THEME_CSS)

_MD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<base href="__BASE__">
<title>__MD_TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet">
__BOOT__
<style>
__STYLE__
</style>
</head>
<body>
<div class="progress" id="progress"></div>
<div class="site">
  <header class="masthead"><div class="wrap masthead-inner">
    <a class="brand" href="__BRAND_HREF__" aria-label="home">
      <span class="seal" aria-hidden="true"><svg viewBox="0 0 64 64" shape-rendering="crispEdges"><rect x="8" y="8" width="48" height="48" rx="8"></rect><rect x="20" y="20" width="24" height="4" rx="1" fill="var(--accent)"></rect><rect x="20" y="30" width="24" height="4" rx="1" fill="var(--accent)" opacity=".72"></rect><rect x="20" y="40" width="15" height="4" rx="1" fill="var(--accent)" opacity=".5"></rect></svg></span>
      <span class="wordmark"><span class="ttl">研究笔记</span><span class="sub">AI · Systems · Notes</span></span>
    </a>
    <nav class="nav" aria-label="Primary">
      __NAV_HOME__
      <span class="nav-sep" aria-hidden="true"></span>
      <button class="icon-btn" id="theme-toggle" type="button" title="切换主题" aria-label="切换明暗主题"></button>
    </nav>
  </div></header>
  <main id="main-content"><div class="wrap prose">
    <header class="article-head">
      __CRUMB__
      <h1>__MD_TITLE__</h1>
      <div class="byline"><span>__DATE__</span><span class="dot">·</span><span>__SOURCE__</span></div>
    </header>
    <article class="prose-body cjk">
__BODY__
    </article>
    <div class="article-foot">__FOOT_HOME__</div>
  </div></main>
  <footer class="footer"><div class="wrap footer-inner">
    <div class="footer-about"><div class="f-brand"><span class="seal" aria-hidden="true"><svg viewBox="0 0 64 64" shape-rendering="crispEdges"><rect x="8" y="8" width="48" height="48" rx="8"></rect><rect x="20" y="20" width="24" height="4" rx="1" fill="var(--accent)"></rect><rect x="20" y="30" width="24" height="4" rx="1" fill="var(--accent)" opacity=".72"></rect><rect x="20" y="40" width="15" height="4" rx="1" fill="var(--accent)" opacity=".5"></rect></svg></span><span class="ttl">研究笔记</span></div></div>
  </div></footer>
</div>
<script>
const tb = document.getElementById('theme-toggle');
tb && tb.addEventListener('click', () => {
  const n = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = n;
  try { localStorage.setItem('nj-theme', n); } catch(e){}
});
const bar = document.getElementById('progress');
function tick(){ const h=document.documentElement; const m=h.scrollHeight-h.clientHeight;
  bar.style.width = (m>0 ? (h.scrollTop/m*100) : 0) + '%'; }
document.addEventListener('scroll', tick, {passive:true}); window.addEventListener('resize', tick); tick();
</script>
</body>
</html>
"""
MD_PAGE_TEMPLATE = _MD_TEMPLATE.replace("__BOOT__", THEME_BOOT).replace("__STYLE__", THEME_CSS)

# Shared (standalone) markdown: content only — no masthead, no footer, no nav.
# The analogue of "查看原稿" for a rendered page: just the prose on the page bg.
_MD_BARE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<base href="__BASE__">
<title>__MD_TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet">
__BOOT__
<style>
__STYLE__
  .site { padding: 0; }
  .bare { padding: 40px 0 64px; }
  .bare .prose-body { padding-top: 0; }
  .bare h1.doc-title { font-family: var(--font-display); font-weight: 600; font-size: clamp(28px, 4vw, 40px); line-height: 1.14; letter-spacing: -.8px; margin: 0 0 28px; color: var(--ink); text-wrap: balance; }
</style>
</head>
<body>
<div class="site"><main id="main-content"><div class="wrap prose bare">
  <article class="prose-body cjk">
    <h1 class="doc-title">__MD_TITLE__</h1>
__BODY__
  </article>
</div></main></div>
</body>
</html>
"""
MD_BARE_PAGE_TEMPLATE = _MD_BARE_TEMPLATE.replace("__BOOT__", THEME_BOOT).replace("__STYLE__", THEME_CSS)


def is_self_contained_html(text: str) -> bool:
    """True if the document carries its own behavior or presentation.

    Interactive pages need their scripts, and polished visual artifacts need
    their authored styles. Passing either through html_to_reader_body strips
    the defining content, so serve them verbatim instead of re-skinning them in
    the reader shell."""
    return re.search(r"(?is)<(?:script|style)\b", text) is not None


def html_layout(text: str) -> str:
    """Return the explicit doc-shell-v1 layout, or an empty string for legacy HTML."""
    layout = html_metadata(text).get("nj-layout", "").lower()
    return layout if layout in {"artifact-v1", "isolated"} else ""


def _chrome_script_tag(
    *, title: str, category: str, updated: str, source: str, home: str, site_title: str,
    standalone: bool = False,
) -> str:
    attrs = {
        "data-shell-version": SITE_CHROME_VERSION,
        "data-home": home,
        "data-site-title": site_title,
        "data-page-title": title,
        "data-category": category,
        "data-updated": updated,
        "data-source": source,
    }
    if standalone:
        # A shared page must not advertise or link back to the site home.
        attrs["data-standalone"] = "1"
    encoded = " ".join(
        f'{name}="{html.escape(str(value), quote=True)}"' for name, value in attrs.items()
    )
    return (
        f'<script id="nj-site-chrome-script" src="/assets/site-chrome.js?v={SITE_CHROME_VERSION}" '
        f"{encoded} defer></script>"
    )


def inject_site_chrome(
    text: str,
    *,
    title: str,
    category: str,
    updated: str,
    source: str,
    home: str = "/",
    site_title: str = "HTML 学习资料",
    standalone: bool = False,
) -> str:
    """Inject the shared Shadow-DOM site chrome without touching artifact content."""
    if 'id="nj-site-chrome-script"' in text or "id='nj-site-chrome-script'" in text:
        return text
    script = _chrome_script_tag(
        title=title,
        category=category,
        updated=updated,
        source=source,
        home=home,
        site_title=site_title,
        standalone=standalone,
    )
    if re.search(r"</head\s*>", text, re.IGNORECASE):
        return re.sub(r"</head\s*>", script + "\n</head>", text, count=1, flags=re.IGNORECASE)
    if re.search(r"<body\b", text, re.IGNORECASE):
        return re.sub(r"<body\b", script + "\n<body", text, count=1, flags=re.IGNORECASE)
    return script + "\n" + text


def build_isolated_page(
    *,
    title: str,
    category: str,
    updated: str,
    source: str,
    raw_url: str,
    site_title: str = "HTML 学习资料",
    standalone: bool = False,
) -> str:
    """Wrap a complex autonomous HTML artifact in a uniform shell and iframe."""
    script = _chrome_script_tag(
        title=title,
        category=category,
        updated=updated,
        source=source,
        home="/",
        site_title=site_title,
        standalone=standalone,
    )
    escaped_title = html.escape(title)
    escaped_url = html.escape(raw_url, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped_title}</title>
{THEME_BOOT}
{script}
<style>
  :root {{ color-scheme: light; --nj-frame-bg:#f8f9fb; --nj-frame-line:#e1e4ea; }}
  :root[data-theme="dark"] {{ color-scheme: dark; --nj-frame-bg:#0d0f14; --nj-frame-line:#242936; }}
  * {{ box-sizing:border-box; }}
  html,body {{ margin:0; min-height:100%; background:var(--nj-frame-bg); }}
  .nj-isolated-stage {{ width:100%; margin:0; padding:0; }}
  #nj-isolated-frame {{ display:block; width:100%; min-height:72vh; border:0; background:transparent; }}
  @media print {{ nj-site-header,nj-site-footer {{ display:none !important; }} }}
</style>
</head>
<body>
<main class="nj-isolated-stage" id="main-content">
  <iframe id="nj-isolated-frame" src="{escaped_url}" title="{escaped_title}"></iframe>
</main>
<script>
(() => {{
  const frame = document.getElementById('nj-isolated-frame');
  let resizeObserver;
  const syncTheme = () => {{
    try {{ frame.contentDocument.documentElement.dataset.theme = document.documentElement.dataset.theme || 'light'; }} catch (_) {{}}
  }};
  const resize = () => {{
    try {{
      const doc = frame.contentDocument;
      const height = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0, 640);
      frame.style.height = height + 'px';
    }} catch (_) {{}}
  }};
  frame.addEventListener('load', () => {{
    syncTheme(); resize();
    try {{
      if ('ResizeObserver' in window) {{
        resizeObserver = new ResizeObserver(resize);
        resizeObserver.observe(frame.contentDocument.documentElement);
      }}
    }} catch (_) {{}}
  }});
  new MutationObserver(syncTheme).observe(document.documentElement, {{attributes:true, attributeFilter:['data-theme']}});
  window.addEventListener('resize', resize, {{passive:true}});
}})();
</script>
</body>
</html>"""


def html_to_reader_body(text: str) -> str:
    """Extract a user-authored HTML document's content so it can be re-rendered
    inside our reader shell (build_md_page). We keep the semantic markup
    (headings, tables, code, lists) and drop the document's own <script>/<style>
    and first <h1> (shown as the page title), so our theme styles it uniformly."""
    match = re.search(r"(?is)<body[^>]*>(.*?)</body>", text)
    body = match.group(1) if match else text
    body = re.sub(r"(?is)<script[^>]*>.*?</script>", "", body)
    body = re.sub(r"(?is)<style[^>]*>.*?</style>", "", body)
    body = re.sub(r"(?is)<h1[^>]*>.*?</h1>", "", body, count=1)
    return body.strip()


def build_index_html(title: str, tagline: str, files: list[dict], footer: str | None = None) -> str:
    keys = ("title", "excerpt", "path", "href", "dir", "ext", "date", "cat")
    public = [{k: f.get(k, "") for k in keys} for f in files]
    counts: dict = {}
    for f in files:
        c = f.get("cat", "misc")
        counts[c] = counts.get(c, 0) + 1
    cats_public = [
        {"k": k, "label": lab, "n": counts[k]} for k, lab in CATEGORIES if counts.get(k)
    ]
    if footer is None:
        footer = f"© {time.strftime('%Y')} · 共 {len(files)} 篇 · {time.strftime('%Y-%m-%d %H:%M')}"
    return (
        PAGE_TEMPLATE
        .replace("__PAGE_TITLE__", html.escape(title))
        .replace("__TAGLINE__", html.escape(tagline))
        .replace("__FOOTER__", html.escape(footer))
        .replace("__CATS__", json.dumps(cats_public, ensure_ascii=False))
        .replace("__DATA__", json.dumps(public, ensure_ascii=False))
    )


def build_md_page(title, source, date, body, base_href, back_href, standalone=False):
    # A <base> tag breaks in-page `#anchor` links (they resolve against the base
    # URL, not the current page). The dynamic server serves each page at its own
    # URL so relative links already resolve correctly — it passes base_href="" to
    # drop the tag. Only the static exporter (pages relocated under _md/) needs it.
    base_tag = f'<base href="{html.escape(base_href, quote=True)}">' if base_href else ""
    if standalone:
        # Shared page: content only, no site chrome (masthead/footer/nav).
        return (
            MD_BARE_PAGE_TEMPLATE
            .replace('<base href="__BASE__">', base_tag)
            .replace("__MD_TITLE__", html.escape(title))
            .replace("__BODY__", body)
        )
    esc_source = html.escape(source)
    back = html.escape(back_href, quote=True)
    nav_home = f'<a class="navlink" href="{back}">← 索引</a>'
    crumb = f'<div class="crumb"><a href="{back}">首页</a><span>/</span><span>{esc_source}</span></div>'
    foot_home = f'<a class="navlink" href="{back}">← 返回索引</a>'
    return (
        MD_PAGE_TEMPLATE
        .replace('<base href="__BASE__">', base_tag)
        .replace("__MD_TITLE__", html.escape(title))
        .replace("__BRAND_HREF__", back)
        .replace("__NAV_HOME__", nav_home)
        .replace("__CRUMB__", crumb)
        .replace("__FOOT_HOME__", foot_home)
        .replace("__SOURCE__", esc_source)
        .replace("__DATE__", html.escape(date))
        .replace("__BODY__", body)
    )
