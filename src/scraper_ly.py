"""
同程旅行（ly.com）机票抓取器 - Playwright 版

策略：
1. 用无头 Chromium 加载 m.ly.com 移动版搜索页（SPA，需 JS 执行）
2. 拦截所有 JSON API 响应，收集航班数据
3. 等到 networkidle，确保所有 XHR/Fetch 请求完成
4. 保存调试截图和原始 JSON，方便排查
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(__file__).resolve().parent.parent / "data"

# 城市名 → 同程三字码
CITY_CODES = {
    "北京": "BJS",
    "武汉": "WUH",
    "上海": "SHA",
    "广州": "CAN",
    "深圳": "SZX",
}

# 城市名 → 中文全名（URL 参数）
CITY_CN = {
    "北京": "北京",
    "武汉": "武汉",
    "上海": "上海",
    "广州": "广州",
    "深圳": "深圳",
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
    """同程旅行机票抓取器（Playwright 无头浏览器）"""

    MOBILE_URL = (
        "https://m.ly.com/ft/touch/book1"
        "?date={date}&fromCode={dep}&toCode={arr}"
        "&fromCity={from_city}&toCity={to_city}"
        "&adult=1&child=0&infant=0&cabin=0"
    )

    def __init__(self, delay_range: tuple[int, int] = (2, 5), timeout: int = 25):
        self.delay_range = delay_range
        self.timeout = timeout  # seconds
        self._debug_saved: set[str] = set()

    def search(
        self,
        from_city: str,
        to_city: str,
        depart_date: date,
    ) -> list[FlightOffer]:
        dep = CITY_CODES.get(from_city, from_city)
        arr = CITY_CODES.get(to_city, to_city)
        date_str = depart_date.strftime("%Y-%m-%d")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("[同程] playwright 未安装，跳过。运行: pip install playwright && playwright install chromium")
            return []

        url = self.MOBILE_URL.format(
            date=date_str,
            dep=dep,
            arr=arr,
            from_city=from_city,
            to_city=to_city,
        )
        logger.info(f"[同程] 打开 {url}")

        captured_responses: list[dict] = []
        lock = threading.Lock()

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--lang=zh-CN",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Mobile Safari/537.36"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 390, "height": 844},
            )

            page = context.new_page()

            def on_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    ru = response.url
                    body = response.body()
                    if not body:
                        return
                    data = json.loads(body)
                    with lock:
                        captured_responses.append({"url": ru, "data": data})
                    logger.info(f"[同程] 捕获 JSON: {ru[:100]}")
                except Exception as e:
                    logger.debug(f"[同程] response 解析跳过: {e}")

            page.on("response", on_response)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                # 等待网络静默（数据加载完成）
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass  # 超时也继续，可能数据已来了

                # 额外等待，给 JS 渲染时间
                time.sleep(3)

                # 尝试等待航班列表元素出现（不同版本的选择器）
                SELECTORS = [
                    "[class*='flight'][class*='item']",
                    "[class*='flightItem']",
                    "[class*='flight-item']",
                    ".flight-list .item",
                    "[class*='list'] [class*='price']",
                ]
                found_selector = None
                for sel in SELECTORS:
                    try:
                        page.wait_for_selector(sel, timeout=5_000)
                        found_selector = sel
                        logger.info(f"[同程] DOM 元素已出现: {sel}")
                        break
                    except Exception:
                        pass

                # 保存截图以便诊断
                self._save_screenshot(page, dep, arr, date_str)

                # 如果还没拿到 JSON，尝试从 DOM 解析
                dom_offers: list[FlightOffer] = []
                if found_selector:
                    dom_offers = self._parse_dom(page, from_city, to_city, date_str)

            except Exception as e:
                logger.error(f"[同程] 页面加载失败: {e}")
                self._save_screenshot(page, dep, arr, date_str)
            finally:
                browser.close()

        # ── 诊断日志：打印每个 JSON 响应的结构 ──
        logger.info(f"[同程] 共捕获 {len(captured_responses)} 个 JSON 响应，结构摘要:")
        for i, resp in enumerate(captured_responses):
            _log_json_structure(resp["url"], resp["data"], i)

        # ── 解析捕获到的 JSON 响应 ──
        offers: list[FlightOffer] = []
        for resp in captured_responses:
            parsed = self._parse_json_response(resp["url"], resp["data"], from_city, to_city, date_str)
            offers.extend(parsed)

        # 保存原始 JSON 便于下次改进解析器
        if captured_responses:
            self._save_debug_json(captured_responses, dep, arr, date_str)
        else:
            logger.warning(f"[同程] {from_city}→{to_city} {date_str} 未捕获到任何 JSON 响应")
            self._save_debug_json([], dep, arr, date_str)

        # 合并去重（JSON 优先，再补 DOM）
        all_offers = offers if offers else dom_offers
        all_offers = _dedup(all_offers)

        if all_offers:
            logger.info(f"[同程] {from_city}→{to_city} {date_str}: {len(all_offers)} 个航班，最低 ¥{all_offers[0].price}")
        else:
            logger.warning(f"[同程] {from_city}→{to_city} {date_str}: 未解析到航班数据（共捕获 {len(captured_responses)} 个 JSON 响应）")

        return all_offers

    # ─────────────────────────────────────────────
    # JSON 解析
    # ─────────────────────────────────────────────
    def _parse_json_response(
        self, url: str, data: dict | list,
        from_city: str, to_city: str, date_str: str,
    ) -> list[FlightOffer]:
        now = datetime.now().isoformat(timespec="seconds")
        offers: list[FlightOffer] = []

        # 如果是列表直接尝试
        if isinstance(data, list):
            for item in data:
                o = self._parse_flight_item(item, from_city, to_city, date_str, now)
                if o:
                    offers.append(o)
            return sorted(offers, key=lambda x: x.price) if offers else []

        # 递归找航班列表（覆盖同程/去哪儿/携程等常见字段名）
        flight_list = (
            _dig(data, "data", "flightList")
            or _dig(data, "data", "flightVOList")
            or _dig(data, "data", "flightInfoList")
            or _dig(data, "data", "flightInfos")
            or _dig(data, "data", "flights")
            or _dig(data, "data", "result", "flightList")
            or _dig(data, "data", "result", "flightVOList")
            or _dig(data, "result", "flightList")
            or _dig(data, "result", "flightVOList")
            or _dig(data, "flightList")
            or _dig(data, "flightVOList")
            or _dig(data, "flights")
            or _dig(data, "data", "list")
            or _dig(data, "list")
            or _dig(data, "data", "rows")
            or _dig(data, "rows")
        )

        if not flight_list:
            # 广搜：找第一个元素有 price/lowestPrice 的数组
            flight_list = _find_flight_array(data)

        if not flight_list:
            logger.debug(f"[同程] {url[:60]} 无 flightList 字段，跳过")
            return []

        for item in flight_list:
            try:
                o = self._parse_flight_item(item, from_city, to_city, date_str, now)
                if o:
                    offers.append(o)
            except Exception as e:
                logger.debug(f"[同程] 跳过单条: {e}")

        return sorted(offers, key=lambda x: x.price) if offers else []

    def _parse_flight_item(
        self, f: dict,
        from_city: str, to_city: str,
        date_str: str, now: str,
    ) -> Optional[FlightOffer]:
        if not isinstance(f, dict):
            return None

        price = (
            f.get("price") or f.get("lowestPrice") or f.get("minPrice")
            or f.get("salPrice") or f.get("salePrice")
            or f.get("adultPrice") or f.get("economyPrice") or 0
        )
        if not price:
            return None
        try:
            price = int(float(str(price)))
        except (ValueError, TypeError):
            return None
        if price <= 0 or price > 20000:
            return None

        def g(*keys):
            for k in keys:
                v = f.get(k)
                if v:
                    return str(v)
            return ""

        dep_time = g("departTime", "deptTime", "dep_time", "depTime", "departureTime")
        arr_time = g("arriveTime", "arrTime", "arr_time", "arrivalTime", "arrTime")
        flight_no = g("flightNo", "flight_no", "flightNumber", "flightNum")
        carrier = g("airlineName", "airline", "carrier", "airName", "airlineCode")
        dep_airport = g("departAirportName", "deptAirport", "dep_airport", "departAirport")
        arr_airport = g("arriveAirportName", "arrAirport", "arr_airport", "arrivalAirport")
        duration = 0
        for dk in ("flyTime", "duration", "flightTime", "flightDuration"):
            v = f.get(dk)
            if v:
                try:
                    duration = int(float(str(v)))
                    break
                except (ValueError, TypeError):
                    pass

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

    # ─────────────────────────────────────────────
    # DOM 解析（备用）
    # ─────────────────────────────────────────────
    def _parse_dom(self, page, from_city: str, to_city: str, date_str: str) -> list[FlightOffer]:
        """尝试从渲染后的 DOM 提取价格（粗略兜底）"""
        now = datetime.now().isoformat(timespec="seconds")
        offers: list[FlightOffer] = []
        try:
            items = page.query_selector_all("[class*='flight'][class*='item'], [class*='flightItem']")
            logger.info(f"[同程/DOM] 找到 {len(items)} 个航班元素")
            for el in items:
                text = el.inner_text()
                import re
                prices = re.findall(r"[¥￥]?\s*(\d{3,4})", text)
                flight_nos = re.findall(r"[A-Z]{2}\d{3,4}", text)
                times = re.findall(r"\d{2}:\d{2}", text)
                if not prices or not flight_nos:
                    continue
                price = int(prices[0])
                if price <= 0 or price > 20000:
                    continue
                offers.append(FlightOffer(
                    source="ly_dom",
                    from_city=from_city,
                    to_city=to_city,
                    depart_date=date_str,
                    flight_no=flight_nos[0],
                    carrier=flight_nos[0][:2] if flight_nos else "",
                    dep_time=times[0] if len(times) > 0 else "",
                    arr_time=times[1] if len(times) > 1 else "",
                    dep_airport="",
                    arr_airport="",
                    duration_min=0,
                    price=price,
                    captured_at=now,
                ))
        except Exception as e:
            logger.warning(f"[同程/DOM] 解析异常: {e}")
        return sorted(offers, key=lambda x: x.price) if offers else []

    # ─────────────────────────────────────────────
    # 调试辅助
    # ─────────────────────────────────────────────
    def _save_screenshot(self, page, dep: str, arr: str, date_str: str):
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / f"screenshot_ly_{dep}_{arr}_{date_str}.png"
            page.screenshot(path=str(path))
            logger.info(f"[同程] 截图已存: {path.name}")
        except Exception as e:
            logger.warning(f"[同程] 无法保存截图: {e}")

    def _save_debug_json(self, responses: list, dep: str, arr: str, date_str: str):
        key = f"{dep}_{arr}_{date_str}"
        if key in self._debug_saved:
            return
        self._debug_saved.add(key)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        fname = DEBUG_DIR / f"debug_ly_{dep}_{arr}_{date_str}.json"
        try:
            with fname.open("w", encoding="utf-8") as f:
                json.dump(responses, f, ensure_ascii=False, indent=2)
            logger.info(f"[同程] 捕获 JSON 已存: {fname.name} ({len(responses)} 个响应)")
        except Exception as e:
            logger.warning(f"[同程] 无法保存调试 JSON: {e}")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _dig(obj: dict, *keys):
    """安全深取值：_dig(d, 'a', 'b', 'c') → d['a']['b']['c'] or None"""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur if isinstance(cur, list) and cur else None


def _find_flight_array(obj, depth: int = 0) -> list | None:
    """在嵌套 dict 里广搜，找第一个含有 price 字段的列表"""
    if depth > 5:
        return None
    if isinstance(obj, list) and obj:
        if isinstance(obj[0], dict) and any(
            k in obj[0] for k in ("price", "lowestPrice", "minPrice", "salPrice")
        ):
            return obj
    if isinstance(obj, dict):
        for v in obj.values():
            result = _find_flight_array(v, depth + 1)
            if result:
                return result
    return None


def _log_json_structure(url: str, obj, idx: int, max_depth: int = 3):
    """递归打印 JSON 结构摘要，帮助找到航班数据字段路径"""
    def _summarize(o, depth=0) -> str:
        indent = "  " * depth
        if isinstance(o, dict):
            parts = []
            for k, v in list(o.items())[:10]:  # 最多显示10个键
                parts.append(f"{indent}  {k!r}: {_summarize(v, depth+1)}")
            suffix = f"\n{indent}  ... (+{len(o)-10} more)" if len(o) > 10 else ""
            return "{\n" + "\n".join(parts) + suffix + f"\n{indent}}}"
        elif isinstance(o, list):
            if not o:
                return "[]"
            first = _summarize(o[0], depth+1) if depth < max_depth else "..."
            return f"[{len(o)} items, first: {first}]"
        else:
            s = repr(o)
            return s[:60] + "..." if len(s) > 60 else s

    short_url = url.split("?")[0][-60:]
    summary = _summarize(obj)
    logger.info(f"[同程/诊断] [{idx}] {short_url}\n{summary}")


def _dedup(offers: list[FlightOffer]) -> list[FlightOffer]:
    seen: set[str] = set()
    result = []
    for o in offers:
        key = f"{o.flight_no}_{o.depart_date}_{o.price}"
        if key not in seen:
            seen.add(key)
            result.append(o)
    return sorted(result, key=lambda x: x.price)
