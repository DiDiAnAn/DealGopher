"""
价格记录导出（Excel / CSV）

设计要点：
1. 数据源是内存中的 Product / PriceRecord，**不读取界面控件**
   （旧实现遍历 QTableWidget 单元格取 text()，导致导出的是 "¥9498.00" 这种字符串）
2. 价格写成数值 + 人民币数字格式，Excel 里可直接求和、排序、画趋势图
3. 时间写成真正的日期时间类型，可按月份分组/做透视表
4. 单工作表输出（按需求不拆多 sheet）
"""
import csv
import os
from datetime import datetime
from typing import List

import status as st

HEADERS = [
    "序号", "商品名称", "平台", "商品链接", "刷新时间", "状态",
    "当前价格", "原价", "预期价格", "是否达标", "规格",
]

# ¥#,##0.00 需要把 ¥ 用引号包起来，否则 openpyxl 会当成格式码
PRICE_FORMAT = '"¥"#,##0.00'
TIME_FORMAT = "yyyy-mm-dd hh:mm:ss"

COLUMN_WIDTHS = {
    "A": 8,   # 序号
    "B": 38,  # 商品名称
    "C": 10,  # 平台
    "D": 44,  # 商品链接
    "E": 20,  # 刷新时间
    "F": 10,  # 状态
    "G": 13,  # 当前价格
    "H": 13,  # 原价
    "I": 13,  # 预期价格
    "J": 10,  # 是否达标
    "K": 30,  # 规格
}

HEADER_FILL_COLOR = "34495E"
ALT_FILL_COLOR = "F8F9FA"
BORDER_COLOR = "D5D8DC"
REACHED_COLOR = "C0392B"
LINK_COLOR = "1F6FEB"
UNAVAILABLE_COLOR = "7F8C8D"

SHEET_TITLE = "价格记录"
LAST_COLUMN = "K"


def _parse_time(text: str):
    """把 "YYYY-MM-DD HH:MM:SS" 解析成 datetime，失败返回 None"""
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def collect_records(products) -> List[dict]:
    """
    把各商品的价格历史摊平成记录列表（按时间升序）

    非 ok 状态的记录也会保留（无货/下架/规格失效），但价格为 0，
    导出时价格列留空、状态列写明原因。
    """
    records = []
    for product in products:
        expected = float(product.expected_price or 0)
        for record in product.price_history:
            record_status = getattr(record, "status", st.STATUS_OK) or st.STATUS_OK
            trusted = st.is_price_trusted(record_status)
            price = float(record.price or 0) if trusted else 0.0
            original = float(record.original_price or 0) if trusted else 0.0
            records.append({
                "name": product.name or "(未获取)",
                "platform": product.platform_display_name(),
                "url": product.url,
                "time": _parse_time(record.timestamp),
                "time_text": record.timestamp,
                "status": record_status,
                "status_text": st.display(record_status),
                "sku_text": getattr(record, "sku_text", "") or "",
                "price": price,
                "original_price": original,
                "expected_price": expected,
                "reached": bool(trusted and expected > 0 and price > 0 and price <= expected),
            })

    records.sort(key=lambda r: (r["time"] or datetime.min, r["time_text"]))
    return records


def _price_value(value: float):
    """价格 <= 0 视为没有数据，写空单元格而不是 0"""
    return value if value and value > 0 else None


def write_xlsx(products, path: str) -> int:
    """
    写出 Excel 工作簿，返回写入的记录条数

    文件被占用（多为 Excel 正打开）时抛出 PermissionError，由调用方提示用户。
    openpyxl 在函数内导入：CSV 导出不依赖它。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    records = collect_records(products)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_TITLE

    header_font = Font(name="Microsoft YaHei", bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color=HEADER_FILL_COLOR,
                              end_color=HEADER_FILL_COLOR, fill_type="solid")
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center", wrap_text=False)
    thin = Side(style="thin", color=BORDER_COLOR)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    alt_fill = PatternFill(start_color=ALT_FILL_COLOR,
                           end_color=ALT_FILL_COLOR, fill_type="solid")
    body_font = Font(name="Microsoft YaHei", size=10)
    reached_font = Font(name="Microsoft YaHei", size=10, bold=True, color=REACHED_COLOR)
    link_font = Font(name="Microsoft YaHei", size=9, color=LINK_COLOR, underline="single")

    # ---- 表头 ----
    for column, header in enumerate(HEADERS, 1):
        cell = sheet.cell(row=1, column=column, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border
    sheet.row_dimensions[1].height = 22

    # ---- 数据行 ----
    for row_index, record in enumerate(records, start=2):
        trusted = st.is_price_trusted(record["status"])
        values = [
            row_index - 1,
            record["name"],
            record["platform"],
            record["url"],
            record["time"] or record["time_text"],
            record["status_text"],
            _price_value(record["price"]) if trusted else None,
            _price_value(record["original_price"]) if trusted else None,
            _price_value(record["expected_price"]),
            "是" if record["reached"] else "",
            record["sku_text"],
        ]
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row=row_index, column=column, value=value)
            cell.font = body_font
            cell.border = border
            cell.alignment = left if column in (2, 4, 11) else center
            if row_index % 2 == 1:
                cell.fill = alt_fill

        for column in (7, 8, 9):
            sheet.cell(row=row_index, column=column).number_format = PRICE_FORMAT

        if record["time"] is not None:
            sheet.cell(row=row_index, column=5).number_format = TIME_FORMAT

        if record["url"]:
            url_cell = sheet.cell(row=row_index, column=4)
            url_cell.hyperlink = record["url"]
            url_cell.font = link_font

        # 不可购买状态整行灰显，便于一眼区分
        if not trusted:
            for column in (6, 7, 10):
                sheet.cell(row=row_index, column=column).font = Font(
                    name="Microsoft YaHei", size=10, color=UNAVAILABLE_COLOR)

        if record["reached"]:
            sheet.cell(row=row_index, column=7).font = reached_font
            sheet.cell(row=row_index, column=10).font = reached_font

    # ---- 版式 ----
    for letter, width in COLUMN_WIDTHS.items():
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = "A2"
    if records:
        sheet.auto_filter.ref = f"A1:{LAST_COLUMN}{len(records) + 1}"

    workbook.save(path)
    return len(records)


def write_csv(products, path: str) -> int:
    """写出 CSV（utf-8-sig 带 BOM，Excel 双击打开中文不乱码），返回记录条数"""
    records = collect_records(products)
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(HEADERS)
        for index, record in enumerate(records, 1):
            trusted = st.is_price_trusted(record["status"])
            writer.writerow([
                index,
                record["name"],
                record["platform"],
                record["url"],
                record["time_text"],
                record["status_text"],
                f"{record['price']:.2f}" if trusted and record["price"] > 0 else "",
                f"{record['original_price']:.2f}" if trusted and record["original_price"] > 0 else "",
                f"{record['expected_price']:.2f}" if record["expected_price"] > 0 else "",
                "是" if record["reached"] else "",
                record["sku_text"],
            ])
    return len(records)


def export_records(products, path: str, csv_format: bool = False) -> int:
    """按目标文件扩展名（或显式 csv_format）选择导出格式"""
    if csv_format or path.lower().endswith(".csv"):
        return write_csv(products, path)
    return write_xlsx(products, path)
