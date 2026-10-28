"""
数据模型与存储
"""
import json
import os
from datetime import datetime, timedelta
from typing import List, Optional

from paths import DATA_DIR
from platforms import detect_platform, display_name
import status as st


class PriceRecord:
    """单条价格记录（status 非 ok 时 price 为 0，仅记录状态）"""

    def __init__(self, price: float, original_price: float = 0.0,
                 timestamp: str = "", status: str = st.STATUS_OK,
                 status_text: str = "", sku_text: str = ""):
        self.price = price
        self.original_price = original_price
        self.timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status = status or st.STATUS_OK
        self.status_text = status_text
        self.sku_text = sku_text

    @property
    def status_display(self) -> str:
        return st.display(self.status)

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "original_price": self.original_price,
            "timestamp": self.timestamp,
            "status": self.status,
            "status_text": self.status_text,
            "sku_text": self.sku_text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PriceRecord":
        return cls(
            price=data.get("price", 0.0),
            original_price=data.get("original_price", 0.0),
            timestamp=data.get("timestamp", ""),
            status=data.get("status", st.STATUS_OK),
            status_text=data.get("status_text", ""),
            sku_text=data.get("sku_text", ""),
        )


class Product:
    """商品模型"""

    def __init__(
        self,
        url: str = "",
        name: str = "",
        platform: str = "",
        expected_price: float = 0.0,
        refresh_mode: str = "daily",
        refresh_time: str = "10:00",
        refresh_interval: int = 60,
        enabled: bool = True,
    ):
        self.url = url
        self.name = name
        self.platform = platform or self._detect_platform(url)
        self.expected_price = expected_price
        self.refresh_mode = refresh_mode          # "daily" 或 "interval"
        self.refresh_time = refresh_time          # daily模式: "HH:MM"
        self.refresh_interval = refresh_interval  # interval模式: 间隔分钟数
        self.enabled = enabled
        self.current_price: float = 0.0
        self.original_price: float = 0.0
        self.last_refresh: str = ""
        self.price_history: List[PriceRecord] = []

        # ---- 库存/上下架状态（与价格分离）----
        self.last_status: str = st.STATUS_OK        # 最近一次抓取的状态
        self.last_status_text: str = ""             # 页面原文，便于排查
        self.last_status_time: str = ""             # 状态时间
        self.last_valid_price: float = 0.0          # 最近一次可信价格
        self.last_valid_time: str = ""              # 最近一次可信价格的时间

        # ---- 规格锁定：要监控的具体版本/套餐 ----
        self.sku_spec: List[str] = []               # 例如 ["版本类型=中国大陆", "套餐=单机标配"]
        self.sku_recorded_at: str = ""              # 规格采集时间
        # 是否允许自动识别并锁定规格（首次抓取时按链接里已选好的版本自动记录）
        self.sku_auto: bool = True
        # 连续规格失效次数：达到阈值后自动清除锁定，改为重新识别，
        # 避免页面改版/促销文案变化后永久卡在"规格失效"
        self.sku_fail_count: int = 0

    def _detect_platform(self, url: str) -> str:
        """按域名识别平台（规则集中在 platforms.py）"""
        return detect_platform(url)

    def platform_display_name(self) -> str:
        return display_name(self.platform)

    def status_display(self) -> str:
        """当前状态的中文显示（无货/已下架等）"""
        return st.display(self.last_status)

    def is_available(self) -> bool:
        """当前是否处于可购买状态"""
        return self.last_status == st.STATUS_OK

    def has_sku_lock(self) -> bool:
        return bool(self.sku_spec)

    def sku_display(self) -> str:
        """规格的人类可读文本"""
        return " / ".join(self.sku_spec) if self.sku_spec else "(未锁定规格)"

    def add_price_record(self, price: float, original_price: float = 0.0,
                         status: str = st.STATUS_OK, status_text: str = "",
                         sku_text: str = "") -> None:
        """
        写入一条抓取记录

        注意：只有 status=ok 时才更新当前价格；其他状态（无货/下架/规格失效）
        只记录状态、不覆盖价格，避免把"没货"当成"降价"写进历史。
        last_refresh 在每次抓取后都会更新（否则无货商品会被调度器反复重试）。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_refresh = now
        self.last_status = status or st.STATUS_UNKNOWN
        self.last_status_text = status_text or ""
        self.last_status_time = now

        if st.is_price_trusted(self.last_status) and price > 0:
            self.current_price = price
            self.original_price = original_price
            self.last_valid_price = price
            self.last_valid_time = now

        self.price_history.append(
            PriceRecord(
                price=price if st.is_price_trusted(self.last_status) else 0.0,
                original_price=original_price if st.is_price_trusted(self.last_status) else 0.0,
                status=self.last_status,
                status_text=self.last_status_text,
                sku_text=sku_text,
            )
        )
        if len(self.price_history) > 500:
            self.price_history = self.price_history[-500:]

    def is_price_reached(self) -> bool:
        """是否达到预期价（仅在价格可信、且在售时判断）"""
        if self.expected_price <= 0 or self.current_price <= 0:
            return False
        if self.last_status != st.STATUS_OK:
            return False
        return self.current_price <= self.expected_price

    def get_refresh_time_display(self) -> str:
        """获取刷新时间的显示文本"""
        if self.refresh_mode == "daily":
            return self.refresh_time
        else:
            minutes = self.refresh_interval
            if minutes >= 60 and minutes % 60 == 0:
                hours = minutes // 60
                return f"每{hours}小时"
            elif minutes >= 60:
                hours = minutes // 60
                mins = minutes % 60
                return f"每{hours}小时{mins}分钟"
            else:
                return f"每{minutes}分钟"

    def get_next_refresh_time(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """
        计算下次应该刷新的时间

        说明：调度器（MainWindow._check_and_fetch）与界面"下次刷新倒计时"列
        都只调用本方法，保证显示的刷新时间和实际刷新行为完全一致。

        - daily 模式：以 last_refresh 为基准，取其后第一个 "HH:MM"
        - interval 模式：last_refresh + 间隔分钟数
        - last_refresh 为空或格式非法：返回 now，表示立即到期
          （否则新添加/首次抓取失败的商品会永远被调度器跳过）

        now 参数用于让调用方注入同一个"当前时间"，避免 is_due() 内部
        再次取时间造成微秒级偏差、把"立即到期"误判成"未到期"。
        """
        now = now or datetime.now()

        if not self.last_refresh:
            return now

        try:
            last_dt = datetime.strptime(self.last_refresh, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return now

        if self.refresh_mode == "daily":
            try:
                h, m = map(int, self.refresh_time.split(":"))
            except (ValueError, AttributeError):
                # 刷新时间配置损坏，退化为按间隔刷新，避免商品永久不再刷新
                interval = self.refresh_interval if self.refresh_interval > 0 else 60
                return last_dt + timedelta(minutes=interval)

            target = last_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= last_dt:
                target += timedelta(days=1)
            return target

        interval = self.refresh_interval if self.refresh_interval > 0 else 1
        return last_dt + timedelta(minutes=interval)

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """
        当前是否已到该刷新的时刻（调度器的唯一判定入口）

        与 get_next_refresh_time() 共用同一套逻辑，避免出现
        "界面显示明早 8 点刷新、实际每 3 小时刷一次" 的不一致。
        """
        if not self.enabled:
            return False

        now = now or datetime.now()
        next_time = self.get_next_refresh_time(now)
        if next_time is None:
            return False
        return next_time <= now

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "name": self.name,
            "platform": self.platform,
            "expected_price": self.expected_price,
            "refresh_mode": self.refresh_mode,
            "refresh_time": self.refresh_time,
            "refresh_interval": self.refresh_interval,
            "enabled": self.enabled,
            "current_price": self.current_price,
            "original_price": self.original_price,
            "last_refresh": self.last_refresh,
            "last_status": self.last_status,
            "last_status_text": self.last_status_text,
            "last_status_time": self.last_status_time,
            "last_valid_price": self.last_valid_price,
            "last_valid_time": self.last_valid_time,
            "sku_spec": list(self.sku_spec),
            "sku_recorded_at": self.sku_recorded_at,
            "sku_auto": self.sku_auto,
            "sku_fail_count": self.sku_fail_count,
            "price_history": [r.to_dict() for r in self.price_history],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Product":
        p = cls(
            url=data.get("url", ""),
            name=data.get("name", ""),
            platform=data.get("platform", ""),
            expected_price=data.get("expected_price", 0.0),
            refresh_mode=data.get("refresh_mode", "daily"),
            refresh_time=data.get("refresh_time", "10:00"),
            refresh_interval=data.get("refresh_interval", 60),
            enabled=data.get("enabled", True),
        )
        p.current_price = data.get("current_price", 0.0)
        p.original_price = data.get("original_price", 0.0)
        p.last_refresh = data.get("last_refresh", "")
        # 旧数据没有以下字段，给默认值保证兼容
        p.last_status = data.get("last_status", st.STATUS_OK)
        p.last_status_text = data.get("last_status_text", "")
        p.last_status_time = data.get("last_status_time", "")
        p.last_valid_price = data.get("last_valid_price", p.current_price)
        p.last_valid_time = data.get("last_valid_time", data.get("last_refresh", ""))
        p.sku_spec = list(data.get("sku_spec", []) or [])
        p.sku_recorded_at = data.get("sku_recorded_at", "")
        p.sku_auto = bool(data.get("sku_auto", True))
        p.sku_fail_count = int(data.get("sku_fail_count", 0) or 0)
        p.price_history = [
            PriceRecord.from_dict(r) for r in data.get("price_history", [])
        ]
        return p


class DataStore:
    """数据持久化存储"""

    def __init__(self, data_dir: str = ""):
        # 注意：下面必须统一用 self.data_dir，不能再用参数 data_dir
        # （默认值是空串，os.makedirs("") 会抛 FileNotFoundError）
        self.data_dir = data_dir or DATA_DIR
        self.products_file = os.path.join(self.data_dir, "products.json")
        self.config_file = os.path.join(self.data_dir, "config.json")
        os.makedirs(self.data_dir, exist_ok=True)
        # 加载失败标记：避免"读取失败→商品为空→再被空列表覆盖"导致数据丢失
        self._load_failed = False

    def load_products(self) -> List[Product]:
        if not os.path.exists(self.products_file):
            return []
        try:
            # utf-8-sig 可兼容记事本另存为带 BOM 的文件；
            # 用 utf-8 严格读时 BOM 会让 json 解析失败，商品会被静默清空
            with open(self.products_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            self._load_failed = False
            return [Product.from_dict(item) for item in data]
        except Exception as e:
            self._load_failed = True
            print(f"[DataStore] 加载商品数据失败（为避免覆盖，本次不会保存）: {e}")
            return []

    def save_products(self, products: List[Product]):
        if self._load_failed and not products:
            print("[DataStore] 上次加载失败且当前商品为空，跳过保存以保护原文件")
            return
        try:
            data = [p.to_dict() for p in products]
            with open(self.products_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[DataStore] 保存商品数据失败: {e}")

    def load_config(self) -> dict:
        if not os.path.exists(self.config_file):
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config(self, config: dict):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[DataStore] 保存配置失败: {e}")