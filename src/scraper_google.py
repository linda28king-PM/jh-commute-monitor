"""
Google Flights 机票抓取器

使用 fast-flights 库（无需 API Key），适合 GitHub Actions 海外节点。
GitHub Actions 的 ubuntu-latest 节点是海外 IP，可正常访问 Google。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


def _get_seat_value():
    """返回当前 fast-flights 版本接受的 seat 参数值，不支持则返回 None"""
    try:
        from fast_flights import SeatType
        return SeatType.ECONOMY
    except ImportError:
        pass
    # 尝试字符串形式（部分版本接受）
    return "economy"


# 城市名 → IATA 代码（Google Flights 用）
AIRPORT_CODES: dict[str, list[str]] = {
    "北京": ["PEK", "PKX"],   # 首都/大兴，先试首都
    "武汉": ["WUH"],
    "上海": ["SHA", "PVG"],
    "广州": ["CAN"],
    "深圳": ["SZX"],
}

# 粗略汇率（USD→CNY），Google Flights 在海外节点默认显示 USD
USD_TO_CNY = 7.2


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
    price: int           # 统一存 CNY
    base_price: Optional[int] = None
    discount: Optional[float] = None
    captured_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_price_to_cny(price_str: str) -> Optional[int]:
    """把 '$120' / '¥750' / 'CNY750' / '750' 统一换算成 CNY 整数"""
    if not price_str:
        return None
    s = str(price_str).strip().replace(",", "")
    m = re.search(r"[\d.]+", s)
    if not m:
        return None
    val = float(m.group())
    # 判断货币
    if "$" in s or "USD" in s.upper():
        return int(val * USD_TO_CNY)
    # ¥ / CNY / 人民币 → 直接用
    return int(val)


def _parse_duration_min(duration_str: str) -> int:
    """'2 hr 15 min' / '1h30m' / '135 min' → 分钟数"""
    if not duration_str:
        return 0
    hours = re.search(r"(\d+)\s*h(?:r)?", duration_str, re.IGNORECASE)
    mins = re.search(r"(\d+)\s*m(?:in)?", duration_str, re.IGNORECASE)
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))
    if total == 0:
        # 纯数字，当分钟
        m = re.search(r"(\d+)", duration_str)
        if m:
            total = int(m.group(1))
    return total


def _fmt_time(t: str) -> str:
    """'7:00 AM' → '07:00' / '14:30' → '14:30'（已是24h直接返回）"""
    if not t:
        return "--"
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", t, re.IGNORECASE)
    if not m:
        return t
    h, mn, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mn:02d}"


class GoogleFlightScraper:
    """Google Flights 机票抓取器（依赖 fast-flights 库）"""

    def __init__(self, delay_range: tuple[int, int] = (2, 5), timeout: int = 30):
        self.delay_range = delay_range
        self.timeout = timeout
        self._check_import()

    def _check_import(self):
        try:
            import fast_flights as ff
            exported = dir(ff)
            logger.info(f"[Google] fast-flights 加载成功，导出名: {exported}")
        except ImportError:
            logger.error("[Google] 缺少依赖：pip install fast-flights")
            raise

    def search(
        self,
        from_city: str,
        to_city: str,
        depart_date: date,
    ) -> list[FlightOffer]:
        from_codes = AIRPORT_CODES.get(from_city, [from_city])
        to_codes = AIRPORT_CODES.get(to_city, [to_city])
        date_str = depart_date.strftime("%Y-%m-%d")

        for dep_code in from_codes:
            for arr_code in to_codes:
                offers = self._search_pair(dep_code, arr_code, from_city, to_city, date_str)
                if offers:
                    return offers
                time.sleep(2)

        logger.warning(f"[Google] {from_city}→{to_city} {date_str} 未查到航班")
        return []

    def _search_pair(
        self,
        dep_code: str,
        arr_code: str,
        from_city: str,
        to_city: str,
        date_str: str,
    ) -> list[FlightOffer]:
        try:
            import fast_flights as ff
            from fast_flights import FlightData, Passengers, create_filter

            logger.info(f"[Google] 查询 {dep_code}→{arr_code} {date_str}")

            filter_kwargs: dict = dict(
                flight_data=[FlightData(date=date_str, from_airport=dep_code, to_airport=arr_code)],
                trip="one-way",
                passengers=Passengers(adults=1),
            )
            seat_val = _get_seat_value()
            if seat_val is not None:
                filter_kwargs["seat"] = seat_val

            f = create_filter(**filter_kwargs)

            # 优先用 get_flights_from_filter（新版 API）
            if hasattr(ff, "get_flights_from_filter"):
                result = ff.get_flights_from_filter(f)
            else:
                result = ff.get_flights(f)

        except Exception as e:
            logger.error(f"[Google] 查询异常: {e}")
            return []

        if not result or not hasattr(result, "flights") or not result.flights:
            logger.warning(f"[Google] {dep_code}→{arr_code} 无结果，result={result}")
            return []

        now = datetime.now().isoformat(timespec="seconds")
        offers: list[FlightOffer] = []

        for f in result.flights:
            try:
                price_raw = getattr(f, "price", None) or getattr(f, "price_str", None)
                price_cny = _parse_price_to_cny(str(price_raw)) if price_raw else None
                if not price_cny or price_cny <= 0:
                    continue

                dep_t = _fmt_time(str(getattr(f, "departure", "") or ""))
                arr_t = _fmt_time(str(getattr(f, "arrival", "") or ""))
                dur = _parse_duration_min(str(getattr(f, "duration", "") or ""))
                name = str(getattr(f, "name", "") or "")
                stops = int(getattr(f, "stops", 0) or 0)

                offers.append(FlightOffer(
                    source="google",
                    from_city=from_city,
                    to_city=to_city,
                    depart_date=date_str,
                    flight_no=name or f"({dep_code}→{arr_code})",
                    carrier=name.split()[0] if name else "未知",
                    dep_time=dep_t,
                    arr_time=arr_t,
                    dep_airport=dep_code,
                    arr_airport=arr_code,
                    duration_min=dur,
                    price=price_cny,
                    base_price=None,
                    discount=None,
                    captured_at=now,
                ))
            except Exception as e:
                logger.debug(f"[Google] 跳过单条解析异常: {e} | flight={f}")
                continue

        direct = [o for o in offers if "non" not in o.flight_no.lower() and "stop" not in o.carrier.lower()]
        result_list = direct if direct else offers
        result_list.sort(key=lambda x: x.price)

        logger.info(f"[Google] {dep_code}→{arr_code} {date_str}: {len(result_list)} 个航班，最低 ¥{result_list[0].price if result_list else '-'}")
        return result_list

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
