# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "httpx>=0.27"]
# ///
"""
خادم MCP محلي يغلّف Thaqalayn API (https://www.thaqalayn-api.net)
يتيح لـ Claude البحث النصي في أحاديث موقع thaqalayn.net (الكافي، عيون أخبار الرضا،
الأمالي، الخصال، وغيرها) بالعربية والإنجليزية، وجلب الحديث برقمه، وتصفح الكتاب.

التشغيل:
    uv run thaqalayn_mcp.py            # عبر stdio (Claude Desktop / Claude Code)
    uv run thaqalayn_mcp.py --http     # خادم HTTP على المنفذ 8000 أو $PORT (المسار /mcp)
    (على الاستضافة يكفي وجود متغير البيئة PORT ليعمل وضع HTTP تلقائياً)

ملاحظة: الـ API عام بلا مفتاح، لكنه محدود بـ 30 طلباً في الدقيقة لكل عنوان IP.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

BASE = "https://www.thaqalayn-api.net/api"
UA = "thaqalayn-mcp/1.0 (local MCP wrapper)"

HTTP_MODE = "--http" in sys.argv or bool(os.environ.get("PORT"))
_http_kwargs = dict(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), stateless_http=True) if HTTP_MODE else {}

mcp = FastMCP(
    "thaqalayn",
    **_http_kwargs,
    instructions=(
        "أدوات للبحث في أحاديث thaqalayn.net (كتب الحديث الإمامية). "
        "ابدأ بـ list_books لمعرفة معرّفات الكتب (bookId)، ثم search_hadith للبحث النصي، "
        "ثم get_hadith لجلب النص الكامل. استشهد دائماً باسم الكتاب ورقم الحديث ورابط URL المعاد. "
        "البحث يطابق صيغ الكلمات لا أجزاءها؛ جرّب صيغاً متعددة مفصولة بـ | عند الحاجة. "
        "الأرقام هي أرقام الأحاديث في thaqalayn.net وليست أرقام صفحات طبعة ورقية."
    ),
)

_client: httpx.AsyncClient | None = None
_books_cache: tuple[float, list[dict]] | None = None
_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE, timeout=30.0, headers={"User-Agent": UA, "Accept": "application/json"})
    return _client


async def _get(path: str, params: dict | None = None) -> Any:
    """طلب GET مع إعادة محاولة واحدة عند تجاوز حد الطلبات (429)."""
    c = _client_get()
    for attempt in range(2):
        r = await c.get(path, params={k: v for k, v in (params or {}).items() if v is not None})
        if r.status_code == 429 and attempt == 0:
            wait = int(r.headers.get("Retry-After", "10") or 10)
            await asyncio.sleep(min(wait, 30))
            continue
        if r.status_code == 404:
            raise ValueError(f"غير موجود (404): {path}")
        if r.status_code == 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise ValueError(f"مدخلات غير صالحة (400): {body}")
        r.raise_for_status()
        return r.json()
    raise RuntimeError("تجاوزت حد الطلبات (30/دقيقة). انتظر دقيقة ثم أعد المحاولة.")


def _strip_tashkeel(s: str) -> str:
    return _TASHKEEL.sub("", s)


def _grades(h: dict) -> str:
    g = []
    for key, label in (("majlisiGrading", "المجلسي"), ("behdudiGrading", "البهبودي"), ("mohseniGrading", "المحسني")):
        if h.get(key):
            g.append(f"{label}: {h[key]}")
    return "؛ ".join(g)


def _clip(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.strip()
    return s if n <= 0 or len(s) <= n else s[:n].rstrip() + " …"


def _fmt_hadith(h: dict, max_chars: int, include_english: bool) -> str:
    lines = [f"### {h.get('book') or h.get('bookId')} — رقم {h.get('id')}"]
    meta = []
    if h.get("bookId"):
        meta.append(f"bookId: {h['bookId']}")
    if h.get("volume"):
        meta.append(f"الجزء: {h['volume']}")
    if h.get("category"):
        meta.append(f"الكتاب/القسم: {h['category']}")
    if h.get("chapter"):
        meta.append(f"الباب: {h['chapter']}")
    if h.get("author"):
        meta.append(f"المؤلف: {h['author']}")
    if meta:
        lines.append(" | ".join(meta))
    if h.get("score") is not None:
        lines.append(f"درجة الصلة: {round(float(h['score']), 4)}")
    gr = _grades(h)
    if gr:
        lines.append(f"التقييم: {gr}")
    hl = h.get("highlight") or {}
    if hl.get("arabic"):
        lines.append(f"موضع المطابقة: {hl['arabic']}")
    elif hl.get("english"):
        lines.append(f"Match: {hl['english']}")
    if h.get("thaqalaynSanad"):
        lines.append(f"السند: {_clip(h['thaqalaynSanad'], max_chars)}")
    if h.get("thaqalaynMatn"):
        lines.append(f"المتن: {_clip(h['thaqalaynMatn'], max_chars)}")
    elif h.get("arabicText"):
        lines.append(f"النص: {_clip(h['arabicText'], max_chars)}")
    if include_english and h.get("englishText"):
        lines.append(f"English: {_clip(h['englishText'], max_chars)}")
    if h.get("URL"):
        lines.append(f"الرابط: {h['URL']}")
    return "\n".join(lines)


def _fmt_page(data: dict, max_chars: int, include_english: bool) -> str:
    rows = data.get("data", data if isinstance(data, list) else [])
    pg = data.get("pagination", {}) if isinstance(data, dict) else {}
    head = ""
    if pg:
        head = (f"النتائج: {pg.get('total')} (صفحة {pg.get('page')} من {pg.get('totalPages')}"
                f"{'، توجد صفحة تالية' if pg.get('hasNext') else ''})\n\n")
    if not rows:
        return head + "لا نتائج."
    return head + "\n\n".join(_fmt_hadith(h, max_chars, include_english) for h in rows)


async def _books() -> list[dict]:
    global _books_cache
    if _books_cache and time.time() - _books_cache[0] < 3600:
        return _books_cache[1]
    data = await _get("/v2/allbooks")
    _books_cache = (time.time(), data)
    return data


@mcp.tool()
async def list_books(filter: str | None = None) -> str:
    """قائمة الكتب المتاحة ومعرّفاتها (bookId) ونطاق أرقام أحاديثها.

    filter: نص اختياري لتصفية القائمة (مثل "Kafi" أو "Saduq" أو "Amali").
    """
    books = await _books()
    out = []
    for b in books:
        line = (f"- {b.get('bookId')} | {b.get('BookName') or ''} | {b.get('englishName') or ''} | "
                f"{b.get('author') or ''} | الأرقام {b.get('idRangeMin')}–{b.get('idRangeMax')}")
        if not filter or filter.lower() in line.lower():
            out.append(line)
    return f"عدد الكتب: {len(out)}\n" + "\n".join(out) if out else "لا كتب مطابقة."


@mcp.tool()
async def search_hadith(
    query: str,
    book_id: str | None = None,
    lang: Literal["ar", "en", "any"] = "any",
    page: int = 1,
    limit: int = 20,
    strip_diacritics: bool = True,
    include_english: bool = False,
    max_chars: int = 1200,
) -> str:
    """بحث نصي كامل في الأحاديث (PostgreSQL full-text) مرتّب بالصلة.

    query: كلمات البحث. افصل بدائل متعددة بـ | (منطق OR)، مثل: "الأهواز|خوزستان".
           المطابقة على صيغ الكلمات لا على أجزائها.
    book_id: لتقييد البحث بكتاب واحد (من list_books).
    lang: ar للعربي فقط، en للإنجليزي فقط، any للاثنين.
    page, limit: الترقيم (limit حتى 100).
    strip_diacritics: إزالة التشكيل من الاستعلام قبل الإرسال.
    include_english: إرفاق الترجمة الإنجليزية.
    max_chars: أقصى طول لكل نص (0 = النص كاملاً).
    """
    q = _strip_tashkeel(query) if strip_diacritics else query
    path = f"/v3/query/{book_id}" if book_id else "/v3/query"
    data = await _get(path, {"q": q, "lang": lang, "page": max(1, page),
                             "limit": max(1, min(limit, 100)), "highlight": "true"})
    return _fmt_page(data, max_chars, include_english)


@mcp.tool()
async def get_hadith(book_id: str, hadith_id: int, include_english: bool = True) -> str:
    """جلب حديث واحد كاملاً برقمه داخل كتاب (مع السند والمتن والتقييمات والرابط)."""
    h = await _get(f"/v3/{book_id}/{int(hadith_id)}")
    if isinstance(h, dict) and "data" in h and isinstance(h["data"], dict):
        h = h["data"]
    return _fmt_hadith(h, 0, include_english)


@mcp.tool()
async def browse_book(
    book_id: str,
    page: int = 1,
    limit: int = 30,
    chapter: str | None = None,
    category: str | None = None,
    grading: str | None = None,
    max_chars: int = 600,
) -> str:
    """تصفّح أحاديث كتاب بالترقيم، مع تصفية اختيارية بالباب أو القسم أو التقييم (مثل "صحيح")."""
    data = await _get(f"/v3/{book_id}", {"page": max(1, page), "limit": max(1, min(limit, 100)),
                                         "chapter": chapter, "category": category, "grading": grading})
    return _fmt_page(data, max_chars, False)


@mcp.tool()
async def random_hadith(book_id: str | None = None) -> str:
    """حديث عشوائي من كل الكتب أو من كتاب محدد."""
    h = await _get(f"/v2/{book_id}/random" if book_id else "/v2/random")
    return _fmt_hadith(h, 0, True)


if __name__ == "__main__":
    if HTTP_MODE:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
