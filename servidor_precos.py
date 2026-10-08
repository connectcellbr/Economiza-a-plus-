#!/usr/bin/env python3
"""
Economiza AI — Servidor local de preços reais
======================================
Roda a busca no Zoom e devolve JSON para o painel HTML.

Uso:
  python servidor_precos.py

Depois abra o painel: http://127.0.0.1:8765/
"""

from __future__ import annotations

import json
import os
import re
import time
import threading
import ipaddress
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from auto_aprendizado import record_event, update_metric, snapshot, learn, propose_improvements, get_status
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

APP_VERSION = "2026-10-08-r9"
DEFAULT_PORT = 8765
PORT = int(__import__("os").environ.get("PORT", __import__("os").environ.get("PRECO_PORT", DEFAULT_PORT)))
DIR = Path(__file__).resolve().parent
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "").strip()
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "").strip().rstrip("/")
MAX_QUERY_LEN = 240
MAX_LEARN_BODY = 64 * 1024

# Cache em memória: query normalizada -> (timestamp, payload)
# TTL de 18 minutos reduz carga no Render e estabiliza resultados.
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SEC = 18 * 60
_CACHE_MAX_ENTRIES = 120

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# URLs de busca por loja (só para referência / fallback de descoberta — nunca destino final).
STORE_SEARCH_URLS: Dict[str, str] = {
    "Mercado Livre": "https://lista.mercadolivre.com.br/",
    "Amazon Brasil": "https://www.amazon.com.br/s?k=",
    "Magazine Luiza": "https://www.magazineluiza.com.br/busca/",
    "KaBuM!": "https://www.kabum.com.br/busca/",
    "Casas Bahia": "https://www.casasbahia.com.br/busca/",
    "Americanas": "https://www.americanas.com.br/busca/",
    "Shopee": "https://shopee.com.br/search?keyword=",
    "Carrefour": "https://www.carrefour.com.br/busca/",
    "Ponto": "https://www.pontofrio.com.br/busca/",
    "Extra": "https://www.extra.com.br/busca/",
    "Pichau": "https://www.pichau.com.br/search?q=",
}


def parse_price(price_str: str) -> Optional[float]:
    if not price_str:
        return None
    cleaned = re.sub(r"[R$\s]", "", price_str)
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_installments(text: str) -> str:
    m = re.search(r"(\d+\s*x\s*de\s*R\$\s*[\d.,]+)(?:\s*sem\s*juros)?", text, re.I)
    if m:
        result = m.group(0).strip()
        if "sem juros" in text.lower() and "sem juros" not in result.lower():
            result += " sem juros"
        return result
    return ""


def extract_cashback(text: str) -> str:
    m = re.search(r"(\d+%\s*de\s*volta)", text, re.I)
    if m:
        return m.group(1)
    if "cashback" in text.lower():
        return "Cashback disponível"
    return ""


def is_new_product(name: str, text: str) -> bool:
    lower = (name + " " + text).lower()
    return not any(w in lower for w in ["usado", "seminovo", "recondicionado", "open box"])


