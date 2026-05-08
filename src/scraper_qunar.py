"""
去哪儿机票抓取器

策略：
1. 依次尝试多个已知接口，成功则返回
2. 每次失败都详细打印响应（方便 Actions 日志排查）
3. 把第一次原始响应存到 data/debug_qunar_*.json 方便离线分析
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
]

CITY_CODES = {
    "北京": "BJS",
    "武汉": "WUH",
    "上海": "SHA",
    "广州": "CAN",
    "深圳": "SZX",
}

DEBUG_DIR = Path(__file__).resolve().parent.parent / "data"


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


class QunarFlightScraper:

    def __init__(self, delay_range: tuple[int, int] = (2, 5), timeout: int = 20):
        self.delay_range = delay_range
        self.timeout = timeout
        self._debug_saved: set[str] = set()
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
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
        time.sleep(random.uniform(*self.delay_range))

    def _get_json(self, url: str, params: dict, label: str) -> Optional[dict]:
        """发请求，详细记录每次结果，遇到重定向/非JSON立刻返回 None"""
        self.client.headers.update({"User-Agent": random.choice(USER_AGENTS)})
        try:
            resp = self.client.get(url, params=params)
            logger.info(f"[去哪儿/{label}] {resp.status_code} {resp.url}")

            if resp.status_code in (301, 302):
                location = resp.headers.get("location", "")
                logger.warning(f"[去哪儿/{label}] 重定向到 {location}，接口已失效")
                return None

            if resp.status_code != 200:
                logger.warning(f"[去哪儿/{label}] HTTP {resp.status_code}，跳过")
                return None

            # 先看是否 HTML（被风控）
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                logger.warning(f"[去哪儿/{label}] 返回 HTML（被风控），Content-Type={ct}")
                logger.warning(f"[去哪儿/{label}] 响应前300字: {resp.text[:300]}")
                return None

            try:
                data = resp.json()
                logger.debug(f"[去哪儿/{label}] JSON 顶层 keys: {list(data.keys())[:10]}")
                return data
            except json.JSONDecodeError:
                logger.warning(f"[去哪儿/{label}] 非 JSON，Content-Type={ct}")
                logger.warning(f"[去哪儿/{label}] 响应前500字: {resp.text[:500]}")
                return None

        except httpx.RequestError as e:
            logger.error(f"[去哪儿/{label}] 网络异常: {e}")
            return None

    def search(
        self,
        from_city: str,
        to_city: str,
        depart_date: date,
    ) -> list[FlightOffer]:
        date_str = depart_date.strftime("%Y-%m-%d")
        dep = CITY_CODES.get(from_city, from_city)
        arr = CITY_CODES.get(to_city, to_city)

        # ── 尝试1：低价日历（中文城市代码，YYYY-MM-DD）──
        data = self._get_json(
            "https://m.flight.qunar.com/h5/api/cheapday/oneWayPriceCalendar",
            {"dep": dep, "arr": arr, "startDate": depart_date.strftime("%Y-%m-01")},
            "日历v1",
        )
        if data is not None:
            offers = self._parse_calendar(data, from_city, to_city, date_str)
            if offers:
                return offers
            logger.warning(f"[去哪儿/日历v1] 有响应但解析不到 {date_str}，完整响应: {json.dumps(data, ensure_ascii=False)[:600]}")
            self._save_debug(data, dep, arr, date_str, "calendar_v1")
        self._polite_delay()

        # ── 尝试2：低价日历（depCity/arrCity 参数名）──
        data = self._get_json(
            "https://m.flight.qunar.com/h5/api/cheapday/oneWayPriceCalendar",
            {"depCity": dep, "arrCity": arr, "startDate": depart_date.strftime("%Y-%m-01")},
            "日历v2",
        )
        if data is not None:
            offers = self._parse_calendar(data, from_city, to_city, date_str)
            if offers:
                return offers
            logger.warning(f"[去哪儿/日历v2] 有响应但解析不到 {date_str}，完整响应: {json.dumps(data, ensure_ascii=False)[:600]}")
        self._polite_delay()

        # ── 尝试3：PC 端低价月历 ──
        data = self._get_json(
            "https://flight.qunar.com/site/lowestPriceMonth.htm",
            {
                "searchDepartureAirport": from_city,
                "searchArrivalAirport": to_city,
                "date": depart_date.strftime("%Y-%m"),
            },
            "PC月历",
        )
        if data is not None:
            offers = self._parse_calendar(data, from_city, to_city, date_str)
            if offers:
                return offers
            logger.warning(f"[去哪儿/PC月历] 有响应但解析不到 {date_str}，完整响应: {json.dumps(data, ensure_ascii=False)[:600]}")
            self._save_debug(data, dep, arr, date_str, "pc_calendar")
        self._polite_delay()

        # ── 尝试4：OTA 航班列表（depCity/arrCity）──
        data = self._get_json(
            "https://m.flight.qunar.com/h5/api/flight/onewayList",
            {"depCity": dep, "arrCity": arr, "dptDate": date_str, "channel": "h5"},
            "列表v1",
        )
        if data is not None:
            offers = self._parse_flight_list(data, from_city, to_city, date_str)
            if offers:
                return sorted(offers, key=lambda x: x.price)
            logger.warning(f"[去哪儿/列表v1] 有响应但无航班，完整响应: {json.dumps(data, ensure_ascii=False)[:600]}")
            self._save_debug(data, dep, arr, date_str, "list_v1")
        self._polite_delay()

        # ── 尝试5：OTA 航班列表（dep/arr）──
        data = self._get_json(
            "https://m.flight.qunar.com/h5/api/flight/onewayList",
            {"dep": dep, "arr": arr, "date": date_str, "channel": "h5"},
            "列表v2",
        )
        if data is not None:
            offers = self._parse_flight_list(data, from_city, to_city, date_str)
            if offers:
                return sorted(offers, key=lambda x: x.price)
            logger.warning(f"[去哪儿/列表v2] 有响应但无航班，完整响应: {json.dumps(data, ensure_ascii=False)[:600]}")
        self._polite_delay()

        logger.error(f"[去哪儿] {from_city}→{to_city} {date_str} 所有接口均失败")
        return []

    def _save_debug(self, data: dict, dep: str, arr: str, date_str: str, tag: str):
        """保存原始响应到 data/ 供排查（每个路线+标签只存一次）"""
        key = f"{dep}_{arr}_{tag}"
        if key in self._debug_saved:
            return
        self._debug_saved.add(key)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        fname = DEBUG_DIR / f"debug_qunar_{dep}_{arr}_{date_str}_{tag}.json"
        try:
            with fname.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"[去哪儿] 原始响应已存: {fname.name}")
        except Exception as e:
            logger.warning(f"[去哪儿] 无法保存调试文件: {e}")

    def _parse_calendar(
        self, data: dict, from_city: str, to_city: str, date_str: str
    ) -> list[FlightOffer]:
        now = datetime.now().isoformat(timespec="seconds")
        raw = data.get("data") or data.get("result") or {}

        price = None
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("date") == date_str:
                    price = item.get("price") or item.get("lowestPrice") or item.get("minPrice")
                    break
        elif isinstance(raw, dict):
            if date_str in raw:
                val = raw[date_str]
                price = val if isinstance(val, (int, float)) else (
                    val.get("price") or val.get("lowestPrice") if isinstance(val, dict) else None
                )
            elif "calendar" in raw:
                for item in raw.get("calendar", []):
                    if isinstance(item, dict) and item.get("date") == date_str:
                        price = item.get("price") or item.get("lowestPrice")
                        break
            # 结构: {"priceList": [{"departDate": "2026-05-15", "price": 680}]}
            elif "priceList" in raw or "priceList" in data:
                pl = raw.get("priceList") or data.get("priceList") or []
                for item in pl:
                    d = item.get("departDate") or item.get("date")
                    if d == date_str:
                        price = item.get("price") or item.get("lowestPrice")
                        break

        if not price:
            return []

        return [FlightOffer(
            source="qunar",
            from_city=from_city,
            to_city=to_city,
            depart_date=date_str,
            flight_no="(日历最低价)",
            carrier="综合",
            dep_time="--",
            arr_time="--",
            dep_airport="",
            arr_airport="",
            duration_min=0,
            price=int(price),
            captured_at=now,
        )]

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
                logger.debug(f"[去哪儿/列表] 跳过解析异常: {e}")

        return offers

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
