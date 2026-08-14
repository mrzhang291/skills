#!/usr/bin/env python3

"""URL normalization and hard separation between discovery pages and evidence pages."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit


TRACKING_KEYS = {
    "from", "source", "ref", "referrer", "fbclid", "gclid", "msclkid", "spm",
    "scm", "scene", "share_token", "share_source", "campaign", "campaignid",
    "trace_id", "hit_type", "keyword_360", "pa_pids", "from_360",
}

SEARCH_HOSTS = {
    "bing.com", "www.bing.com", "cn.bing.com",
    "so.com", "www.so.com",
    "search.yahoo.com", "yahoo.com", "www.yahoo.com",
    "baidu.com", "www.baidu.com", "m.baidu.com",
    "sogou.com", "www.sogou.com",
    "google.com", "www.google.com",
    "duckduckgo.com", "html.duckduckgo.com",
    "yandex.com", "yandex.ru", "search.brave.com",
    "yimg.com", "s.yimg.com", "map.360.cn",
}

PLATFORM_SEARCH_HOSTS = {
    "search.jd.com", "search.suning.com", "search.dangdang.com",
    "s.taobao.com", "list.tmall.com", "s.1688.com", "mobile.yangkeduo.com",
}

# A deliberately small, dependency-free subset of common multi-label public
# suffixes.  This is not a replacement for the Public Suffix List; it prevents
# obvious sibling-subdomain budget bypasses on the sites most likely to appear
# in a CN trademark investigation.  The function is therefore named site_key,
# not eTLD+1.
COMMON_MULTI_LABEL_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn", "mil.cn",
    "bj.cn", "sh.cn", "tj.cn", "cq.cn", "gd.cn", "zj.cn", "js.cn", "fj.cn",
    "sd.cn", "hb.cn", "hn.cn", "sc.cn", "ln.cn", "jl.cn", "hl.cn", "ah.cn",
    "jx.cn", "gx.cn", "hi.cn", "gz.cn", "yn.cn", "xz.cn", "sn.cn", "gs.cn",
    "qh.cn", "nx.cn", "xj.cn", "tw.cn", "hk.cn", "mo.cn",
    "co.uk", "org.uk", "gov.uk", "ac.uk", "sch.uk", "ltd.uk", "plc.uk", "me.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "co.kr", "ne.kr", "or.kr",
    "com.hk", "net.hk", "org.hk", "edu.hk", "gov.hk", "com.tw", "net.tw",
    "org.tw", "edu.tw", "gov.tw", "com.sg", "net.sg", "org.sg", "com.my",
    "com.br", "com.mx", "co.nz", "co.in", "firm.in", "net.in", "org.in",
}

WINDOWS_RESERVED_BASENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
SAFE_FILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)


def _host_matches(host: str, known: set[str]) -> bool:
    host = (host or "").lower().split(":", 1)[0].strip(".")
    return any(host == item or host.endswith("." + item) for item in known)


def unwrap_search_redirect(raw: str) -> str:
    """Decode common Bing/Yahoo/search-engine redirect wrappers without fetching them."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    decoded = unquote(raw)
    yahoo = re.search(r"/RU=([^/]+)/RK=", decoded, re.I)
    if yahoo:
        return unquote(yahoo.group(1))
    try:
        parts = urlsplit(raw)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key in ("url", "target", "dest", "destination", "redirect", "redirect_url", "r"):
            value = params.get(key)
            if value and value.startswith(("http://", "https://")):
                return unquote(value)
        value = params.get("u")
        if value:
            if value.startswith("a1"):
                payload = value[2:].replace("-", "+").replace("_", "/")
                payload += "=" * (-len(payload) % 4)
                try:
                    candidate = base64.b64decode(payload).decode("utf-8", errors="strict")
                    if candidate.startswith(("http://", "https://")):
                        return candidate
                except Exception:
                    pass
            if value.startswith(("http://", "https://")):
                return unquote(value)
    except Exception:
        pass
    return raw


def normalize_url(raw: str) -> str | None:
    raw = unwrap_search_redirect(raw).strip().rstrip(".,;:!?)]}>，。；：！？）】》'\"")
    try:
        parts = urlsplit(raw)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return None
        host = (parts.hostname or "").lower().strip(".")
        if not host:
            return None
        port = parts.port
        netloc = host
        if port and not ((parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)):
            netloc = f"{host}:{port}"
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lower = key.lower()
            if lower.startswith("utm_") or lower in TRACKING_KEYS:
                continue
            query.append((key, value))
        query.sort(key=lambda item: (item[0].lower(), item[1]))
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        if re.search(r"\.(?:js|css|woff2?|ttf|map)(?:$|/)", path, re.I):
            return None
        return urlunsplit((
            parts.scheme.lower(), netloc, path,
            urlencode(query, doseq=True, quote_via=quote), "",
        ))
    except Exception:
        return None


def hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def site_key(value: str) -> str:
    """Return a conservative registrable-site budget key.

    IP literals and localhost-like single-label hosts remain unchanged.  For
    ordinary DNS names this collapses sibling subdomains, with explicit
    handling for common multi-label public suffixes such as ``com.cn``.
    """
    host = hostname(value) if "://" in (value or "") else (value or "").lower().strip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = [item for item in host.split(".") if item]
    if len(labels) <= 1:
        return host
    suffix2 = ".".join(labels[-2:])
    if suffix2 in COMMON_MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix2


def is_safe_file_id(value: str) -> bool:
    """Reject path separators, traversal and Win32 alias/canonicalization traps."""
    if not isinstance(value, str) or not SAFE_FILE_ID_RE.fullmatch(value) or ".." in value:
        return False
    if value.endswith((".", " ")):
        return False
    # Windows treats CON.txt and COM1.anything as reserved device names too.
    return value.split(".", 1)[0].upper() not in WINDOWS_RESERVED_BASENAMES


def require_safe_file_id(value: str, label: str) -> str:
    if not is_safe_file_id(value):
        raise ValueError(
            f"{label} contains unsupported characters or a Windows-reserved/trailing-dot name"
        )
    return value


def is_search_result_url(url: str, include_platform_search: bool = True) -> bool:
    try:
        parts = urlsplit(url)
    except Exception:
        return True
    host = (parts.hostname or "").lower()
    path = (parts.path or "/").lower()
    query = (parts.query or "").lower()
    if _host_matches(host, SEARCH_HOSTS):
        return True
    if include_platform_search and _host_matches(host, PLATFORM_SEARCH_HOSTS):
        return True
    if include_platform_search:
        patterns = (
            ("jd.com", "/search"), ("taobao.com", "/search"),
            ("tmall.com", "/search"), ("1688.com", "/offer_search"),
            ("suning.com", "/search"), ("dangdang.com", "/search"),
        )
        if any((host == domain or host.endswith("." + domain)) and marker in path for domain, marker in patterns):
            return True
        if ("keyword=" in query or "q=" in query) and re.search(r"/(search|s)(/|$)", path):
            return True
    return False


def target_id(url: str) -> str:
    return "T" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12].upper()
