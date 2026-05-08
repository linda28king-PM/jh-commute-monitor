"""
去哪儿机票抓取器

策略：
1. 优先用低价日历接口（返回每天最低价 JSON，稳定、反爬弱）
2. 降级用 OTA 搜索接口（返回具体航班，偶尔被风控）
3. 接口随时可能变，实际跑出问题第一时间检查这里
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
]

# 城市名 → 去哪儿三字代码
CITY_CODES = {
    "北京": "BJS",
    "武汉": "WUH",
    "上海": "SHA",
    "广州": "CAN",
    "深圳": "SZX",
}


@dataclass
class FlightOffer:
    """统一的航班报价数据结构"""
    source: str              # 数据源 qunar/ctrip
    from_city: str
    to_city: str
    depart_date: str         # YYYY-MM-DD
    flight_no: str           # CA1234 / 或 "日历最低价" 占位
    carrier: str             # 国航
    dep_time: str            # 08:30
    arr_time: str            # 11:00
    dep_airport: str         # 首都T2
    arr_airport: str         # 天河T3
    duration_min: int        # 时长分钟
    price: int               # 含税总价
    base_price: Optional[int] = None
    discount: Optional[float] = None
    captured_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class QunarFlightScraper:
    """去哪儿机票抓取器"""

    # 低价日历接口：返回指定月份每天最低价，JSON 格式，最稳定
    CALENDAR_URL = "https://m.flight.qunar.com/h5/api/cheapday/oneWayPriceCalendar"

    # 备用：OTA 航班列表接口
    FLIGHT_LIST_URL = "https://m.flight.qunar.com/h5/api/flight/onewayList"

    # 备用2：PC 版接口
    PC_LIST_URL = "https://flight.qunar.com/site/onewayFlightList.htm"

    def __init__(self, delay_range: tuple[int, int] = (2, 5), timeout: int = 20):
        self.delay_range = delay_range
        self.timeout = timeout
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,   # 不跟 301，方便检测被重定向
            headers=self._build_headers(),
        )

    def _build_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://m.flight.qunar.com/",
            "Origin": "https://m.flight.qunar.com",
        }

    def _polite_delay(self):
        delay = random.uniform(*self.delay_range)
        time.sleep(delay)

    def search(
        self,
        from_city: str,
        to_city: str,
        depart_date: date,
    ) -> list[FlightOffer]:
        """
        搜索单程机票，优先走日历接口

        Returns:
            航班报价列表，按价格升序
        """
        # 先尝试日历接口（最稳定）
        offers = self._search_calendar(from_city, to_city, depart_date)
        if offers:
            return offers

        # 降级尝试 OTA 航班列表接口
        offers = self._search_flight_list(from_city, to_city, depart_date)
        if offers:
            return offers

        logger.error(f"[去哪儿] {from_city}→{to_city} {depart_date} 所有接口均失败")
        return []

    # ──────────────────────────────────────────────
    # 接口1：低价日历（推荐，返回 JSON 最稳定）
    # ──────────────────────────────────────────────
    def _search_calendar(
        self, from_city: str, to_city: str, depart_date: date
    ) -> list[FlightOffer]:
        date_str = depart_date.strftime("%Y-%m-%d")
        month_str = depart_date.strftime("%Y-%m")

        dep_code = CITY_CODES.get(from_city, from_city)
        arr_code = CITY_CODES.get(to_city, to_city)

        params = {
            "dep": dep_code,
            "arr": arr_code,
            "startDate": month_str + "-01",
            "endDate": depart_date.replace(day=28).strftime("%Y-%m") + "-28",
        }

        for attempt in range(3):
            try:
                self.client.headers.update({"User-Agent": random.choice(USER_AGENTS)})
                logger.info(f"[去哪儿日历] {from_city}→{to_city} {month_str} (尝试 {attempt+1}/3)")

                resp = self.client.get(self.CALENDAR_URL, params=params)

                if resp.status_code in (301, 302):
                    logger.warning(f"[去哪儿日历] 被重定向 {resp.status_code}，跳过")
                    return []

                if resp.status_code != 200:
                    logger.warning(f"[去哪儿日历] HTTP {resp.status_code}，重试")
                    self._polite_delay()
                    continue

                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    logger.warning(f"[去哪儿日历] 非 JSON 响应，前200字: {resp.text[:200]}")
                    self._polite_delay()
                    continue

                offers = self._parse_calendar(data, from_city, to_city, date_str)
                if offers:
                    logger.info(f"[去哪儿日历] 抓到 {date_str} 最低价 ¥{offers[0].price}")
                    return offers
                else:
                    logger.warning(f"[去哪儿日历] 未找到 {date_str} 数据，响应: {str(data)[:300]}")
                    return []

            except httpx.RequestError as e:
                logger.error(f"[去哪儿日历] 网络异常: {e}")
            self._polite_delay()

        return []

    def _parse_calendar(
        self, data: dict, from_city: str, to_city: str, date_str: str
    ) -> list[FlightOffer]:
        """解析低价日历响应，提取指定日期的最低价"""
        now = datetime.now().isoformat(timespec="seconds")

        # 尝试多种响应结构
        # 结构1: {"ret": true, "data": [{"date": "2026-05-15", "price": 680}, ...]}
        # 结构2: {"data": {"2026-05-15": 680, ...}}
        # 结构3: {"ret": true, "data": {"calendar": [...]}}

        raw = data.get("data") or data.get("result") or {}

        price = None

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("date") == date_str:
                    price = item.get("price") or item.get("lowestPrice") or item.get("minPrice")
                    break
        elif isinstance(raw, dict):
            # {"2026-05-15": 680, ...}
            if date_str in raw:
                val = raw[date_str]
                price = val if isinstance(val, (int, float)) else (
                    val.get("price") or val.get("lowestPrice") if isinstance(val, dict) else None
                )
            # {"calendar": [...]}
            elif "calendar" in raw:
                for item in raw.get("calendar", []):
                    if isinstance(item, dict) and item.get("date") == date_str:
                        price = item.get("price") or item.get("lowestPrice")
                        break

        if not price:
            logger.debug(f"[去哪儿日历] {date_str} 无价格，data keys: {list(data.keys())}")
            return []

        price = int(price)
        return [FlightOffer(
            source="qunar",
            from_city=from_city,
            to_city=to_city,
            depart_date=date_str,
            flight_no="(最低价)",
            carrier="综合",
            dep_time="--",
            arr_time="--",
            dep_airport="",
            arr_airport="",
            duration_min=0,
            price=price,
            captured_at=now,
        )]

    # ──────────────────────────────────────────────
    # 接口2：OTA 航班列表（有具体航班，偶被风控）
    # ──────────────────────────────────────────────
    def _search_flight_list(
        self, from_city: str, to_city: str, depart_date: date
    ) -> list[FlightOffer]:
        date_str = depart_date.strftime("%Y-%m-%d")
        dep_code = CITY_CODES.get(from_city, from_city)
        arr_code = CITY_CODES.get(to_city, to_city)

        params = {
            "depCity": dep_code,
            "arrCity": arr_code,
            "dptDate": date_str,
            "channel": "h5",
            "from": "m_flight_h5",
        }

        for attempt in range(2):
            try:
                self.client.headers.update({"User-Agent": random.choice(USER_AGENTS)})
                logger.info(f"[去哪儿列表] {from_city}→{to_city} {date_str} (尝试 {attempt+1}/2)")

                resp = self.client.get(self.FLIGHT_LIST_URL, params=params)

                if resp.status_code in (301, 302):
                    logger.warning(f"[去哪儿列表] 被重定向，接口失效")
                    return []

                if resp.status_code != 200:
                    logger.warning(f"[去哪儿列表] HTTP {resp.status_code}")
                    self._polite_delay()
                    continue

                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    logger.warning(f"[去哪儿列表] 非 JSON，前200字: {resp.text[:200]}")
                    return []

                offers = self._parse_flight_list(data, from_city, to_city, date_str)
                if offers:
                    logger.info(f"[去哪儿列表] 抓到 {len(offers)} 个航班")
                    return sorted(offers, key=lambda x: x.price)
                else:
                    logger.warning(f"[去哪儿列表] 解析后为空，keys: {list(data.keys())[:10]}")

            except httpx.RequestError as e:
                logger.error(f"[去哪儿列表] 网络异常: {e}")
            self._polite_delay()

        return []

    def _parse_flight_list(
        self, data: dict, from_city: str, to_city: str, date_str: str
    ) -> list[FlightOffer]:
        now = datetime.now().isoformat(timespec="seconds")
        offers: list[FlightOffer] = []

        flights = (
            data.get("data", {}).get("flights")
            or data.get("data", {}).get("list")
            or data.get("flights")
            or data.get("list")
            or []
        )

        for f in flights:
            try:
                offer = FlightOffer(
                    source="qunar",
                    from_city=from_city,
                    to_city=to_city,
                    depart_date=date_str,
                    flight_no=str(f.get("flightNo") or f.get("flight_no") or ""),
                    carrier=str(f.get("airShortName") or f.get("airline") or ""),
                    dep_time=str(f.get("dptTime") or f.get("dep_time") or ""),
                    arr_time=str(f.get("arrTime") or f.get("arr_time") or ""),
                    dep_airport=str(f.get("dptAirport") or f.get("dep_airport") or ""),
                    arr_airport=str(f.get("arrAirport") or f.get("arr_airport") or ""),
                    duration_min=int(f.get("flyTime") or f.get("duration") or 0),
                    price=int(f.get("price") or f.get("minPrice") or 0),
                    base_price=f.get("basePrice"),
                    discount=f.get("discount"),
                    captured_at=now,
                )
                if offer.price > 0 and offer.flight_no:
                    offers.append(offer)
            except (ValueError, TypeError) as e:
                logger.debug(f"[去哪儿列表] 跳过解析异常: {e}")
                continue

        return offers

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
