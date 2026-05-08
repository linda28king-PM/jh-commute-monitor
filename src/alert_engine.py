"""
提醒规则引擎

输入：本次抓取结果 + 配置中的规则
输出：触发的告警列表（含格式化好的推送内容）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    rule_name: str
    title: str
    content: str
    payload: dict


class AlertEngine:
    def __init__(self, rules: list[dict], storage=None):
        self.rules = rules or []
        self.storage = storage

    def evaluate(
        self,
        flights_by_route: dict[tuple, list[dict]],
        trains_by_route: dict[tuple, list[dict]],
    ) -> list[Alert]:
        """
        Args:
            flights_by_route: {("out"/"back", date): [flight, ...]}
            trains_by_route:  同上
        Returns:
            触发的告警列表
        """
        alerts = []

        for rule in self.rules:
            if not rule.get("enabled", True):
                continue

            mode = rule.get("mode")          # flight / train / any
            direction = rule.get("direction") # out / back / any

            # 收集匹配此规则的全部候选
            candidates = []
            if mode in ("flight", "any"):
                for (dir_, date_), flights in flights_by_route.items():
                    if direction in (dir_, "any"):
                        candidates.extend([("flight", dir_, date_, f) for f in flights])
            if mode in ("train", "any"):
                for (dir_, date_), trains in trains_by_route.items():
                    if direction in (dir_, "any"):
                        candidates.extend([("train", dir_, date_, t) for t in trains])

            # 应用具体规则
            triggered = self._apply_rule(rule, candidates)
            if triggered:
                alert = self._build_alert(rule, triggered)
                alerts.append(alert)
                if self.storage:
                    self.storage.record_alert(rule["name"], alert.payload)

        return alerts

    def _apply_rule(self, rule: dict, candidates: list[tuple]) -> list[tuple]:
        """根据规则筛选候选，返回触发条目"""
        rule_type = rule.get("condition", "price_below")
        threshold = rule.get("threshold")

        triggered = []
        for kind, dir_, date_, item in candidates:
            if rule_type == "price_below":
                if kind == "flight":
                    price = item.get("price", 0)
                    if 0 < price <= threshold:
                        triggered.append((kind, dir_, date_, item, price))
                elif kind == "train":
                    price = item.get("second_class_price", 0)
                    if 0 < price <= threshold:
                        triggered.append((kind, dir_, date_, item, price))
            elif rule_type == "low_seats" and kind == "train":
                seats_str = str(item.get("second_class_seats", ""))
                seats_num = self._parse_seats(seats_str)
                threshold_seats = rule.get("threshold_seats", 20)
                if seats_num is not None and seats_num <= threshold_seats:
                    triggered.append((kind, dir_, date_, item, seats_num))

        return triggered

    @staticmethod
    def _parse_seats(s: str):
        """ "有"->999, "无"->0, "15"->15, "少"->10 """
        if not s or s == "无" or s == "--":
            return 0
        if s == "有":
            return 999
        try:
            return int(s)
        except ValueError:
            return None

    def _build_alert(self, rule: dict, triggered: list[tuple]) -> Alert:
        """格式化推送内容"""
        rule_name = rule["name"]
        cond = rule.get("condition", "price_below")

        # 取最优的几条
        if cond == "price_below":
            triggered.sort(key=lambda x: x[4])  # 按价格升序
            best_n = triggered[:5]
        else:
            best_n = triggered[:5]

        lines = [f"## 🔔 {rule_name}", ""]

        for kind, dir_, date_, item, metric in best_n:
            dir_text = "周五去程" if dir_ == "out" else "周日回程" if dir_ == "back" else dir_
            if kind == "flight":
                lines.append(
                    f"- **{dir_text}** {date_} | "
                    f"{item.get('carrier', '')} {item.get('flight_no', '')} "
                    f"{item.get('dep_time', '')} → {item.get('arr_time', '')} "
                    f"| **¥{item.get('price', 0)}**"
                )
            else:
                seats = item.get("second_class_seats", "")
                lines.append(
                    f"- **{dir_text}** {date_} | "
                    f"{item.get('train_no', '')} "
                    f"{item.get('dep_time', '')} → {item.get('arr_time', '')} "
                    f"| 二等座 ¥{item.get('second_class_price', 0)} "
                    f"| 余票 {seats}"
                )

        lines.append("")
        lines.append(f"_共触发 {len(triggered)} 条结果_")

        title = f"[京汉通勤] {rule_name}（{len(triggered)}条）"
        content = "\n".join(lines)

        return Alert(
            rule_name=rule_name,
            title=title,
            content=content,
            payload={
                "rule": rule,
                "triggered_count": len(triggered),
                "samples": [{
                    "kind": k, "direction": d, "date": dt,
                    "item": item, "metric": m
                } for k, d, dt, item, m in best_n],
            },
        )
