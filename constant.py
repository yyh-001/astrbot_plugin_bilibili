import os
from typing import Dict

CURRENT_DIR = os.path.dirname(__file__)
ASSETS_DIR = os.path.join(CURRENT_DIR, "assets")


def _asset_path(*parts: str) -> str:
    return os.path.join(ASSETS_DIR, *parts)


LOGO_PATH = _asset_path("Astrbot.png")
BANNER_PATH = _asset_path("banner.png")
BV = r"(?:\?.*)?(?:https?:\/\/)?(?:www\.)?(?:bilibili\.com\/video\/(BV[a-zA-Z0-9]+)|b23\.tv\/([a-zA-Z0-9]+))\/?(?:\?.*)?|BV[a-zA-Z0-9]+"
VALID_FILTER_TYPES = {
    "forward",
    "lottery",
    "video",
    "article",
    "draw",
    "live",
    "forward_lottery",
}
LIVE_ATALL_OPTION = "live_atall"
VALID_SUB_OPTIONS = {LIVE_ATALL_OPTION}
DATA_PATH = "data/astrbot_plugin_bilibili.json"
DEFAULT_CFG = {
    "bili_sub_list": {},  # sub_user -> [{"uid": "uid", "last": "last_dynamic_id", ...}]
    "credential": None,
}

# ==================== 模板注册表 ====================
# 集中管理所有可用的卡片模板
# 添加新模板只需在此处注册即可

CARD_TEMPLATES: Dict[str, dict] = {
    "template_1": {
        "name": "经典风格",
        "description": "原版设计",
        "file": "template_1.html",
        "path": _asset_path("template_1.html"),
    },
    "template_2": {
        "name": "B站粉风格",
        "description": "B站风格设计",
        "file": "template_2.html",
        "path": _asset_path("template_2.html"),
    },
    "simple": {
        "name": "简约风格",
        "description": "简洁现代的设计",
        "file": "template_simple.html",
        "path": _asset_path("template_simple.html"),
    },
}

# 默认模板
DEFAULT_TEMPLATE = "template_2"


def get_template_path(style: str) -> str:
    """获取指定样式的模板路径"""
    template = CARD_TEMPLATES.get(style, CARD_TEMPLATES[DEFAULT_TEMPLATE])
    return template["path"]


def get_template_names() -> list:
    """获取所有模板的 ID 列表"""
    return list(CARD_TEMPLATES.keys())


MAX_ATTEMPTS = 3
RETRY_DELAY = 2
RECENT_DYNAMIC_CACHE = 4

category_mapping = {
    "全部": "ALL",
    "原创": "ORIGINAL",
    "漫画改": "COMIC",
    "小说改": "NOVEL",
    "游戏改": "GAME",
    "特摄": "TOKUSATSU",
    "布袋戏": "BUDAIXI",
    "热血": "WARM",
    "穿越": "TIMEBACK",
    "奇幻": "IMAGING",
    "战斗": "WAR",
    "搞笑": "FUNNY",
    "日常": "DAILY",
    "科幻": "SCIENCE_FICTION",
    "萌系": "MOE",
    "治愈": "HEAL",
    "校园": "SCHOOL",
    "儿童": "CHILDREN",
    "泡面": "NOODLES",
    "恋爱": "LOVE",
    "少女": "GIRLISH",
    "魔法": "MAGIC",
    "冒险": "ADVENTURE",
    "历史": "HISTORY",
    "架空": "ALTERNATE",
    "机战": "MACHINE_BATTLE",
    "神魔": "GODS_DEM",
    "声控": "VOICE",
    "运动": "SPORT",
    "励志": "INSPIRATION",
    "音乐": "MUSIC",
    "推理": "ILLATION",
    "社团": "SOCIEITES",
    "智斗": "OUTWIT",
    "催泪": "TEAR",
    "美食": "FOOD",
    "偶像": "IDOL",
    "乙女": "OTOME",
    "职场": "WORK",
}
