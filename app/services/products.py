"""아이템 찾기 (쇼핑몰): 네이버 쇼핑·알리익스프레스·이베이(유럽)에서 분야별 상품을 모읍니다.

유튜브 인기 쇼츠와 같은 분야(trends.CATEGORIES)를 쓰고, 정해진 주기로 한 번 모아 JSON 파일에 저장해 둡니다.
키가 없는 쇼핑몰은 건너뜁니다. 모두 공식 API만 씁니다 (화면을 긁어 오지 않음).

- 네이버: 쇼핑 검색 API(분야 검색어별 상위 상품) + 데이터랩 쇼핑인사이트(분야 클릭 추이)
- 알리익스프레스: 제휴(Affiliate) API의 상품 검색을 최근 판매량 순으로. 공식 상품 영상·사진 주소가 함께 옵니다.
- 이베이: Browse API로 독일·영국 마켓의 상품 검색 (판매량 정보는 없음)
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from html import unescape
from pathlib import Path

import httpx

from ..config import Settings
from .trends import CATEGORIES

logger = logging.getLogger(__name__)

SOURCES: dict[str, str] = {
    "naver": "네이버 쇼핑",
    "aliexpress": "알리익스프레스",
    "ebay": "이베이 (유럽)",
}

# 분야별 검색어. 네이버는 한국어, 알리·이베이는 영어로 찾습니다 (해외 쇼핑몰은 한국어 검색 결과가 빈약함).
NAVER_QUERIES: dict[str, tuple[str, ...]] = {
    "living": ("살림템", "인테리어 소품"),
    "kitchen": ("주방용품", "주방 아이디어상품"),
    "beauty": ("뷰티 디바이스", "화장품 세트"),
    "digital": ("전자기기 액세서리", "스마트 가전"),
    "fashion": ("패션잡화", "가방"),
    "baby": ("육아용품", "아기용품"),
    "pet": ("강아지 용품", "고양이 용품"),
    "camping": ("캠핑용품", "캠핑 장비"),
}
GLOBAL_QUERIES: dict[str, tuple[str, ...]] = {
    "living": ("home gadgets", "home decor"),
    "kitchen": ("kitchen gadgets", "kitchen tools"),
    "beauty": ("beauty tools", "skin care device"),
    "digital": ("phone accessories", "smart gadgets"),
    "fashion": ("fashion accessories", "women bags"),
    "baby": ("baby products", "baby feeding"),
    "pet": ("dog supplies", "cat supplies"),
    "camping": ("camping gear", "camping gadgets"),
}
# 네이버 쇼핑 대분류 코드 (데이터랩 쇼핑인사이트용)
NAVER_CATEGORY_CODES: dict[str, str] = {
    "living": "50000008",   # 생활/건강
    "kitchen": "50000008",  # 생활/건강 > 주방용품
    "beauty": "50000002",   # 화장품/미용
    "digital": "50000003",  # 디지털/가전
    "fashion": "50000001",  # 패션잡화
    "baby": "50000005",     # 출산/육아
    "pet": "50000008",      # 생활/건강 > 반려동물
    "camping": "50000007",  # 스포츠/레저
}
EBAY_MARKETS: dict[str, str] = {"EBAY_DE": "독일", "EBAY_GB": "영국"}

PRODUCT_SORTS: dict[str, str] = {
    "sales": "판매량순",
    "price_asc": "낮은 가격순",
    "price_desc": "높은 가격순",
}

NAVER_SEARCH_URL = "https://openapi.naver.com/v1/search/shop.json"
NAVER_DATALAB_URL = "https://openapi.naver.com/v1/datalab/shopping/categories"
ALIEXPRESS_URL = "https://api-sg.aliexpress.com/sync"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

PER_QUERY = 20


class ProductSourceError(RuntimeError):
    pass


@dataclass
class Product:
    source: str
    product_id: str
    title: str
    category: str
    url: str
    image_url: str = ""
    price: float | None = None
    currency: str = ""
    mall: str = ""
    sales: int | None = None  # 최근 판매량 (알리익스프레스만)
    rating: float | None = None  # 긍정 평가 비율 % (알리익스프레스만)
    images: list[str] = field(default_factory=list)  # 추가 사진
    video_url: str = ""  # 공식 상품 영상 (알리익스프레스만, 있을 때)
    market: str = ""  # 이베이 마켓 (독일/영국)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.product_id}"


def _float(value: object) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _strip_tags(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _https(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith(("https://", "http://")) else ""


def _error(source: str, response: httpx.Response) -> ProductSourceError:
    detail = ""
    try:
        body = response.json()
        detail = (body.get("errorMessage") or body.get("message") or body.get("error_description")
                  or (body.get("errors") or [{}])[0].get("message", "") or "")
    except (ValueError, AttributeError, IndexError):
        pass
    hints = {401: "키가 틀렸거나 이 API 사용 권한이 없습니다", 403: "이 API 사용 권한이 없습니다",
             429: "호출 한도를 넘었습니다. 잠시 뒤 다시 하세요"}
    message = hints.get(response.status_code, f"HTTP {response.status_code}")
    return ProductSourceError(f"{SOURCES[source]}: {message}" + (f" ({detail})" if detail else ""))


# --------------------------------------------------------------------------- 네이버
class NaverShopping:
    name = "naver"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._s = settings
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._s.naver_client_id and self._s.naver_client_secret)

    def _headers(self) -> dict:
        return {"X-Naver-Client-Id": self._s.naver_client_id, "X-Naver-Client-Secret": self._s.naver_client_secret}

    async def collect(self, categories: list[str]) -> list[Product]:
        found: list[Product] = []
        async with httpx.AsyncClient(transport=self._transport, timeout=20) as c:
            for cat in categories:
                for query in NAVER_QUERIES.get(cat, ()):
                    r = await c.get(NAVER_SEARCH_URL, headers=self._headers(),
                                    params={"query": query, "display": PER_QUERY, "sort": "sim"})
                    if r.status_code != 200:
                        raise _error(self.name, r)
                    for item in r.json().get("items") or []:
                        pid = str(item.get("productId") or item.get("link") or "")
                        if not pid:
                            continue
                        found.append(Product(
                            source=self.name, product_id=pid, title=_strip_tags(item.get("title", "")),
                            category=cat, url=_https(item.get("link", "")), image_url=_https(item.get("image", "")),
                            price=_float(item.get("lprice")), currency="KRW", mall=item.get("mallName", ""),
                        ))
        return found

    async def probe(self) -> None:
        """설정 점검용: 상품 1개만 검색해 봅니다."""
        async with httpx.AsyncClient(transport=self._transport, timeout=20) as c:
            r = await c.get(NAVER_SEARCH_URL, headers=self._headers(), params={"query": "캠핑", "display": 1})
        if r.status_code != 200:
            raise _error(self.name, r)

    async def category_trends(self, categories: list[str], today: date | None = None) -> dict[str, float]:
        """분야 → 최근 4주 쇼핑 클릭이 그 전 4주보다 몇 % 늘었는지 (데이터랩 쇼핑인사이트).
        데이터랩 권한이 없으면 빈 결과를 돌려줍니다 (상품 검색은 그대로 씀)."""
        end = (today or datetime.now(UTC).date()) - timedelta(days=1)
        start = end - timedelta(weeks=8) + timedelta(days=1)
        codes = sorted({NAVER_CATEGORY_CODES[c] for c in categories if c in NAVER_CATEGORY_CODES})
        change: dict[str, float] = {}
        async with httpx.AsyncClient(transport=self._transport, timeout=20) as c:
            for i in range(0, len(codes), 3):  # 한 번에 분야 3개까지
                batch = codes[i:i + 3]
                r = await c.post(NAVER_DATALAB_URL, headers=self._headers(), json={
                    "startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "week",
                    "category": [{"name": code, "param": [code]} for code in batch],
                })
                if r.status_code != 200:
                    logger.info("네이버 데이터랩 건너뜀: HTTP %s", r.status_code)
                    return {}
                for result in r.json().get("results") or []:
                    ratios = [_float(d.get("ratio")) or 0.0 for d in result.get("data") or []]
                    if len(ratios) < 8:
                        continue
                    before, recent = sum(ratios[-8:-4]), sum(ratios[-4:])
                    if before > 0:
                        change[result.get("title", "")] = round((recent - before) / before * 100, 1)
        return {cat: change[code] for cat in categories
                if (code := NAVER_CATEGORY_CODES.get(cat)) in change}


# --------------------------------------------------------------------------- 알리익스프레스
def aliexpress_sign(params: dict[str, str], secret: str) -> str:
    """AliExpress Open Platform 서명 (sign_method=sha256): 키 이름순으로 '키값'을 이어 붙여 HMAC-SHA256."""
    message = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


class AliExpress:
    name = "aliexpress"
    METHOD = "aliexpress.affiliate.product.query"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._s = settings
        self._transport = transport
        self._clock = clock

    @property
    def configured(self) -> bool:
        return bool(self._s.aliexpress_app_key and self._s.aliexpress_app_secret)

    def signed_params(self, business: dict[str, str]) -> dict[str, str]:
        params = {
            "app_key": self._s.aliexpress_app_key,
            "method": self.METHOD,
            "sign_method": "sha256",
            "timestamp": str(int(self._clock() * 1000)),
            **business,
        }
        params["sign"] = aliexpress_sign(params, self._s.aliexpress_app_secret)
        return params

    async def probe(self) -> None:
        """설정 점검용: 상품 1개만 찾아 봅니다."""
        async with httpx.AsyncClient(transport=self._transport, timeout=30) as c:
            r = await c.post(ALIEXPRESS_URL, data=self.signed_params({"keywords": "camping", "page_size": "1"}))
        if r.status_code != 200:
            raise _error(self.name, r)
        self._parse(r.json(), "camping")

    async def collect(self, categories: list[str]) -> list[Product]:
        found: list[Product] = []
        async with httpx.AsyncClient(transport=self._transport, timeout=30) as c:
            for cat in categories:
                for query in GLOBAL_QUERIES.get(cat, ()):
                    business = {
                        "keywords": query, "page_no": "1", "page_size": str(PER_QUERY),
                        "sort": "LAST_VOLUME_DESC", "target_currency": "KRW", "target_language": "KO",
                        "ship_to_country": "KR",
                    }
                    if self._s.aliexpress_tracking_id:
                        business["tracking_id"] = self._s.aliexpress_tracking_id
                    r = await c.post(ALIEXPRESS_URL, data=self.signed_params(business))
                    if r.status_code != 200:
                        raise _error(self.name, r)
                    found.extend(self._parse(r.json(), cat))
        return found

    def _parse(self, body: dict, cat: str) -> list[Product]:
        if "error_response" in body:
            err = body["error_response"]
            raise ProductSourceError(f"{SOURCES[self.name]}: {err.get('msg') or err.get('code') or '오류'}")
        resp = (body.get("aliexpress_affiliate_product_query_response") or {}).get("resp_result") or {}
        if resp.get("resp_code") not in (None, 200, "200"):
            raise ProductSourceError(f"{SOURCES[self.name]}: {resp.get('resp_msg') or resp.get('resp_code')}")
        products = (((resp.get("result") or {}).get("products") or {}).get("product")) or []
        result = []
        for p in products:
            pid = str(p.get("product_id") or "")
            if not pid:
                continue
            small = (p.get("product_small_image_urls") or {}).get("string") or []
            rating = _float(str(p.get("evaluate_rate") or "").rstrip("%"))
            result.append(Product(
                source=self.name, product_id=pid, title=(p.get("product_title") or "").strip(), category=cat,
                url=_https(p.get("promotion_link") or p.get("product_detail_url") or ""),
                image_url=_https(p.get("product_main_image_url") or ""),
                price=_float(p.get("target_sale_price") or p.get("sale_price")),
                currency=p.get("target_sale_price_currency") or p.get("sale_price_currency") or "",
                mall=p.get("shop_name") or "", sales=_int(p.get("lastest_volume")), rating=rating,
                images=[u for u in (_https(s) for s in small) if u][:8],
                video_url=_https(p.get("product_video_url") or ""),
            ))
        return result


# --------------------------------------------------------------------------- 이베이
class Ebay:
    name = "ebay"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._s = settings
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._s.ebay_client_id and self._s.ebay_client_secret)

    async def _token(self, c: httpx.AsyncClient) -> str:
        basic = base64.b64encode(f"{self._s.ebay_client_id}:{self._s.ebay_client_secret}".encode()).decode()
        r = await c.post(EBAY_TOKEN_URL, headers={"Authorization": f"Basic {basic}"}, data={
            "grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope",
        })
        if r.status_code != 200:
            raise _error(self.name, r)
        return r.json()["access_token"]

    async def probe(self) -> None:
        """설정 점검용: 앱 토큰만 받아 봅니다."""
        async with httpx.AsyncClient(transport=self._transport, timeout=30) as c:
            await self._token(c)

    async def collect(self, categories: list[str]) -> list[Product]:
        found: list[Product] = []
        async with httpx.AsyncClient(transport=self._transport, timeout=30) as c:
            token = await self._token(c)
            for cat in categories:
                query = GLOBAL_QUERIES.get(cat, ("",))[0]
                for market, market_label in EBAY_MARKETS.items():
                    r = await c.get(EBAY_SEARCH_URL, params={"q": query, "limit": PER_QUERY}, headers={
                        "Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": market,
                    })
                    if r.status_code != 200:
                        raise _error(self.name, r)
                    for item in r.json().get("itemSummaries") or []:
                        pid = str(item.get("itemId") or "")
                        if not pid:
                            continue
                        price = item.get("price") or {}
                        found.append(Product(
                            source=self.name, product_id=pid, title=(item.get("title") or "").strip(), category=cat,
                            url=_https(item.get("itemWebUrl") or ""),
                            image_url=_https((item.get("image") or {}).get("imageUrl") or ""),
                            price=_float(price.get("value")), currency=price.get("currency") or "",
                            mall=(item.get("seller") or {}).get("username") or "", market=market_label,
                            images=[u for u in (_https((i or {}).get("imageUrl", ""))
                                                for i in item.get("additionalImages") or []) if u][:8],
                        ))
        return found


# --------------------------------------------------------------------------- 수집·저장
class ProductCollector:
    """키가 있는 쇼핑몰에서 분야별 상품을 모아 `path`(JSON)에 저장합니다. 한 번에 하나만 실행."""

    def __init__(self, settings: Settings, path: Path, sources: list | None = None) -> None:
        self._s = settings
        self.path = path
        self.sources = sources if sources is not None else [NaverShopping(settings), AliExpress(settings), Ebay(settings)]
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return any(s.configured for s in self.sources)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    async def refresh(self) -> int:
        if self._lock.locked():
            return 0
        async with self._lock:
            categories = list(CATEGORIES)
            items: dict[str, Product] = {}
            errors: dict[str, str] = {}
            trends: dict[str, float] = {}
            for source in self.sources:
                if not source.configured:
                    continue
                try:
                    for p in await source.collect(categories):
                        items.setdefault(p.key, p)
                    if isinstance(source, NaverShopping):
                        trends = await source.category_trends(categories)
                except Exception as exc:
                    errors[source.name] = str(exc) if isinstance(exc, ProductSourceError) else f"{SOURCES[source.name]}: {exc}"
                    logger.warning("상품 수집 실패 (%s): %s", source.name, exc)
            previous = self.load()
            # 실패한 쇼핑몰은 지난번에 모은 상품을 그대로 둡니다.
            kept = [p for p in previous.get("items", []) if p.get("source") in errors]
            data = {
                "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "items": [asdict(p) for p in items.values()] + kept,
                "category_trends": trends or previous.get("category_trends", {}),
                "errors": errors,
            }
            await asyncio.to_thread(self._save, data)
            return len(items)

    async def loop(self, interval_sec: float) -> None:
        while True:
            data = self.load()
            last = data.get("collected_at")
            due = not last or (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() >= interval_sec
            if self.configured and due:
                await self.refresh()
            await asyncio.sleep(min(interval_sec, 3600))


def query_products(data: dict, *, source: str = "all", category: str = "all", sort: str = "sales",
                   q: str = "", limit: int = 120) -> list[dict]:
    items = data.get("items") or []
    words = q.lower().split()
    picked = [
        p for p in items
        if (source == "all" or p.get("source") == source)
        and (category == "all" or p.get("category") == category)
        and all(w in (p.get("title") or "").lower() or w in (p.get("mall") or "").lower() for w in words)
    ]
    if sort == "sales":
        picked.sort(key=lambda p: (p.get("sales") is None, -(p.get("sales") or 0)))
    elif sort in ("price_asc", "price_desc"):
        picked.sort(key=lambda p: (p.get("price") is None, (p.get("price") or 0) * (1 if sort == "price_asc" else -1)))
    return [{**p, "source_label": SOURCES.get(p.get("source", ""), p.get("source")),
             "category_label": CATEGORIES.get(p.get("category", ""), (p.get("category"),))[0]}
            for p in picked[:limit]]
