"""
蹲价 · 数据上传模块（把抓取结果推送到云端看板）

设计要点：
- 只用标准库 urllib，不引入额外依赖（容器镜像更小）
- 上传失败不丢数据：写入本地队列文件，下次循环重试
- 幂等：同一条记录重复推送由服务端按 (url, timestamp) 去重

环境变量（由 docker-compose 注入）：
  PM_UPLOAD_URL    例如 http://lcgp.anan-nas.top/dunjia/api/push.php
  PM_UPLOAD_TOKEN  与服务端约定的密钥
"""
import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List

QUEUE_FILENAME = "upload_queue.json"


class Uploader:
    """把单条价格记录推送到云端"""

    def __init__(self, data_dir: str, url: str = "", token: str = "", timeout: int = 15):
        self.data_dir = data_dir
        self.url = (url or os.environ.get("PM_UPLOAD_URL", "")).strip()
        self.token = (token or os.environ.get("PM_UPLOAD_TOKEN", "")).strip()
        self.timeout = timeout
        self.queue_file = os.path.join(data_dir, QUEUE_FILENAME)
        self.enabled = bool(self.url)

    # ==================== 队列 ====================

    def _load_queue(self) -> List[Dict]:
        if not os.path.exists(self.queue_file):
            return []
        try:
            with open(self.queue_file, "r", encoding="utf-8-sig") as file:
                data = json.load(file)
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[Uploader] 读取队列失败（忽略）: {e}")
            return []

    def _save_queue(self, items: List[Dict]):
        try:
            # 只保留最近 500 条，防止无限增长
            items = items[-500:]
            with open(self.queue_file, "w", encoding="utf-8") as file:
                json.dump(items, file, ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"[Uploader] 保存队列失败: {e}")

    # ==================== 推送 ====================

    def _post(self, payload: Dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "Dunjia/1.6"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", "ignore").strip()
                ok = response.status == 200 and '"ok":true' in text.replace(" ", "").lower()
                if not ok:
                    print(f"[Uploader] 服务端返回异常: HTTP {response.status} {text[:200]}")
                return ok
        except urllib.error.HTTPError as e:
            print(f"[Uploader] 上传失败 HTTP {e.code}: {e.reason}")
            return False
        except Exception as e:
            print(f"[Uploader] 上传失败（网络）: {e}")
            return False

    def push(self, record: Dict) -> bool:
        """
        推送单条记录；失败则进队列等下次重试

        注意：队列里存的是**不含 token** 的记录，token 在发送时再注入。
        否则用户换了 token 后，队列里的旧记录会一直用旧 token 失败。
        """
        if not self.enabled:
            return False

        payload = dict(record)

        if self._post({**payload, "token": self.token}):
            return True

        queue = self._load_queue()
        queue.append(payload)
        self._save_queue(queue)
        return False

    def flush_queue(self, limit: int = 20) -> int:
        """尝试重发队列里的记录，返回成功条数"""
        if not self.enabled:
            return 0
        queue = self._load_queue()
        if not queue:
            return 0

        remaining, sent = [], 0
        for index, payload in enumerate(queue):
            if sent >= limit:
                remaining.extend(queue[index:])
                break
            if self._post({**payload, "token": self.token}):
                sent += 1
                time.sleep(0.1)
            else:
                # 一次失败就不再继续，避免网络抖动时刷屏
                remaining.extend(queue[index:])
                break

        self._save_queue(remaining)
        if sent:
            print(f"[Uploader] 补发成功 {sent} 条，队列剩余 {len(remaining)} 条")
        return sent

    def queue_size(self) -> int:
        return len(self._load_queue())


def build_record(product, price: float, original_price: float) -> Dict:
    """把一次抓取结果整理成上传用的字典"""
    expected = float(product.expected_price or 0)
    return {
        "url": product.url,
        "name": product.name or "",
        "platform": product.platform,
        "platform_name": product.platform_display_name(),
        "price": float(price),
        "original_price": float(original_price or 0),
        "expected_price": expected,
        "reached": bool(expected > 0 and price > 0 and price <= expected),
        "timestamp": product.last_refresh,
    }
