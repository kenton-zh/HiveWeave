"""Flower-name (花名) generator — pure function module.

契约 18: 花名生成
- 8 个风格池（poetic_single / nature_pairs / modern_short / bold / elegant /
  playful / three_char / four_char）
- generate_flower_name() 随机选池随机选名，返回 1-4 字 CJK 字符串
- is_flower_name(name) 校验 1-4 字 CJK（^[\u4e00-\u9fff]{1,4}$）
- generate_unique_flower_name(existing) 生成不重复花名
- 无 DB，纯函数模块

移植自 TS 源码 packages/shared/src/names.ts。
HR 可覆盖这些名字——这是默认的自动生成池。
"""

import random
import re

__all__ = [
    "generate_flower_name",
    "generate_flower_name_with_style",
    "is_flower_name",
    "generate_unique_flower_name",
    "POOLS",
]

# CJK Unified Ideographs 范围（契约 18: \u4e00-\u9fff，1-4 字）
_FLOWER_NAME_RE = re.compile(r"^[\u4e00-\u9fff]{1,4}$")


# ── 花名池（8 个风格）──────────────────────────────────────
# 移植自 packages/shared/src/names.ts

POETIC_SINGLE = [
    "寂", "默", "禅", "澈", "渡", "归", "隐", "逸",
    "止", "觉", "空", "宁", "静", "渊", "素", "简",
]

NATURE_PAIRS = [
    "霜月", "暮雨", "柳烟", "云溪", "海雾", "露华", "霞光",
    "风荷", "星野", "雪霁", "松风", "鹤影", "蝉鸣", "萤火",
    "春涧", "秋潭", "朝露", "夕岚", "山月", "水镜",
]

MODERN_SHORT = [
    "未央", "无邪", "半夏", "浮生", "初見", "长歌",
    "远行", "来迟", "不知", "可期", "如一", "重逢",
    "默存", "向晚", "拾光", "等风",
]

BOLD_NAMES = [
    "剑心", "破阵", "凌霄", "斩风", "逆鳞", "惊鸿",
    "铁衣", "燃灯", "铸骨", "擎天", "踏雪", "逐日",
    "断浪", "劈山", "疾风", "雷动",
]

ELEGANT_NAMES = [
    "寒露", "霜降", "白鹭", "紫烟", "青鸾", "碧落",
    "玄机", "灵犀", "锦瑟", "玉壶", "冰弦", "银烛",
    "瑶台", "琼枝", "画屏", "篆香",
]

PLAYFUL_NAMES = [
    "猫九", "鱼丸", "豆包", "汤圆", "栗子", "年糕",
    "橘子", "红枣", "芝麻", "花生", "糖瓜", "肉松",
    "小满", "大寒", "端午", "重阳",
]

THREE_CHAR_NAMES = [
    "风之子", "水中月", "镜中花", "云中鹤", "石上泉",
    "梦里身", "画外音", "局外人", "守夜人", "摆渡人",
    "半山居", "逍遥游", "无所有", "有所思",
]

FOUR_CHAR_NAMES = [
    "一蓑烟雨", "长风万里", "大漠孤烟", "流星赶月",
    "白驹过隙", "高山流水", "来日方长", "春风得意",
    "浮云一别", "人间草木",
]

# 所有池及其风格标签（先选池再选名，等概率）
POOLS: list[tuple[str, list[str]]] = [
    ("poetic", POETIC_SINGLE),
    ("nature", NATURE_PAIRS),
    ("modern", MODERN_SHORT),
    ("bold", BOLD_NAMES),
    ("elegant", ELEGANT_NAMES),
    ("playful", PLAYFUL_NAMES),
    ("poetic-3", THREE_CHAR_NAMES),
    ("poetic-4", FOUR_CHAR_NAMES),
]


def generate_flower_name() -> str:
    """随机生成一个花名（1-4 字 CJK）。

    从 8 个风格池中随机选 1 个池，再从该池中随机选 1 个名字。
    """
    _style, names = random.choice(POOLS)
    return random.choice(names)


def generate_flower_name_with_style() -> tuple[str, str]:
    """随机生成花名并返回 (name, style)。

    对齐 TS generateFlowerName 的返回结构，需要风格标签时使用。
    """
    style, names = random.choice(POOLS)
    return random.choice(names), style


def is_flower_name(name: str | None) -> bool:
    """判断 name 是否为花名格式（1-4 字 CJK Unified Ideographs）。

    - None / 非字符串 → False
    - 含非 CJK 字符（如 "CEO"/"HR"/英文名）→ False
    - 超过 4 字 → False
    """
    if not isinstance(name, str):
        return False
    return bool(_FLOWER_NAME_RE.match(name))


def generate_unique_flower_name(
    existing: set[str] | None = None,
    max_attempts: int = 1000,
) -> str:
    """生成一个不在 existing 集合中的花名。

    重试最多 max_attempts 次；若穷尽仍重复（池子太小），返回最后一次生成的花名。
    用于启动时花名迁移：确保新生成的花名不与现有 agent 重名。
    """
    existing = existing if existing is not None else set()
    name = generate_flower_name()
    attempts = 1
    while name in existing and attempts < max_attempts:
        name = generate_flower_name()
        attempts += 1
    return name
