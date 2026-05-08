"""
京汉通勤监控 - 主入口

执行顺序：
1. 读取 config.yaml
2. 计算未来 N 周的所有目标日期
3. 对每条航线 × 每个日期 × 每个数据源 抓取
4. 全部存入 SQLite
5. 跑提醒规则引擎
6. 触发推送
7. 导出 JSON 给前端用
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

# 让 src 目录可被导入
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scraper_qunar import QunarFlightScraper
from src.scraper_google import GoogleFlightScraper
from src.scraper_railway import RailwayScraper
from src.storage import Storage
from src.notifier import Notifier
from src.alert_engine import AlertEngine


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def compute_target_dates(routes: list[dict], weeks_ahead: int) -> list[tuple[dict, date]]:
    """对每条航线，算出未来 N 周对应 weekday 的日期"""
    today = date.today()
    pairs = []
    for route in routes:
        weekday = route.get("weekday")  # 1=Mon, 5=Fri, 7=Sun
        # Python 的 weekday(): Mon=0..Sun=6；这里 1..7
        target_py_wd = weekday - 1
        days_to_first = (target_py_wd - today.weekday() + 7) % 7
        if days_to_first == 0:
            days_to_first = 7  # 永远算下一个，避免今天就是该 weekday
        first = today + timedelta(days=days_to_first)
        for w in range(weeks_ahead):
            pairs.append((route, first + timedelta(weeks=w)))
    return pairs


def export_for_frontend(storage: Storage, routes: list[dict], weeks_ahead: int, out_path: Path):
    """
    导出最近一次抓取结果为 JSON，供前端 HTML 直接读取
    """
    today = date.today()
    output = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "routes": [],
    }

    for route in routes:
        target_py_wd = route["weekday"] - 1
        days_to_first = (target_py_wd - today.weekday() + 7) % 7 or 7
        first_date = today + timedelta(days=days_to_first)

        weeks_data = []
        for w in range(weeks_ahead):
            d = first_date + timedelta(weeks=w)
            d_str = d.strftime("%Y-%m-%d")
            flights = storage.get_latest_flights(
                route["from_city"], route["to_city"], d_str
            )
            trains = storage.get_latest_trains(
                route["from_city"], route["to_city"], d_str
            )
            history = storage.get_price_history(
                route["from_city"], route["to_city"], d_str, days=30
            )
            weeks_data.append({
                "date": d_str,
                "weekday_label": ["一","二","三","四","五","六","日"][d.weekday()],
                "flights": flights,
                "trains": trains,
                "history": history,
            })

        output["routes"].append({
            "name": route["name"],
            "from_city": route["from_city"],
            "to_city": route["to_city"],
            "direction": route.get("direction", ""),
            "weeks": weeks_data,
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logging.info(f"导出前端数据: {out_path} ({out_path.stat().st_size} bytes)")


def main():
    setup_logging()
    log = logging.getLogger("main")
    log.info("=" * 60)
    log.info("京汉通勤监控启动")
    log.info("=" * 60)

    config = load_config()
    storage = Storage(ROOT / "data" / "prices.db")

    routes = config["routes"]
    weeks_ahead = config.get("monitor_weeks_ahead", 4)
    target_pairs = compute_target_dates(routes, weeks_ahead)

    log.info(f"本次抓取目标: {len(target_pairs)} 个 (路线 × 日期)")
    for route, d in target_pairs:
        log.info(f"  → {route['from_city']}→{route['to_city']} {d}")

    # ---- 抓机票 ----
    flights_by_route: dict[tuple, list[dict]] = {}

    if config["sources"].get("qunar"):
        with QunarFlightScraper(
            delay_range=(
                config["scraping"]["request_delay_min"],
                config["scraping"]["request_delay_max"],
            ),
            timeout=config["scraping"]["timeout"],
        ) as q:
            for route, d in target_pairs:
                offers = q.search(route["from_city"], route["to_city"], d)
                offer_dicts = [o.to_dict() for o in offers]
                if offer_dicts:
                    storage.save_flights(offer_dicts)
                key = (route["direction"], d.strftime("%Y-%m-%d"))
                flights_by_route.setdefault(key, []).extend(offer_dicts)

    if config["sources"].get("google_flights"):
        with GoogleFlightScraper(
            delay_range=(
                config["scraping"]["request_delay_min"],
                config["scraping"]["request_delay_max"],
            ),
            timeout=config["scraping"]["timeout"],
        ) as g:
            for route, d in target_pairs:
                offers = g.search(route["from_city"], route["to_city"], d)
                offer_dicts = [o.to_dict() for o in offers]
                if offer_dicts:
                    storage.save_flights(offer_dicts)
                key = (route["direction"], d.strftime("%Y-%m-%d"))
                flights_by_route.setdefault(key, []).extend(offer_dicts)

    # ---- 抓高铁 ----
    trains_by_route: dict[tuple, list[dict]] = {}
    if config["sources"].get("railway"):
        with RailwayScraper(timeout=config["scraping"]["timeout"]) as r:
            for route, d in target_pairs:
                offers = r.search(
                    from_city=route["from_city"],
                    to_city=route["to_city"],
                    depart_date=d,
                )
                offer_dicts = [o.to_dict() for o in offers]
                if offer_dicts:
                    storage.save_trains(offer_dicts)
                key = (route["direction"], d.strftime("%Y-%m-%d"))
                trains_by_route.setdefault(key, []).extend(offer_dicts)

    # ---- 提醒规则 ----
    engine = AlertEngine(config.get("alert_rules", []), storage=storage)
    alerts = engine.evaluate(flights_by_route, trains_by_route)

    if alerts:
        log.info(f"触发 {len(alerts)} 条告警")
        notifier = Notifier(config.get("notifiers", {}))
        for alert in alerts:
            notifier.send(alert.title, alert.content)
    else:
        log.info("本次无告警触发")

    # ---- 导出前端数据 ----
    export_for_frontend(
        storage, routes, weeks_ahead,
        ROOT / "docs" / "data.json",  # docs/ 目录便于 GitHub Pages 直接发布
    )

    # ---- 清理老数据 ----
    storage.cleanup_old(keep_days=90)

    log.info("=" * 60)
    log.info("本次任务完成")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