def search_zoom(product: str, max_results: int = 15) -> List[Dict[str, Any]]:
    query = urllib.parse.quote_plus(product)
    url = f"https://www.zoom.com.br/search?q={query}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=8)
        response.raise_for_status()
    except requests.RequestException as e:
        return {"error": str(e), "results": []}  # type: ignore

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select('article[class*="OrqProductCard"]')

    results: List[Dict[str, Any]] = []
    keywords = [k.lower() for k in product.split() if len(k) > 2]

    for card in cards[: max_results * 3]:
        full_text = card.get_text(separator=" | ", strip=True)

        name_el = card.select_one('[class*="Name_OrqProductCard_Name"]')
        name = name_el.get_text(strip=True) if name_el else "Nome não encontrado"

        price_text = None
        for s in card.find_all(string=re.compile(r"^R\$\s*[\d.,]+$")):
            parent_text = s.parent.get_text() if s.parent else ""
            if "x de" in parent_text.lower():
                continue
            price_text = s.strip()
            break
        if not price_text:
            matches = re.findall(r"R\$\s*[\d.,]+", full_text)
            for m in matches:
                if "x de" not in full_text.lower().split(m)[0][-25:]:
                    price_text = m
                    break

        price_value = parse_price(price_text) if price_text else None
        if price_value is None:
            continue

        store = "—"
        via = card.find(string=re.compile(r"Via\s+.+"))
        if via:
            store = re.sub(r"^Via\s+", "", via.strip()).strip()
            store = re.split(r"\s{2,}|\|", store)[0].strip()

        link_el = card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        link = ("https://www.zoom.com.br" + href) if href.startswith("/") else href

        rating = "—"
        rating_match = re.search(r"(\d(?:\.\d)?)\s*\(\d+\)", full_text)
        if rating_match:
            rating = rating_match.group(1)

        installments = extract_installments(full_text)
        cashback = extract_cashback(full_text)

        shipping_text = "A calcular"
        shipping_cost = None
        if "frete grátis" in full_text.lower() or "frete gratis" in full_text.lower():
            shipping_text = "Grátis"
            shipping_cost = 0.0

        is_new = is_new_product(name, full_text)

        name_lower = name.lower()
        relevance = sum(1 for k in keywords if k in name_lower)
        main_phrase = " ".join(keywords[:2]) if len(keywords) >= 2 else (keywords[0] if keywords else "")
        if main_phrase and main_phrase in name_lower:
            relevance += 3
        if is_new:
            relevance += 1

        results.append(
            {
                "name": name,
                "price": price_value,
                "priceText": price_text or "—",
                "store": store,
                "link": link or url,
                "rating": rating,
                "isNew": is_new,
                "installments": installments,
                "cashback": cashback,
                "shippingText": shipping_text,
                "shippingCost": shipping_cost,
                "freeShip": shipping_cost == 0,
                "_relevance": relevance,
            }
        )

    # Filtro de relevância
    if keywords:
        important = keywords[:2] if len(keywords) >= 2 else keywords
        filtered = [r for r in results if all(k in r["name"].lower() for k in important)]
        if len(filtered) >= 2:
            results = filtered
        else:
            min_rel = max(1, (len(keywords) + 1) // 2)
            results = [r for r in results if r.get("_relevance", 0) >= min_rel]

    results.sort(key=lambda o: (-o.get("_relevance", 0), o["price"]))
    for r in results:
        r.pop("_relevance", None)

    return results[:max_results]




# ================= FONTES DE PRECO =================

def _domain_store(url: str) -> str:
    # Resolve redirects Yahoo/Bing (RU= / u=) para a loja real.
    raw = url or ""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        for key in ("RU", "u", "url", "q"):
            if key in qs and qs[key]:
                cand = qs[key][0]
                if cand.startswith("http"):
                    raw = cand
                    break
                # RU às vezes vem percent-encoded
                decoded = urllib.parse.unquote(cand)
                if decoded.startswith("http"):
                    raw = decoded
                    break
    except Exception:
        pass
    host = urllib.parse.urlparse(raw).netloc.lower().replace("www.", "")
    # Ignora hosts de motores de busca
    if any(x in host for x in ("search.yahoo.", "google.", "bing.", "duckduckgo.")):
        return ""
    mapping = {
        "mercadolivre.com.br": "Mercado Livre",
        "produto.mercadolivre.com.br": "Mercado Livre",
        "amazon.com.br": "Amazon Brasil",
        "magazineluiza.com.br": "Magazine Luiza",
        "magalu.com": "Magazine Luiza",
        "casasbahia.com.br": "Casas Bahia",
        "kabum.com.br": "KaBuM!",
        "shopee.com.br": "Shopee",
        "carrefour.com.br": "Carrefour",
        "extra.com.br": "Extra",
        "pontofrio.com.br": "Ponto",
        "americanas.com.br": "Americanas",
        "fastshop.com.br": "Fast Shop",
        "pichau.com.br": "Pichau",
        "aliexpress.com": "AliExpress",
        "dell.com": "Dell",
        "samsung.com": "Samsung",
        "apple.com": "Apple",
        "lenovo.com": "Lenovo",
        "acer.com": "Acer",
        "asus.com": "ASUS",
        "zoom.com.br": "Zoom",
    }
    for domain, name in mapping.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host



def _is_exact_product_url(store: str, url: str) -> bool:
    """Aceita somente URLs com forte evidência de página individual de produto.

    O comparador pode descobrir URLs por mecanismos de busca, mas uma URL de
    pesquisa/categoria/listagem nunca pode virar o destino do botão de compra.
    Para lojas sem um padrão único confiável, exigimos marcadores de produto em
    vez de aceitar simplesmente "qualquer caminho com duas barras".
    """
    if not url or not store:
        return False
    raw = str(url).strip()
    try:
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return False
        if parsed.username or parsed.password:
            return False
        host = parsed.netloc.lower().split(":", 1)[0].removeprefix("www.")
        path = urllib.parse.unquote(parsed.path or "").lower().rstrip("/")
        query = parsed.query.lower()
    except Exception:
        return False
    if not path or path == "/":
        return False

    blocked_segments = (
        "/busca", "/search", "/lista", "/listas", "/categoria", "/categorias",
        "/departamento", "/departamentos", "/ofertas", "/catalog", "/catalogo",
        "/resultados", "/resultado", "/s", "/searchresult", "/plp",
    )
    if any(path == x or path.startswith(x + "/") for x in blocked_segments):
        return False
    if re.search(r"(?:^|[?&])(q|query|keyword|search|text|term|page|sort|filter)=", query):
        return False

    patterns = {
        "Mercado Livre": (r"/(?:mlb|mla)-?\d+",),
        "Amazon Brasil": (r"/(?:dp|gp/product)/[a-z0-9]{8,}",),
        "Magazine Luiza": (r"/[^/]+/p/[a-z0-9-]+$", r"/produto/[a-z0-9-]+$"),
        "KaBuM!": (r"/produto/\d+/", r"/[a-z0-9-]+-[0-9]+\.html$"),
        "Casas Bahia": (r"/[^/]+/p/[a-z0-9-]+$", r"/produto(?:/|$)[a-z0-9-]+"),
        "Americanas": (r"/produto/[a-z0-9-]+", r"/[^/]+/p/\d+$"),
        "Shopee": (r"/product/\d+/\d+$",),
        "Carrefour": (r"/[^/]+/p/[a-z0-9-]+$", r"/produto/[a-z0-9-]+"),
        "Ponto": (r"/[^/]+/p/[a-z0-9-]+$", r"/produto/[a-z0-9-]+"),
        "Fast Shop": (r"/[^/]+/p/[a-z0-9-]+$", r"/produto/[a-z0-9-]+"),
        "Extra": (r"/[^/]+/p/[a-z0-9-]+$", r"/produto/[a-z0-9-]+"),
        "Ponto": (r"/[^/]+/p/[a-z0-9-]+$", r"/produto/[a-z0-9-]+"),
        "Pichau": (r"/[a-z0-9-]+-[a-z0-9]+$",),
        "AliExpress": (r"/item/\d+\.html$", r"/[^/]+-\d+\.html$"),
        "Dell": (r"/(?:[^/]+/){1,6}(?:pd|spd)/[^/?#]+", r"/(?:[^/]+/){1,6}p/[^/?#]+"),
        "Samsung": (r"/(?:[^/]+/){1,6}p/[a-z0-9-]+", r"/br/(?:[^/]+/)+[a-z0-9-]+/p/[a-z0-9-]+"),
        "Lenovo": (r"/(?:[^/]+/){1,6}p/[a-z0-9-]+", r"/(?:[^/]+/){1,6}(?:item|product)/[^/?#]+"),
        "Acer": (r"/(?:[^/]+/){1,6}(?:product|produto|notebook|laptop)/[^/?#]+",),
        "ASUS": (r"/(?:[^/]+/){1,6}(?:product|produto)/[^/?#]+", r"/(?:[^/]+/){1,6}[a-z0-9-]+\.html$"),
        "Apple": (r"/shop/buy-(?:iphone|ipad|mac|watch|airpods)/", r"/br/shop/buy-(?:iphone|ipad|mac|watch|airpods)/"),
    }
    if store in patterns:
        return any(re.search(pattern, path) for pattern in patterns[store])

    # Para lojas conhecidas sem um padrão único estável, aceitamos apenas
    # caminhos que tenham sinais fortes de página individual. Isso evita
    # descartar produtos reais quando a loja muda pequenos detalhes da URL.
    strong_product_markers = (
        r"/(?:produto|product|item|p|dp|pd|spd)(?:/|$)",
        r"/(?:mlb|mla)-?\d+",
        r"/[^/]+-[0-9]{3,}(?:\.html)?$",
        r"/[0-9]{5,}(?:\.html)?$",
        r"/[^/]+\.html$",
    )
    if any(re.search(pattern, path, re.I) for pattern in strong_product_markers):
        return True
    return False


def _safe_http_url(url: str) -> str:
    """Normaliza URLs externas retornadas por mecanismos de busca."""
    raw = str(url or "").strip()
    try:
        u = urllib.parse.urlparse(raw)
        if u.scheme.lower() not in ("http", "https") or not u.netloc:
            return ""
        if u.username or u.password:
            return ""
        host = u.hostname or ""
        if not host or host.lower() in {"localhost", "localhost.localdomain"}:
            return ""
        return u._replace(fragment="").geturl()
    except Exception:
        return ""


def _safe_fetch_url(url: str) -> str:
    """Evita SSRF quando o agente visita uma página encontrada na web."""
    safe = _safe_http_url(url)
    if not safe:
        return ""
    try:
        host = urllib.parse.urlparse(safe).hostname or ""
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        for info in infos:
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if not ip.is_global:
                return ""
    except Exception:
        # Se não conseguimos resolver com segurança, não fazemos a requisição.
        return ""
    return safe

def _prices_from_text(text: str) -> List[float]:
    vals = []
    for m in re.finditer(r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|[0-9]+(?:,[0-9]{2})?)", text or "", re.I):
        prefix = (text[max(0, m.start()-24):m.start()] or "").lower()
        # Não confundir parcela (ex.: 12x de R$ 477,67) com preço à vista.
        if re.search(r"\d+\s*x\s*de\s*$", prefix):
            continue
        try:
            vals.append(float(m.group(1).replace('.', '').replace(',', '.')))
        except ValueError:
            pass
    return vals


def _score_product_match(query: str, title: str) -> int:
    q = [x.lower() for x in re.findall(r"[a-z0-9]+", query) if len(x) > 2]
    t = (title or '').lower()
    return sum(1 for x in q if x in t)




def search_mercadolivre_api(product: str, max_results: int = 24) -> tuple[List[Dict[str, Any]], List[str]]:
    """Fonte direta e gratuita do Mercado Livre, usada como fallback robusto.

    Ela não depende de Google/Bing/DuckDuckGo/Yahoo e por isso continua funcionando
    quando mecanismos de busca bloqueiam requisições vindas do Render.
    """
    url = "https://api.mercadolibre.com/sites/MLB/search?" + urllib.parse.urlencode({
        "q": product,
        "limit": min(max_results, 50),
        "sort": "relevance",
    })
    try:
        r = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        return [], [f"Mercado Livre API: {exc}"]

    out: List[Dict[str, Any]] = []
    for item in data.get("results", []) or []:
        title = (item.get("title") or "").strip()
        price = item.get("price")
        if not title or not isinstance(price, (int, float)) or price <= 0:
            continue
        permalink = item.get("permalink") or ""
        if not permalink:
            continue
        shipping = item.get("shipping") or {}
        free_ship = bool(shipping.get("free_shipping"))
        condition = (item.get("condition") or "new").lower()
        out.append({
            "name": title,
            "price": float(price),
            "priceText": f"R$ {float(price):,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
            "store": "Mercado Livre",
            "link": permalink,
            "rating": "—",
            "isNew": condition == "new",
            "installments": "",
            "cashback": "",
            "shippingText": "Grátis" if free_ship else "A calcular",
            "shippingCost": 0.0 if free_ship else None,
            "freeShip": free_ship,
            "source": "Mercado Livre API",
            "searchEngines": ["Mercado Livre API"],
            "_relevance": _score_product_match(product, title),
        })
    return out, []



def search_mercadolivre_html(product: str, max_results: int = 18) -> tuple[List[Dict[str, Any]], List[str]]:
    """Fallback sem API: lê a página pública de resultados do Mercado Livre.

    Serve como segunda rota quando a API pública estiver indisponível no Render.
    Só aceita ofertas que tenham preço explícito e URL de produto.
    """
    url = "https://lista.mercadolivre.com.br/" + urllib.parse.quote(product.replace(" ", "-"))
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        return [], [f"Mercado Livre web: {exc}"]

    out: List[Dict[str, Any]] = []
    cards = soup.select("li.ui-search-layout__item") or soup.select("div.ui-search-result__content")
    for card in cards[:max_results]:
        title_el = card.select_one("h2.ui-search-item__title, h2.poly-component__title, a.ui-search-link")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            continue
        link_el = card.select_one("a.ui-search-link, a.poly-component__title")
        link = link_el.get("href", "") if link_el else ""
        if not link:
            continue
        price = None
        meta = card.select_one('[itemprop="price"]')
        if meta and meta.get("content"):
            try:
                price = float(meta.get("content"))
            except (TypeError, ValueError):
                price = None
        if price is None:
            amount = card.select_one(".andes-money-amount")
            text = amount.get_text(" ", strip=True) if amount else card.get_text(" ", strip=True)
            vals = _prices_from_text(text)
            if vals:
                price = min(vals)
        if not price or price <= 0:
            continue
        shipping = card.get_text(" ", strip=True).lower()
        free_ship = "frete grátis" in shipping or "frete gratis" in shipping
        out.append({
            "name": title,
            "price": float(price),
            "priceText": f"R$ {float(price):,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
            "store": "Mercado Livre",
            "link": link,
            "rating": "—",
            "isNew": True,
            "installments": extract_installments(card.get_text(" ", strip=True)),
            "cashback": extract_cashback(card.get_text(" ", strip=True)),
            "shippingText": "Grátis" if free_ship else "A calcular",
            "shippingCost": 0.0 if free_ship else None,
            "freeShip": free_ship,
            "source": "Mercado Livre web",
            "searchEngines": ["Mercado Livre web"],
            "_relevance": _score_product_match(product, title),
        })
    return out, []


def _cache_key(product: str) -> str:
    return re.sub(r"\s+", " ", (product or "").strip().lower())[:200]


def _cache_get(product: str) -> Optional[Dict[str, Any]]:
    key = _cache_key(product)
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        ts, payload = entry
        if time.time() - ts > _CACHE_TTL_SEC:
            _CACHE.pop(key, None)
            return None
        # Cópia rasa para o cliente não mutar o cache.
        return dict(payload)


def _cache_set(product: str, payload: Dict[str, Any]) -> None:
    key = _cache_key(product)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            # Remove as entradas mais antigas.
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: max(1, _CACHE_MAX_ENTRIES // 4)]
            for k, _ in oldest:
                _CACHE.pop(k, None)
        _CACHE[key] = (time.time(), dict(payload))


def _format_brl(price: float) -> str:
    return f"R$ {float(price):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def search_kabum(product: str, max_results: int = 12) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Fonte estruturada KaBuM!: só aceita cards com URL de produto unitária."""
    url = "https://www.kabum.com.br/busca/" + urllib.parse.quote(product)
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        return [], [f"KaBuM!: {exc}"]

    out: List[Dict[str, Any]] = []
    # Cards comuns do KaBuM (classes mudam com frequência; vários seletores).
    cards = (
        soup.select("div.productCard")
        or soup.select("a.productLink")
        or soup.select("[data-testid='product-card']")
        or soup.select("article")
    )
    for card in cards[: max_results * 2]:
        link_el = card if card.name == "a" else card.select_one("a[href]")
        href = (link_el.get("href") if link_el else "") or ""
        if href.startswith("/"):
            href = "https://www.kabum.com.br" + href
        if not href or not _is_exact_product_url("KaBuM!", href):
            continue
        name_el = card.select_one("span.nameCard, h2, h3, .nameCard, [class*='name']")
        name = (name_el.get_text(" ", strip=True) if name_el else "") or card.get_text(" ", strip=True)[:120]
        if not name or len(name) < 4:
            continue
        text = card.get_text(" ", strip=True)
        vals = _prices_from_text(text)
        if not vals:
            continue
        price = min(vals)
        if price <= 0:
            continue
        free_ship = "frete grátis" in text.lower() or "frete gratis" in text.lower()
        out.append({
            "name": name[:180],
            "price": float(price),
            "priceText": _format_brl(price),
            "store": "KaBuM!",
            "link": href.split("?")[0],
            "rating": "—",
            "isNew": True,
            "installments": extract_installments(text),
            "cashback": extract_cashback(text),
            "shippingText": "Grátis" if free_ship else "A calcular",
            "shippingCost": 0.0 if free_ship else None,
            "freeShip": free_ship,
            "source": "KaBuM!",
            "searchEngines": ["KaBuM!"],
            "directStore": True,
            "_relevance": _score_product_match(product, name),
        })
        if len(out) >= max_results:
            break
    return out, []


def search_magalu(product: str, max_results: int = 12) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Fonte Magazine Luiza: apenas URLs de produto unitárias (/p/ ou /produto/)."""
    url = "https://www.magazineluiza.com.br/busca/" + urllib.parse.quote(product) + "/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        return [], [f"Magazine Luiza: {exc}"]

    out: List[Dict[str, Any]] = []
    cards = (
        soup.select("[data-testid='product-card']")
        or soup.select("li[class*='product']")
        or soup.select("div[class*='ProductCard']")
        or soup.select("a[href*='/p/']")
    )
    seen_links: set = set()
    for card in cards[: max_results * 3]:
        link_el = card if card.name == "a" else card.select_one("a[href*='/p/'], a[href*='/produto/']")
        href = (link_el.get("href") if link_el else "") or ""
        if href.startswith("/"):
            href = "https://www.magazineluiza.com.br" + href
        href = href.split("?")[0].rstrip("/")
        if not href or href in seen_links:
            continue
        if not _is_exact_product_url("Magazine Luiza", href):
            continue
        seen_links.add(href)
        name_el = card.select_one("h2, h3, [data-testid='product-title'], [class*='Title'], [class*='title']")
        name = (name_el.get_text(" ", strip=True) if name_el else "") or ""
        if not name:
            # Fallback: último segmento da URL
            slug = href.rstrip("/").split("/")[-2] if "/p/" in href else href.rstrip("/").split("/")[-1]
            name = slug.replace("-", " ").strip()[:120]
        if not name or len(name) < 4:
            continue
        text = card.get_text(" ", strip=True)
        vals = _prices_from_text(text)
        if not vals:
            continue
        price = min(vals)
        if price <= 0:
            continue
        free_ship = "frete grátis" in text.lower() or "frete gratis" in text.lower()
        out.append({
            "name": name[:180],
            "price": float(price),
            "priceText": _format_brl(price),
            "store": "Magazine Luiza",
            "link": href,
            "rating": "—",
            "isNew": True,
            "installments": extract_installments(text),
            "cashback": extract_cashback(text),
            "shippingText": "Grátis" if free_ship else "A calcular",
            "shippingCost": 0.0 if free_ship else None,
            "freeShip": free_ship,
            "source": "Magazine Luiza",
            "searchEngines": ["Magazine Luiza"],
            "directStore": True,
            "_relevance": _score_product_match(product, name),
        })
        if len(out) >= max_results:
            break
    return out, []


def search_multifonte(product: str, max_results: int = 60) -> Dict[str, Any]:
    """Pesquisa multi-fonte com cache, timeout por fonte e só links de produto.

    Regras:
    - Nenhum preço é inventado ou estimado.
    - Só entram ofertas com URL unitária de produto validada.
    - Fontes que falham ou estouram timeout não bloqueiam as demais.
    - Zoom é usado só para descoberta; destino final nunca é o Zoom.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

    product = (product or "").strip()
    if not product:
        return {
            "ok": False, "query": "", "count": 0, "results": [], "stores": [],
            "searchedStores": [], "webResults": [], "searchEngines": [],
            "coverage": {}, "errors": ["Consulta vazia."], "cached": False,
        }

    cached = _cache_get(product)
    if cached is not None:
        cached["cached"] = True
        return cached

    collected: List[Dict[str, Any]] = []
    errors: List[str] = []
    sources_ok: List[str] = []

    def do_ml():
        local_errors: List[str] = []
        try:
            items, errs = search_mercadolivre_api(product, min(max_results, 30))
            local_errors.extend(errs or [])
            if items:
                return items, local_errors
        except Exception as exc:
            local_errors.append(f"Mercado Livre API: {exc}")
        try:
            items, errs = search_mercadolivre_html(product, min(max_results, 18))
            local_errors.extend(errs or [])
            return items, local_errors
        except Exception as exc:
            local_errors.append(f"Mercado Livre web: {exc}")
            return [], local_errors

    def do_zoom():
        try:
            zoom = search_zoom(product, max_results=12)
            if not isinstance(zoom, list):
                msg = zoom.get("error", "Zoom sem resultados") if isinstance(zoom, dict) else "Zoom sem resultados"
                return [], [str(msg)]
            out = []
            for item in zoom:
                item = dict(item)
                item["store"] = item.get("store") or "Zoom"
                item["source"] = "Zoom"
                item["searchEngines"] = ["Zoom"]
                item["_relevance"] = _score_product_match(product, item.get("name", "")) + 2
                if item["_relevance"] > 0 and item.get("price", 0) > 0:
                    out.append(item)
            return out, []
        except Exception as exc:
            return [], [f"Zoom: {exc}"]

    def do_kabum():
        try:
            return search_kabum(product, max_results=10)
        except Exception as exc:
            return [], [f"KaBuM!: {exc}"]

    def do_magalu():
        try:
            return search_magalu(product, max_results=10)
        except Exception as exc:
            return [], [f"Magazine Luiza: {exc}"]

    # Fontes em paralelo. Timeout global 22s; cada fonte tem timeout próprio na request.
    executor = ThreadPoolExecutor(max_workers=4)
    futures = {
        executor.submit(do_ml): "Mercado Livre",
        executor.submit(do_zoom): "Zoom",
        executor.submit(do_kabum): "KaBuM!",
        executor.submit(do_magalu): "Magazine Luiza",
    }
    try:
        for future in as_completed(futures, timeout=28):
            name = futures[future]
            try:
                result = future.result()
                items, errs = result
                if items:
                    collected.extend(items)
                    sources_ok.append(name)
                errors.extend(errs or [])
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    except TimeoutError:
        errors.append("Uma ou mais fontes demoraram demais e foram ignoradas.")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    # Separar Zoom (descoberta) de fontes com link direto de loja.
    direct_collected = [x for x in collected if x.get("source") != "Zoom"]
    zoom_collected = [x for x in collected if x.get("source") == "Zoom"]

    grouped: Dict[tuple, Dict[str, Any]] = {}
    for item in direct_collected:
        normalized = re.sub(r"[^a-z0-9]+", "", item.get("name", "").lower())[:140]
        key = (item.get("store", "").lower(), normalized)
        old = grouped.get(key)
        if old is None or float(item.get("price", float("inf"))) < float(old.get("price", float("inf"))):
            grouped[key] = item
        elif old is not None:
            old["searchEngines"] = sorted(set(old.get("searchEngines", [])) | set(item.get("searchEngines", [])))

    results = _replace_zoom_links_with_store_links(list(grouped.values()) + zoom_collected, product)

    # REGRA DE OURO: só ofertas com URL unitária de produto validada.
    # A validação é rígida contra buscas/categorias, mas não depende de um
    # único formato de URL que possa mudar sem aviso.
    by_link: Dict[str, Dict[str, Any]] = {}
    discarded_no_link = 0
    for item in results:
        link = (item.get("link") or "").strip().rstrip("/")
        store = item.get("store", "")
        if not link or not _is_exact_product_url(store, link):
            discarded_no_link += 1
            continue
        item["directStore"] = True
        item["link"] = link
        old = by_link.get(link)
        if old is None or float(item.get("price", float("inf"))) < float(old.get("price", float("inf"))):
            by_link[link] = item
        elif old is not None:
            old["searchEngines"] = sorted(set(old.get("searchEngines", [])) | set(item.get("searchEngines", [])))

    results = list(by_link.values())
    # Ranking: relevância desc, depois preço asc.
    results.sort(key=lambda x: (-(x.get("_relevance") or 0), float(x.get("price") or 1e18)))
    by_store: Dict[str, bool] = {}
    for item in results:
        by_store[item.get("store") or "Outra"] = True
        item.pop("_relevance", None)

    engines = sorted(set(
        eng
        for item in results
        for eng in (item.get("searchEngines") or [item.get("source") or ""])
        if eng
    )) or sources_ok

    payload = {
        "ok": bool(results),
        "query": product,
        "count": len(results),
        "results": results[:max_results],
        "stores": sorted(by_store.keys()),
        "searchedStores": sorted(set(sources_ok) | set(by_store.keys())) or ["Mercado Livre", "Zoom", "KaBuM!", "Magazine Luiza"],
        "webResults": [],
        "searchEngines": engines,
        "coverage": {
            "engines": len(engines),
            "stores": len(by_store),
            "sourcesOk": sources_ok,
            "zoom": "Zoom" in sources_ok,
            "kabum": "KaBuM!" in sources_ok,
            "magalu": "Magazine Luiza" in sources_ok,
            "broadSearch": False,
            "strategy": "multi_fonte_somente_link_produto",
            "discardedNoValidLink": discarded_no_link,
        },
        "errors": errors[:20],
        "cached": False,
    }
    if results:
        _cache_set(product, payload)
    return payload

def _product_tokens(text: str) -> List[str]:
    return [x for x in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(x) > 2]


def _direct_store_link(store: str, product: str) -> str:
    base = STORE_SEARCH_URLS.get(store)
    if not base:
        return ""
    return base + urllib.parse.quote_plus(product)


def _replace_zoom_links_with_store_links(results: List[Dict[str, Any]], product: str) -> List[Dict[str, Any]]:
    """Converte resultados do Zoom apenas quando há URL unitária comprovada.

    O Zoom pode descobrir a oferta, mas nunca é usado como destino final. Se não
    houver uma página individual da loja correspondente, o resultado é removido
    das ofertas clicáveis em vez de cair numa busca genérica.
    """
    direct = [
        x for x in results
        if x.get("source") != "Zoom"
        and _domain_store(x.get("link", "")) not in ("", "Zoom")
        and _is_exact_product_url(x.get("store", ""), x.get("link", ""))
    ]
    converted: List[Dict[str, Any]] = []
    for item in results:
        if item.get("source") != "Zoom":
            if _is_exact_product_url(item.get("store", ""), item.get("link", "")):
                item["directStore"] = True
                converted.append(item)
            continue

        store = item.get("store", "")
        name = item.get("name", "")
        candidates = [x for x in direct if x.get("store") == store and x.get("link")]
        qtokens = set(_product_tokens(product + " " + name))
        best = None
        best_score = 0
        for candidate in candidates:
            ctokens = set(_product_tokens(candidate.get("name", "")))
            overlap = len(qtokens & ctokens)
            score = overlap
            if candidate.get("price") and item.get("price"):
                ratio = abs(float(candidate["price"]) - float(item["price"])) / max(float(item["price"]), 1)
                if ratio <= 0.05:
                    score += 3
                elif ratio <= 0.15:
                    score += 1
            if score > best_score:
                best_score = score
                best = candidate

        if best and best_score >= max(3, min(5, len(qtokens))):
            item["link"] = best["link"]
            item["directStore"] = True
            item["source"] = f"Zoom → {store}"
            converted.append(item)
        # Sem correspondência unitária, o resultado permanece somente na pesquisa
        # ampla (webResults), não como uma oferta clicável.
    return converted


def escolher_melhor(results: List[Dict]) -> Optional[Dict]:
    """Escolhe a melhor oferta entre as que têm link de produto válido.

    Critérios (menor score vence):
    1. Preço (+ frete conhecido quando disponível)
    2. Preferência forte por produto novo
    3. Frete grátis / frete conhecido
    4. Cashback e parcelas sem juros
    5. Link unitário já validado (pré-requisito)
    """
    valid = [
        o for o in (results or [])
        if o.get("link") and _is_exact_product_url(o.get("store", ""), o.get("link", ""))
    ]
    if not valid:
        return None

    def score(o: Dict) -> float:
        s = float(o.get("price") or 0)
        if not o.get("isNew", True):
            s += 80
        if o.get("shippingCost") is None:
            s += 8
        if o.get("shippingCost") == 0:
            s -= 5
        if o.get("cashback"):
            s -= 3
        inst = (o.get("installments") or "").lower()
        if "sem juros" in inst:
            s -= 3
        # Pequeno bônus para lojas com link direto confirmado.
        if o.get("directStore"):
            s -= 1
        return s

    return min(valid, key=score)



def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def infer_intent(question: str) -> Dict[str, Any]:
    q = _norm(question).lower()
    shopping = bool(re.search(
        r"\b(pre[cç]o|comprar|compra|oferta|promo[cç][aã]o|barato|mais barato|quanto custa|onde comprar|"
        r"frete|parcel|produto|celular|iphone|samsung|notebook|fone|tv|geladeira|air fryer|"
        r"custo.?benef[ií]cio|vale a pena|desconto|loja|magalu|amazon|mercado\s*livre)\b",
        q,
    ))
    current = bool(re.search(r"\b(hoje|agora|atual|atualmente|esta semana|2026|2025|not[ií]cia|lan[cç]amento|vers[aã]o nova)\b", q))
    howto = bool(re.search(r"\b(como|passo a passo|ensine|tutorial|configurar|instalar|resolver|consertar)\b", q))
    compare = bool(re.search(r"\b(compar|diferen[cç]a|melhor|qual escolher|qual vale|versus|vs\.?|entre|custo.?benef)\b", q))
    return {"shopping": shopping, "current": current, "howto": howto, "compare": compare}


def build_plan(question: str) -> List[Dict[str, Any]]:
    q = _norm(question)
    return [{"tool": "price_search", "query": q, "reason": "consultar e comparar preços reais"}]


def synthesize_agent(question: str, prices: List[Dict[str, Any]], intent: Dict[str, Any]) -> Dict[str, Any]:
    if prices:
        ordered = sorted(prices, key=lambda x: x.get("price", float("inf")))
        best = ordered[0]
        parts = [f"Encontrei {len(prices)} ofertas reais. A menor encontrada é {best.get('priceText','—')} em {best.get('store','—')}."]
        if intent.get("compare") and len(ordered) > 1:
            parts.append("Outras opções: " + "; ".join(f"{x.get('store','—')} — {x.get('priceText','—')}" for x in ordered[1:4]) + ".")
        return {"answer":"<br><br>".join(parts),"sources":[],"confidence":"alta" if len(prices)>=3 else "média"}
    return {"answer":"Não encontrei ofertas com preço estruturado nas fontes disponíveis agora. Tente um nome mais específico do produto. Nenhum preço foi inventado ou estimado.","sources":[],"confidence":"baixa"}


def run_agent(question: str) -> Dict[str, Any]:
    question = _norm(question)
    if not question:
        return {"ok":False,"answer":"Faça uma pergunta para eu começar.","plan":[],"sources":[]}
    intent=infer_intent(question)
    plan=build_plan(question)
    errors=[]
    try:
        stop={"qual","quais","melhor","melhores","mais","menos","barato","barata","bom","boa","para","com","sem","que","voce","você","recomenda","comprar","compra","preco","preço","hoje","agora","vale","pena","custo","beneficio","benefício","comparar","comparacao","comparação","entre","versus","vs","onde","quanto","custa","escolher","opcao","opção"}
        tokens=[t for t in re.findall(r"[\wÀ-ÿ0-9]+",question.lower()) if t not in stop and len(t)>1]
        product_query=" ".join(tokens[:8]) if tokens else question
        data=search_multifonte(product_query,max_results=24)
        prices=data.get("results",[]) if isinstance(data,dict) else []
        if isinstance(data,dict): errors.extend(data.get("errors",[]))
    except Exception as exc:
        prices=[]; errors.append(str(exc))
    result=synthesize_agent(question,prices,intent)
    return {"ok":bool(prices),"question":question,"intent":intent,"plan":plan,"sources":[],"prices":prices[:8],"answer":result["answer"],"confidence":result["confidence"],"errors":errors[:6]}


def _admin_authorized(handler: BaseHTTPRequestHandler) -> bool:
    """Protege operações que gravam estado ou criam checkpoints.

    Em produção, defina ADMIN_API_TOKEN no Render e envie o mesmo valor em
    ``X-Admin-Token``. Sem token configurado, operações administrativas ficam
    desativadas por segurança, mas leitura/consulta do aplicativo continua normal.
    """
    if not ADMIN_API_TOKEN:
        return False
    supplied = handler.headers.get("X-Admin-Token", "").strip()
    return bool(supplied) and supplied == ADMIN_API_TOKEN


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[Economiza AI] {args[0]}")

    def _cors(self):
        # O painel usa a mesma origem. Só habilitamos CORS explícito quando
        # ALLOWED_ORIGIN foi configurado no ambiente do Render.
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > MAX_LEARN_BODY:
            self._json(413, {"ok": False, "error": "Payload administrativo muito grande."})
            return
        body = self.rfile.read(length) if length else b""

        if path == "/api/learn":
            if not _admin_authorized(self):
                self._json(403, {"ok": False, "error": "Endpoint administrativo protegido."})
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                self._json(200, learn(payload))
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # API de busca
        if path == "/api/search":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                self._json(400, {"error": "Parâmetro q obrigatório", "results": []})
                return
            if len(q) > MAX_QUERY_LEN:
                self._json(413, {"error": f"Consulta muito longa. Limite: {MAX_QUERY_LEN} caracteres.", "results": []})
                return
            print(f"Buscando: {q}")
            try:
                data = search_multifonte(q, max_results=36)
                results = data.get("results", [])
                # Garante no endpoint que só saem ofertas com link de produto.
                results = [
                    r for r in results
                    if r.get("link") and _is_exact_product_url(r.get("store", ""), r.get("link", ""))
                ]
                best = escolher_melhor(results)
                stores = sorted({r.get("store") or "Outra" for r in results})
                engines = data.get("searchEngines") or []
                coverage_label = " + ".join(engines) if engines else "fontes estruturadas"
                self._json(200, {
                    "ok": bool(results),
                    "query": q,
                    "count": len(results),
                    "results": results,
                    "best": best,
                    "source": "multifonte",
                    "searchCoverage": coverage_label,
                    "stores": stores,
                    "coverage": data.get("coverage", {}),
                    "webResults": [],
                    "errors": data.get("errors", []),
                    "cached": bool(data.get("cached")),
                })
            except Exception as exc:
                # Nunca deixa uma exceção de uma fonte externa virar 502 do Render.
                print(f"Erro /api/search: {exc}")
                self._json(200, {
                    "ok": False,
                    "query": q,
                    "count": 0,
                    "results": [],
                    "best": None,
                    "source": "multifonte",
                    "error": "As fontes de preços não responderam agora.",
                    "errors": [str(exc)],
                    "cached": False,
                })
            return

        # Pesquisa web geral desativada por design.
        if path == "/api/web-search":
            self._json(410, {"ok": False, "error": "Pesquisa web geral desativada. Use /api/search para comparar preços."})
            return

        # Agente autônomo: planeja, pesquisa, cruza fontes, consulta preços e sintetiza.
        if path == "/api/agent":
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                self._json(400, {"ok": False, "error": "Parâmetro q obrigatório"})
                return
            if len(q) > MAX_QUERY_LEN:
                self._json(413, {"ok": False, "error": f"Consulta muito longa. Limite: {MAX_QUERY_LEN} caracteres."})
                return
            print(f"Agente: {q}")
            data = run_agent(q)
            if data.get("ok"):
                update_metric("agent_success")
            else:
                update_metric("agent_errors")
            self._json(200 if data.get("ok") else 502, data)
            return

        # Autoaprendizado controlado. Status é leitura; gravações/propostas
        # administrativas exigem token e nunca ficam abertas publicamente.
        if path == "/api/learn":
            self._json(405, {"ok": False, "error": "Use POST autenticado para este endpoint."})
            return

        if path == "/api/self-improve":
            action = (qs.get("action") or ["status"])[0].lower()
            if action not in ("status", "checkpoint", "propose"):
                self._json(400, {"ok": False, "error": "Ação inválida."})
                return
            if not _admin_authorized(self):
                self._json(403, {"ok": False, "error": "Endpoint administrativo protegido."})
                return
            if action == "checkpoint":
                self._json(200, snapshot("auto-checkpoint"))
            elif action == "propose":
                self._json(200, propose_improvements())
            else:
                self._json(200, get_status())
            return

        if path == "/api/health":
            learning_health = get_status().get("health", {})
            required_assets = all((DIR / name).is_file() for name in (
                "economiza_ai_painel.html", "bg_app.png", "loading_melhor_oferta.png"
            ))
            overall = bool(learning_health.get("ok")) and required_assets
            self._json(200, {
                "ok": overall,
                "service": "Economiza AI",
                "version": APP_VERSION,
                "port": PORT,
                "web_search": False,
                "price_search": True,
                "image_ocr": False,
                "learning": {"ok": bool(learning_health.get("ok"))},
                "assets": {"ok": required_assets},
            })
            return

        # Arquivos públicos: whitelist explícita. Nunca servimos código-fonte,
        # bytecode, .env, ai_state, diretórios internos ou qualquer arquivo
        # arbitrário existente dentro do diretório do aplicativo.
        if path == "/" or path == "":
            path = "/economiza_ai_painel.html"

        PUBLIC_FILES = {
            "/economiza_ai_painel.html": "text/html; charset=utf-8",
            "/bg_app.png": "image/png",
            "/loading_melhor_oferta.png": "image/png",
            "/logo_economizaAI.png": "image/png",
            "/logo_melhor_oferta.png": "image/png",
            "/robo_economiza_ai.png": "image/png",
        }
        ctype = PUBLIC_FILES.get(path)
        if not ctype:
            self.send_response(404)
            self.end_headers()
            return

        file_path = DIR / path.lstrip("/")
        if not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Economiza-Version", APP_VERSION)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self._cors()
        self.end_headers()
        self.wfile.write(body)


def main():
    # Injeta no HTML a URL da API real (mesma origem)
    html_path = DIR / "economiza_ai_painel.html"
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")
        if "USE_REAL_API" not in html:
            # marca será usada no JS
            pass

    global PORT
    requested = PORT
    last_error = None
    for candidate in range(requested, requested + 20):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", candidate), Handler)
            PORT = candidate
            break
        except OSError as exc:
            last_error = exc
    else:
        raise RuntimeError(f"Não foi possível abrir uma porta entre {requested} e {requested + 19}: {last_error}")

    print("=" * 50)
    print(f"  Economiza AI — Servidor de preços REAIS ({APP_VERSION})")
    print("=" * 50)
    print(f"  Abra no navegador:")
    print(f"  → http://127.0.0.1:{PORT}/")
    print()
    print("  Fontes: ML API + KaBuM! + Magalu + Zoom (descoberta)")
    print("  Regra: só links de produto unitários (Ver oferta → loja)")
    print("  API: http://127.0.0.1:{}/api/search?q=produto".format(PORT))
    print("  Ctrl+C para parar")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
        server.server_close()


if __name__ == "__main__":
    main()
