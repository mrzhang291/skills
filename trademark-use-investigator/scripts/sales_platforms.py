#!/usr/bin/env python3

"""Shared sales-platform definitions and URL classification helpers."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_policy import SALES_PLATFORMS


PLATFORMS = {
    "taobao": {"label": "淘宝", "domains": ("taobao.com",), "home_url": "https://www.taobao.com/", "api_method": "taobao.tbk.dg.material.optional.upgrade", "api_scope": "affiliate_catalog"},
    "tmall": {"label": "天猫", "domains": ("tmall.com",), "home_url": "https://www.tmall.com/", "api_method": "tmall.selected.items.search", "api_scope": "selected_catalog"},
    "jd": {"label": "京东", "domains": ("jd.com",), "home_url": "https://www.jd.com/", "api_method": "jd.union.open.goods.query", "api_scope": "affiliate_catalog"},
    "1688": {"label": "1688", "domains": ("1688.com",), "home_url": "https://www.1688.com/", "api_method": "alibaba.product.search", "api_scope": "authorized_catalog"},
    "pinduoduo": {"label": "拼多多", "domains": ("pinduoduo.com", "yangkeduo.com"), "home_url": "https://www.pinduoduo.com/", "api_method": "pdd.ddk.goods.search", "api_scope": "affiliate_catalog"},
    "suning": {"label": "苏宁易购", "domains": ("suning.com",), "home_url": "https://www.suning.com/"},
    "dangdang": {"label": "当当", "domains": ("dangdang.com",), "home_url": "https://www.dangdang.com/"},
    "vip": {"label": "唯品会", "domains": ("vip.com",), "home_url": "https://www.vip.com/", "api_method": "UnionGoodsService.goodsListV2", "api_scope": "affiliate_catalog"},
    "amazon_cn": {"label": "亚马逊中国", "domains": ("amazon.cn",), "home_url": "https://www.amazon.cn/"},
    "douyin": {"label": "抖音", "domains": ("douyin.com", "jinritemai.com"), "home_url": "https://www.douyin.com/", "api_method": "/product/listV2", "api_scope": "authorized_shop_only"},
    "kuaishou": {"label": "快手小店", "domains": ("kuaishou.com", "kwaixiaodian.com"), "home_url": "https://www.kuaishou.com/", "api_method": "商品管理接口族", "api_scope": "authorized_shop_only"},
    "xiaohongshu": {"label": "小红书", "domains": ("xiaohongshu.com",), "home_url": "https://www.xiaohongshu.com/", "api_method": "商家商品管理接口族", "api_scope": "authorized_shop_only"},
}

QUICK_PLATFORM_ORDER = ("taobao", "tmall", "jd", "1688", "pinduoduo", "suning", "dangdang")
API_DISCOVERY_PLATFORM_ORDER = ("taobao", "tmall", "jd", "1688", "pinduoduo", "vip")
AUTHORIZED_SHOP_PLATFORM_ORDER = ("douyin", "kuaishou", "xiaohongshu")
FILING_PLATFORM_ORDER = SALES_PLATFORMS

_unknown_policy_platforms = [platform for platform in FILING_PLATFORM_ORDER if platform not in PLATFORMS]
if _unknown_policy_platforms:
    raise RuntimeError(
        "runtime-policy.json contains sales platforms without capability definitions: "
        + ", ".join(_unknown_policy_platforms)
    )


def platform_search_url(platform: str, query: str) -> str:
    """Return a normal human-facing search URL without browser automation."""
    normalized = re.sub(r"\s+", " ", str(query or "").replace("+", " ").replace("＋", " ")).strip()
    encoded = quote(normalized, safe="")
    if not encoded:
        raise ValueError("Search query cannot be empty")
    templates = {
        "taobao": "https://s.taobao.com/search?q={query}",
        "tmall": "https://list.tmall.com/search_product.htm?q={query}",
        "jd": "https://search.jd.com/Search?keyword={query}&enc=utf-8",
        "1688": "https://s.1688.com/selloffer/offer_search.htm?keywords={query}",
        "pinduoduo": "https://mobile.yangkeduo.com/search_result.html?search_key={query}",
        "suning": "https://search.suning.com/{query}/",
        "dangdang": "http://search.dangdang.com/?key={query}",
    }
    template = templates.get(platform)
    if template is None:
        raise ValueError(f"No human search URL is configured for platform: {platform}")
    return template.format(query=encoded)


def clean_host(value: str) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def host_matches(host: str, domain: str) -> bool:
    host = clean_host(host)
    domain = clean_host(domain)
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def platform_for_host(host: str) -> tuple[str, dict] | tuple[None, None]:
    for key, config in PLATFORMS.items():
        if any(host_matches(host, domain) for domain in config["domains"]):
            return key, config
    return None, None


def platform_for_url(url: str) -> tuple[str, dict] | tuple[None, None]:
    try:
        return platform_for_host(urlsplit(url).hostname or "")
    except ValueError:
        return None, None


def allowed_url(url: str, domains: list[str] | tuple[str, ...]) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return any(host_matches(host, domain) for domain in domains)


def product_page_kind(platform: str, url: str) -> str | None:
    """Return a useful sales-page kind, or None for shells/download/login pages."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = clean_host(parsed.hostname or "")
    path = parsed.path.lower()
    query = parsed.query.lower()

    if platform == "taobao":
        if "item.htm" in path or "/list/item/" in path:
            return "product"
        if re.fullmatch(r"shop\d+\.taobao\.com", host) or path.startswith("/shop/"):
            return "shop"
    elif platform == "tmall":
        if host_matches(host, "detail.tmall.com") and ("item.htm" in path or "id=" in query):
            return "product"
        if "tmall.com" in host and ("shop" in host or "/shop" in path):
            return "shop"
    elif platform == "jd":
        if host_matches(host, "item.jd.com") and re.search(r"/\d+\.html$", path):
            return "product"
        if any(token in path for token in ("/chanpin/", "/hprm/", "/phb/")):
            return "category"
        if "mall.jd.com" in host or "shop.jd.com" in host:
            return "shop"
    elif platform == "1688":
        if host_matches(host, "detail.1688.com") and "/offer/" in path:
            return "product"
        if host_matches(host, "1688.com") and "offerid=" in query:
            return "product"
        if "/shop/" in path or "/offer/" in path:
            return "shop_or_product"
        if any(token in path for token in ("/brand/", "/chanpin/")):
            return "category"
    elif platform == "pinduoduo":
        if "goods_id=" in query:
            return "product"
        if "/goods" in path:
            return "product"
    elif platform == "suning":
        if "/item/" in path:
            return "product"
        if "/shop/" in path:
            return "shop"
    elif platform == "dangdang":
        if host_matches(host, "product.dangdang.com") and path.endswith(".html"):
            return "product"
    elif platform == "vip":
        if "/detail-" in path or "/product-" in path:
            return "product"
    elif platform == "amazon_cn":
        if "/dp/" in path or "/gp/product/" in path:
            return "product"
        if "/s" == path or path.startswith("/s/") or "/b" == path:
            return "category"
    elif platform in {"douyin", "kuaishou", "xiaohongshu"}:
        if any(token in path for token in ("/note/", "/product/", "/goods/", "/explore/")):
            return "social_commerce"
    return None
