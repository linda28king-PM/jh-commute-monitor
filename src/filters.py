"""
航班过滤器

读取路线配置里的 exclude 规则，从航班列表中剔除不想要的班次。
规则格式（config.yaml）：
  exclude:
    - arr_airport: "PKX"        # 到达机场码（可选）
      arr_time_after: "22:00"   # 到达时刻晚于此值（HH:MM，可选）
      dep_time_after: "23:00"   # 出发时刻晚于此值（HH:MM，可选）

同一条规则内多个字段是 AND 关系；
多条 exclude 规则之间是 OR 关系（任一匹配则排除）。
"""

from __future__ import annotations
import logging

log = logging.getLogger(__name__)


def _matches(flight: dict, rule: dict) -> bool:
    """判断 flight 是否满足 rule 中所有条件（全满足才排除）"""

    if "arr_airport" in rule:
        if flight.get("arr_airport", "") != rule["arr_airport"]:
            return False

    if "dep_airport" in rule:
        if flight.get("dep_airport", "") != rule["dep_airport"]:
            return False

    if "arr_time_after" in rule:
        arr = flight.get("arr_time", "") or ""
        # HH:MM 字符串直接比较即可（同格式下字典序 == 时间序）
        if not arr or arr <= rule["arr_time_after"]:
            return False

    if "dep_time_after" in rule:
        dep = flight.get("dep_time", "") or ""
        if not dep or dep <= rule["dep_time_after"]:
            return False

    return True  # 所有条件都满足 → 命中排除规则


def apply_route_filters(flights: list[dict], route: dict) -> list[dict]:
    """
    对 flights 列表应用 route 中的 exclude 规则，返回过滤后的列表。
    原列表不修改。
    """
    rules = route.get("exclude") or []
    if not rules:
        return flights

    kept, dropped = [], 0
    for f in flights:
        if any(_matches(f, rule) for rule in rules):
            dropped += 1
            log.debug(
                f"[Filter] 排除: {f.get('flight_no')} "
                f"{f.get('dep_time')}→{f.get('arr_time')} "
                f"{f.get('dep_airport')}→{f.get('arr_airport')}"
            )
        else:
            kept.append(f)

    if dropped:
        log.info(
            f"[Filter] 路线「{route.get('name', '')}」: "
            f"共 {len(flights)} 班，排除 {dropped} 班，保留 {len(kept)} 班"
        )
    return kept
