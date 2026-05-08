"""
去哪儿机票抓取器

策略：
1. 直接调用去哪儿 H5 端的 list API，返回 JSON，无需解析 HTML
2. 这是手机网页版接口，反爬比 PC 版弱
3. 接口随时可能变，实际跑出问题第一时间检查这里
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
]


@dataclass
class FlightOffer:
    """统一的航班报价数据结构"""
    source: str              # 数据源 qunar/ctrip
    from_city: str
    to_city: str
    depart_date: str         # YYYY-MM-DD
    flight_no: str           # CA1234
    carrier: str             # 国航
    dep_time: str            # 08:30
    arr_time: str            # 11:00
    dep_airport: str         # 首都T2
    arr_airport: str         # 天河T3
    duration_min: int        # 时长分钟
    price: int               # 含税总价
    base_price: Optional[int] = None  # 不含税
    discount: Optional[float] = None  # 折扣
    captured_at: str = ""    # 抓取时间戳

    def to_dict(self) -> dict:
        return asdict(self)


class QunarFlightScraper:
    """去哪儿机票抓取器"""

    BASE_URL = "https://touch.dujia.qunar.com/golfz/sight/api/onewayFlightList"
    # 备用 H5 接口
    LIST_URL = "https://m.flight.qunar.com/h5/flight/onewaylist"
    # 真实使用的 JSON 接口（手机网页背后调用的）
    API_URL = "https://m.flight.qunar.com/h5/flight/listinfo"

    def __init__(self, delay_range: tuple[int, int] = (2, 5), timeout: int = 20):
        self.delay_range = delay_range
        self.timeout = timeout
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=self._build_headers(),
        )

    def _build_headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://m.flight.qunar.com/",
            "Origin": "https://m.flight.qunar.com",
        }

    def _polite_delay(self):
        """友善延迟，降低被风控概率"""
        delay = random.uniform(*self.delay_range)
        time.sleep(delay)

    def search(
        self,
        from_city: str,
        to_city: str,
        depart_date: date,
    ) -> list[FlightOffer]:
        """
        搜索单程机票

        Args:
            from_city: 出发城市中文名 / 三字代码
            to_city: 到达城市
            depart_date: 出发日期

        Returns:
            航班报价列表，按价格升序
        """
        date_str = depart_date.strftime("%Y-%m-%d")

        # 去哪儿 H5 搜索参数（已用浏览器抓包验证过的格式）
        params = {
            "dep": from_city,
            "arr": to_city,
            "date": date_str,
            "from": "flight_h5_index",
            "channel": "h5",
            "transit": 0,         # 0=直飞优先, 1=不含中转
        }

        for attempt in range(3):
            try:
                # 刷新 UA
                self.client.headers.update({"User-Agent": random.choice(USER_AGENTS)})

                logger.info(f"[去哪儿] 搜索 {from_city}→{to_city} {date_str} (尝试 {attempt+1}/3)")
                resp = self.client.get(self.API_URL, params=params)

                # 风控页面通常返回非 200 或 HTML
                if resp.status_code != 200:
                    logger.warning(f"[去哪儿] HTTP {resp.status_code}, 重试")
                    self._polite_delay()
                    continue

                # 尝试解析 JSON
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    logger.warning(f"[去哪儿] 返回非 JSON，可能被风控；前 200 字: {resp.text[:200]}")
                    self._polite_delay()
                    continue

                offers = self._parse_response(data, from_city, to_city, date_str)
                if offers:
                    logger.info(f"[去哪儿] 抓到 {len(offers)} 个航班")
                    return sorted(offers, key=lambda x: x.price)
                else:
                    logger.warning(f"[去哪儿] 解析后航班列表为空，响应顶层 keys: {list(data.keys())[:10]}, 前200字: {str(data)[:200]}")

            except httpx.RequestError as e:
                logger.error(f"[去哪儿] 网络异常: {e}")

            self._polite_delay()

        logger.error(f"[去哪儿] {from_city}→{to_city} {date_str} 抓取失败")
        return []

    def _parse_response(
        self,
        data: dict,
        from_city: str,
        to_city: str,
        date_str: str,
    ) -> list[FlightOffer]:
        """
        解析去哪儿返回的 JSON

        注意：去哪儿接口返回结构会变，下面的字段路径可能需要根据实际返回调整。
        实际使用时建议先 print 一次完整 response，根据真实结构修改。
        """
        offers: list[FlightOffer] = []
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")

        # 常见的几种返回结构都尝试一下
        flights = (
            data.get("data", {}).get("flights")
            or data.get("data", {}).get("list")
            or data.get("flights")
            or data.get("list")
            or []
        )

        if not flights:
            logger.debug(f"[去哪儿] 响应结构: {list(data.keys())}")
            return []

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
                # 过滤无效数据
                if offer.price > 0 and offer.flight_no:
                    offers.append(offer)
            except (ValueError, TypeError) as e:
                logger.debug(f"[去哪儿] 跳过单条解析异常: {e}")
                continue

        return offers

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
