"""
12306 高铁余票抓取器

策略：
1. 直接调用 12306 公开的余票查询 API（kyfw.12306.cn/otn/leftTicket/queryO）
2. 必须先访问首页拿 cookies，否则接口会拒绝
3. 站点代码字典需要从 12306 自己的 JS 文件解析

价格说明：
- 北京西↔武汉 G 字头二等座固定 ¥520.5，一等座 ¥875.5，商务座 ¥1630.5
- 价格几乎不变，所以高铁监控的核心是「余票」而不是「价格」
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 北京/武汉相关站点代码（站名拼音首字母 + 内部代码）
# 注意：北京→武汉 G/D 字头高铁从北京西(BXP)发车，不是北京站(BJP)
STATION_CODES = {
    "北京": "BXP",   # G/D 动车组均从北京西发/到，查询用 BXP
    "北京西": "BXP",
    "北京南": "VNP",
    "北京丰台": "FYP",
    "北京站": "BJP",  # 普通列车才从北京站，保留备用
    "武汉": "WHN",
    "汉口": "HKN",
    "武昌": "WCN",
}

# 高铁固定价格（北京↔武汉，京广高铁）
FIXED_PRICES = {
    "second_class": 520,    # 二等座
    "first_class": 875,     # 一等座
    "business": 1630,       # 商务座
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@dataclass
class TrainOffer:
    source: str = "12306"
    from_city: str = ""
    to_city: str = ""
    depart_date: str = ""
    train_no: str = ""           # G79
    dep_station: str = ""
    arr_station: str = ""
    dep_time: str = ""           # 09:00
    arr_time: str = ""           # 13:14
    duration_min: int = 0
    second_class_price: int = 0
    first_class_price: int = 0
    business_price: int = 0
    second_class_seats: str = ""  # "有"/"少"/"15"/"无"
    first_class_seats: str = ""
    business_seats: str = ""
    captured_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class RailwayScraper:
    """12306 高铁查询"""

    INDEX_URL = "https://www.12306.cn/index/"
    QUERY_URL = "https://kyfw.12306.cn/otn/leftTicket/queryO"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
            },
            verify=False,  # 12306 证书有时候会有问题
        )
        self._initialized = False

    def _init_session(self):
        """访问首页获取必要的 cookies"""
        if self._initialized:
            return
        try:
            self.client.get(self.INDEX_URL)
            time.sleep(1)
            # 第二次访问余票查询页面，进一步丰富 cookies
            self.client.get("https://kyfw.12306.cn/otn/leftTicket/init")
            self._initialized = True
            logger.info("[12306] Session 初始化完成")
        except Exception as e:
            logger.warning(f"[12306] Session 初始化失败: {e}")

    def search(
        self,
        from_station: str = "BJP",
        to_station: str = "WHN",
        depart_date: Optional[date] = None,
        from_city: str = "北京",
        to_city: str = "武汉",
    ) -> list[TrainOffer]:
        """查询余票

        Args:
            from_station: 出发站三字码（默认 BJP=北京）
            to_station: 到达站三字码（默认 WHN=武汉）
            depart_date: 出发日期
        """
        if not depart_date:
            return []

        self._init_session()

        # 站点代码兜底
        from_station = STATION_CODES.get(from_city, from_station)
        to_station = STATION_CODES.get(to_city, to_station)

        params = {
            "leftTicketDTO.train_date": depart_date.strftime("%Y-%m-%d"),
            "leftTicketDTO.from_station": from_station,
            "leftTicketDTO.to_station": to_station,
            "purpose_codes": "ADULT",
        }

        for attempt in range(3):
            try:
                logger.info(
                    f"[12306] 查询 {from_city}({from_station})→{to_city}({to_station}) "
                    f"{depart_date} (尝试 {attempt+1}/3)"
                )
                resp = self.client.get(self.QUERY_URL, params=params)

                if resp.status_code != 200:
                    logger.warning(f"[12306] HTTP {resp.status_code}")
                    time.sleep(2)
                    continue

                data = resp.json()
                if data.get("status") and "data" in data:
                    raw_results = data["data"].get("result", [])
                    return self._parse_results(
                        raw_results,
                        depart_date.strftime("%Y-%m-%d"),
                        from_city,
                        to_city,
                    )
                else:
                    logger.warning(f"[12306] 返回异常: {data.get('messages')}")

            except Exception as e:
                logger.error(f"[12306] 查询失败: {e}")

            time.sleep(2)

        return []

    def _parse_results(
        self,
        raw_results: list[str],
        depart_date: str,
        from_city: str,
        to_city: str,
    ) -> list[TrainOffer]:
        """
        12306 的 result 是 | 分隔的字符串，每条对应一趟车次。
        字段顺序固定，索引位置参考 12306 前端 JS。
        """
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")

        offers: list[TrainOffer] = []

        for row in raw_results:
            fields = row.split("|")
            if len(fields) < 35:
                continue

            try:
                train_no = fields[3]  # 车次（如 G79）
                # 只关心高铁/动车
                if not train_no or train_no[0] not in ("G", "D", "C"):
                    continue

                offer = TrainOffer(
                    from_city=from_city,
                    to_city=to_city,
                    depart_date=depart_date,
                    train_no=train_no,
                    # fields[7]=乘客上车站电报码, fields[8]=乘客下车站电报码
                    dep_station=fields[7],
                    arr_station=fields[8],
                    # fields[9]=出发时间, fields[10]=到达时间, fields[11]=历时
                    dep_time=fields[9],
                    arr_time=fields[10],
                    duration_min=self._duration_to_min(fields[11]),
                    second_class_price=FIXED_PRICES["second_class"],
                    first_class_price=FIXED_PRICES["first_class"],
                    business_price=FIXED_PRICES["business"],
                    # 余票（"有"/"无"/具体数字）
                    # fields[31]=ze_num(二等座), fields[32]=zy_num(一等座), fields[33]=swz_num(商务座)
                    second_class_seats=fields[31] or "无",
                    first_class_seats=fields[32] or "无",
                    business_seats=fields[33] or "无",
                    captured_at=now,
                )
                offers.append(offer)
            except (IndexError, ValueError) as e:
                logger.debug(f"[12306] 跳过行: {e}")
                continue

        logger.info(f"[12306] 解析到 {len(offers)} 趟高铁/动车")
        return offers

    @staticmethod
    def _duration_to_min(s: str) -> int:
        """ '04:34' → 274 分钟 """
        m = re.match(r"(\d+):(\d+)", s)
        if not m:
            return 0
        return int(m.group(1)) * 60 + int(m.group(2))

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
