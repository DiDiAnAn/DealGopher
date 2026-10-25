"""
抓取状态定义

把「价格」和「能不能买到」分开：只有拿到了某个具体 SKU 的确定价格，
才认为价格可信（status=ok）；无货、下架、规格失效、价格区间等情况
一律不写价格，只记状态。
"""

STATUS_OK = "ok"                    # 正常：抓到了当前选中 SKU 的确定价格
STATUS_NO_STOCK = "no_stock"        # 无货：选中规格售罄/缺货
STATUS_DELISTED = "delisted"        # 已下架：商品失效、页面不存在
STATUS_PRICE_RANGE = "price_range"  # 价格区间：页面显示的是"最低价起"而非确定的单 SKU 价
STATUS_SKU_MISMATCH = "sku_mismatch"  # 规格失效：记录的规格在页面上找不到或无法选中
STATUS_UNKNOWN = "unknown"          # 抓取异常：网络/解析失败，保留上次状态

ALL_STATUSES = (
    STATUS_OK,
    STATUS_NO_STOCK,
    STATUS_DELISTED,
    STATUS_PRICE_RANGE,
    STATUS_SKU_MISMATCH,
    STATUS_UNKNOWN,
)

STATUS_DISPLAY = {
    STATUS_OK: "监控中",
    STATUS_NO_STOCK: "无货",
    STATUS_DELISTED: "已下架",
    STATUS_PRICE_RANGE: "价格区间",
    STATUS_SKU_MISMATCH: "规格失效",
    STATUS_UNKNOWN: "抓取异常",
}

# 只有这些状态的价格才允许写入历史、参与达标判断与走势图
PRICE_TRUSTED_STATUSES = (STATUS_OK,)

# 这些状态表示"当前买不到"，可以用于补货提醒
UNAVAILABLE_STATUSES = (STATUS_NO_STOCK, STATUS_DELISTED, STATUS_SKU_MISMATCH)


def display(status: str) -> str:
    """状态的中文显示"""
    return STATUS_DISPLAY.get(status, "未知")


def is_price_trusted(status: str) -> bool:
    """该状态下的价格是否可以采信"""
    return status in PRICE_TRUSTED_STATUSES


def is_unavailable(status: str) -> bool:
    """该状态是否表示当前买不到"""
    return status in UNAVAILABLE_STATUSES
