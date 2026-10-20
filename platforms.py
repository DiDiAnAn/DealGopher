"""
支持的购物平台定义（单一事实来源）

新增一个平台只需：
1. 在 PLATFORMS 里加一项（域名、登录页、家庭页、登录 Cookie 特征）
2. 在 browser_engine 里补一个解析器（_parse_xxx）
3. 在 browser_engine.PRICE_SELECTORS / NAME_SELECTORS / ORIGINAL_SELECTORS 里补选择器

注意：淘宝与天猫共用同一套解析器（页面结构一致），所以是"4 个平台 / 3 套解析器"。
"""
from typing import Dict, List

PLATFORMS: Dict[str, dict] = {
    "taobao": {
        "display": "淘宝",
        "domains": ["taobao.com", "tb.cn"],
        "login_url": "https://login.taobao.com/member/login.jhtml",
        "home_url": "https://www.taobao.com",
        "login_indicators": ["www.taobao.com", "i.taobao.com", "login.taobao.com"],
        "login_cookies": ["_tb_token_", "cookie2", "sgcookie"],
    },
    "tmall": {
        "display": "天猫",
        "domains": ["tmall.com"],
        "login_url": "https://login.tmall.com/",
        "home_url": "https://www.tmall.com",
        "login_indicators": ["www.tmall.com", "login.tmall.com"],
        "login_cookies": ["_tb_token_", "cookie2", "sgcookie"],
    },
    "jd": {
        "display": "京东",
        "domains": ["jd.com", "jd.hk"],
        "login_url": "https://passport.jd.com/new/login.aspx",
        "home_url": "https://www.jd.com",
        "login_indicators": ["www.jd.com", "passport.jd.com"],
        "login_cookies": ["pin", "wskey", "thor"],
    },
    "pdd": {
        "display": "拼多多",
        # 拼多多的商品页在移动端域名下（mobile.yangkeduo.com），PC 端页面几乎不可用
        "domains": ["pinduoduo.com", "yangkeduo.com"],
        "login_url": "https://mobile.yangkeduo.com/login.html",
        "home_url": "https://mobile.yangkeduo.com/",
        "login_indicators": ["yangkeduo.com", "pinduoduo.com"],
        "login_cookies": ["PDDAccessToken", "pdd_user_id", "api_uid"],
    },
    "douyin": {
        "display": "抖音商城",
        "domains": ["douyin.com", "jinritemai.com"],
        "login_url": "https://www.douyin.com/",
        "home_url": "https://www.douyin.com/",
        "login_indicators": ["douyin.com", "jinritemai.com"],
        "login_cookies": ["sessionid", "sid_tt", "ttwid"],
    },
}


def detect_platform(url: str) -> str:
    """按域名识别平台，识别不了返回 "unknown" """
    url_lower = (url or "").lower()
    for name, spec in PLATFORMS.items():
        for domain in spec["domains"]:
            if domain in url_lower:
                return name
    return "unknown"


def display_name(platform: str) -> str:
    """平台的中文显示名"""
    return PLATFORMS.get(platform, {}).get("display", platform)


def all_domains() -> List[str]:
    """所有支持的域名（用于添加商品时的链接校验）"""
    domains: List[str] = []
    for spec in PLATFORMS.values():
        domains.extend(spec["domains"])
    return domains


def display_names() -> List[str]:
    """所有平台中文名（用于登录对话框下拉框）"""
    return [spec["display"] for spec in PLATFORMS.values()]


def name_of_display(display: str) -> str:
    """中文显示名 → 平台标识"""
    for name, spec in PLATFORMS.items():
        if spec["display"] == display:
            return name
    return ""
