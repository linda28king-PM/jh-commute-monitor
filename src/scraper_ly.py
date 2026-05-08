"""
同程旅行（ly.com）机票抓取器

策略：
1. 直接请求 www.ly.com 桌面搜索页，提取页面内嵌的 JSON 初始化数据
2. 如果内嵌数据不可用，尝试已知的 JSON API 路径
3. 完整记录每次响应，方便通过 Actions 日志排查
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(__file__).resolve().parent.parent / "data"

USER_AGENTS = [
    # 模拟国内 Android 手机，更像真实用户
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

# 城市名 → 同程用的机场/城市三字码
CITY_CODES = {
    "北京": "BJS",
    "武汉": "WUH",
    "上海": "SHA",
    "广州": "CAN",
    "深圳": "SZX",
}


@dataclass
class FlightOffer:
    source: str
    from_city: str
    to_city: str
    depart_date: str
    flight_no: str
    carrier: str
    dep_time: str
    arr_time: str
    dep_airport: str
    arr_airport: str
    duration_min: int
    price: int
    base_price: Optional[int] = None
    discount: Optional[float] = None
    captured_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class LyFlightScraper:
    """同程旅行机票抓取器"""

    # 桌面版搜索页 URL，页面内通常有 window.__INITIAL_DATA__ 等预加载 JSON
    WEB_URL = "https://www.ly.com/flights/itinerary/oneway/{dep}-{arr}"

    # 移动版（有时 JSON 结构更简单）
    MOBILE_URL = "https://m.ly.com/flights/itinerary/oneway/{dep}-{arr}"

    def __init__(self, delay_range: tuple[int, int] = (2, 5), timeout: int = 25):
        self.delay_range = delay_range
        self.timeout = timeout
        self._debug_saved: set[str] = set()

    def _build_headers(self, mobile: bool = False) -> dict:
        ua = random.choice(USER_AGENTS)
        referer = "https://m.ly.com/" if mobile else "https://www.ly.com/"
        return {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.9",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": referer,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }

    def search(
        self,
        from_city: str,
        to_city: str,
        depart_date: date,
    ) -> list[FlightOffer]:
        dep = CITY_CODES.get(from_city, from_city)
        arr = CITY_CODES.get(to_city, to_city)
        date_str = depart_date.strftime("%Y-%m-%d")

        # 先试桌面版
        offers = self._try_web(dep, arr, from_city, to_city, date_str, mobile=False)
        if offers:
            return offers
        time.sleep(random.uniform(*self.delay_range))

        # 再试移动版
        offers = self._try_web(dep, arr, from_city, to_city, date_str, mobile=True)
        if offers:
            return offers

        logger.error(f"[同程] {from_city}→{to_city} {date_str} 所有方式均失败")
        return []

    def _try_web(
        self, dep: str, arr: str,
        from_city: str, to_city: str,
        date_str: str, mobile: bool,
    ) -> list[FlightOffer]:
        label = "mobile" if mobile else "desktop"
        base = self.MOBILE_URL if mobile else self.WEB_URL
        url = base.format(dep=dep, arr=arr)
        params = {
            "date": date_str,
            "from": from_city,
            "to": to_city,
        }

        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers=self._build_headers(mobile),
            ) as client:
                logger.info(f"[同程/{label}] GET {url}?date={date_str}")
                resp = client.get(url, params=params)

            logger.info(f"[同程/{label}] HTTP {resp.status_code} | CT={resp.headers.get('content-type','')[:60]}")

            if resp.status_code != 200:
                logger.warning(f"[同程/{label}] 非200，跳过")
                return []

            body = resp.text

            # ─── 尝试1：页面直接返回 JSON ───
            if "application/json" in resp.headers.get("content-type", ""):
                try:
                    data = resp.json()
                    offers = self._parse_json_api(data, from_city, to_city, date_str)
                    if offers:
                        return offers
                    logger.warning(f"[同程/{label}] JSON解析无结果，完整响应: {json.dumps(data, ensure_ascii=False)[:800]}")
                    self._save_debug(data, dep, arr, date_str, label)
                    return []
                except Exception as e:
                    logger.warning(f"[同程/{label}] JSON解析异常: {e}")

            # ─── 尝试2：从 HTML 中提取嵌入 JSON ───
            logger.info(f"[同程/{label}] HTML长度={len(body)}，尝试提取嵌入JSON")
            offers = self._extract_from_html(body, from_city, to_city, date_str)
            if offers:
                return offers

            # 没拿到数据，记录便于排查
            logger.warning(f"[同程/{label}] HTML中未找到航班数据，前800字:\n{body[:800]}")
            self._save_debug({"html_snippet": body[:3000]}, dep, arr, date_str, label)
            return []

        except httpx.RequestError as e:
            logger.error(f"[同程/{label}] 网络异常: {e}")
            return []

    # ─────────────────────────────────────────────
    # JSON API 解析（如果接口直接返回 JSON）
    # ─────────────────────────────────────────────
    def _parse_json_api(
        self, data: dict,
        from_city: str, to_city: str, date_str: str,
    ) -> list[FlightOffer]:
        now = datetime.now().isoformat(timespec="seconds")
        offers: list[FlightOffer] = []

        # 同程常见结构尝试
        flights = (
            data.get("data", {}).get("flightList")
            or data.get("data", {}).get("flights")
            or data.get("flightList")
            or data.get("flights")
            or []
        )

        for f in flights:
            try:
                offer = self._flight_dict_to_offer(f, from_city, to_city, date_str, now)
                if offer:
                    offers.append(offer)
            except Exception as e:
                logger.debug(f"[同程] 跳过单条: {e}")

        return sorted(offers, key=lambda x: x.price) if offers else []

    # ─────────────────────────────────────────────
    # HTML 内嵌 JSON 提取
    # ─────────────────────────────────────────────
    def _extract_from_html(
        self, html: str,
        from_city: str, to_city: str, date_str: str,
    ) -> list[FlightOffer]:
        now = datetime.now().isoformat(timespec="seconds")

        # 常见的预加载 JSON 变量名（按可能性排序）
        patterns = [
            r'window\.__INITIAL_DATA__\s*=\s*(\{.+?\})\s*;?\s*(?:</script>|window\.)',
            r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*;?\s*(?:</script>|window\.)',
            r'window\.__NUXT__\s*=\s*(\{.+?\})\s*;?\s*(?:</script>|window\.)',
            r'window\.__DATA__\s*=\s*(\{.+?\})\s*;?\s*(?:</script>|window\.)',
            r'"flightList"\s*:\s*(\[.+?\])\s*[,}]',
            r'"flights"\s*:\s*(\[.+?\])\s*[,}]',
        ]

        for pat in patterns:
            m = re.search(pat, html, re.DOTALL)
            if not m:
                continue
            raw = m.group(1)
            logger.info(f"[同程/html] 匹配到模式 {pat[:40]}，长度={len(raw)}")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # 截断 JSON 可能不完整，跳过
                continue

            if isinstance(data, list):
                # 直接是 flightList 数组
                offers = []
                for f in data:
                    offer = self._flight_dict_to_offer(f, from_city, to_city, date_str, now)
                    if offer:
                        offers.append(offer)
                if offers:
                    return sorted(offers, key=lambda x: x.price)
            else:
                offers = self._parse_json_api(data, from_city, to_city, date_str)
                if offers:
                    return offers

        return []

    # ─────────────────────────────────────────────
    # 单条航班字段映射
    # ─────────────────────────────────────────────
    def _flight_dict_to_offer(
        self, f: dict,
        from_city: str, to_city: str,
        date_str: str, now: str,
    ) -> Optional[FlightOffer]:
        price = (
            f.get("price") or f.get("lowestPrice") or f.get("minPrice")
            or f.get("salPrice") or f.get("salePrice") or 0
        )
        if not price:
            return None
        price = int(float(str(price)))
        if price <= 0:
            return None

        def g(*keys):
            for k in keys:
                v = f.get(k)
                if v:
                    return str(v)
            return ""

        dep_time = g("departTime", "deptTime", "dep_time", "depTime")
        arr_time = g("arriveTime", "arrTime", "arr_time", "arrivalTime")
        flight_no = g("flightNo", "flight_no", "flightNumber")
        carrier = g("airlineName", "airline", "carrier", "airName")
        dep_airport = g("departAirportName", "deptAirport", "dep_airport")
        arr_airport = g("arriveAirportName", "arrAirport", "arr_airport")
        duration = int(f.get("flyTime") or f.get("duration") or f.get("flightTime") or 0)

        if not flight_no:
            return None

        return FlightOffer(
            source="ly",
            from_city=from_city,
            to_city=to_city,
            depart_date=date_str,
            flight_no=flight_no,
            carrier=carrier,
            dep_time=dep_time,
            arr_time=arr_time,
            dep_airport=dep_airport,
            arr_airport=arr_airport,
            duration_min=duration,
            price=price,
            captured_at=now,
        )

    def _save_debug(self, data, dep, arr, date_str, tag):
        key = f"{dep}_{arr}_{tag}"
        if key in self._debug_saved:
            return
        self._debug_saved.add(key)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        fname = DEBUG_DIR / f"debug_ly_{dep}_{arr}_{date_str}_{tag}.json"
        try:
            with fname.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"[同程] 原始响应已存: {fname.name}")
        except Exception as e:
            logger.warning(f"[同程] 无法保存调试文件: {e}")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
