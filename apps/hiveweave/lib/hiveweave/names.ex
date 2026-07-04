defmodule HiveWeave.Names do
  @moduledoc """
  Flower-name (花名) generation and validation.

  Mirrors the TS reference implementation in `packages/shared/src/names.ts`.
  Flower names are 1–4 character Chinese poetic nicknames. Used at startup to
  migrate legacy CEO/HR agents that still carry a non-flower display name.
  """

  # ── Name pools by style ─────────────────────────────────────

  @poetic_single [
    "寂", "默", "禅", "澈", "渡", "归", "隐", "逸",
    "止", "觉", "空", "宁", "静", "渊", "素", "简"
  ]

  @nature_pairs [
    "霜月", "暮雨", "柳烟", "云溪", "海雾", "露华", "霞光",
    "风荷", "星野", "雪霁", "松风", "鹤影", "蝉鸣", "萤火",
    "春涧", "秋潭", "朝露", "夕岚", "山月", "水镜"
  ]

  @modern_short [
    "未央", "无邪", "半夏", "浮生", "初見", "长歌",
    "远行", "来迟", "不知", "可期", "如一", "重逢",
    "默存", "向晚", "拾光", "等风"
  ]

  @bold_names [
    "剑心", "破阵", "凌霄", "斩风", "逆鳞", "惊鸿",
    "铁衣", "燃灯", "铸骨", "擎天", "踏雪", "逐日",
    "断浪", "劈山", "疾风", "雷动"
  ]

  @elegant_names [
    "寒露", "霜降", "白鹭", "紫烟", "青鸾", "碧落",
    "玄机", "灵犀", "锦瑟", "玉壶", "冰弦", "银烛",
    "瑶台", "琼枝", "画屏", "篆香"
  ]

  @playful_names [
    "猫九", "鱼丸", "豆包", "汤圆", "栗子", "年糕",
    "橘子", "红枣", "芝麻", "花生", "糖瓜", "肉松",
    "小满", "大寒", "端午", "重阳"
  ]

  @three_char_names [
    "风之子", "水中月", "镜中花", "云中鹤", "石上泉",
    "梦里身", "画外音", "局外人", "守夜人", "摆渡人",
    "半山居", "逍遥游", "无所有", "有所思"
  ]

  @four_char_names [
    "一蓑烟雨", "长风万里", "大漠孤烟", "流星赶月",
    "白驹过隙", "高山流水", "来日方长", "春风得意",
    "浮云一别", "人间草木"
  ]

  @all_pools [
    @poetic_single,
    @nature_pairs,
    @modern_short,
    @bold_names,
    @elegant_names,
    @playful_names,
    @three_char_names,
    @four_char_names
  ]

  @doc """
  Generate a random flower name from the style pools.
  """
  def generate_flower_name do
    @all_pools
    |> Enum.random()
    |> Enum.random()
  end

  @doc """
  Check whether a name looks like a flower name: 1–4 CJK Unified Ideographs.

  Mirrors the TS `isFlowerName`. Non-Chinese names (e.g. "CEO", "HR", or an
  English display name) return false and should be migrated.
  """
  def is_flower_name?(nil), do: false

  def is_flower_name?(name) when is_binary(name) do
    String.match?(name, ~r/\A[\x{4e00}-\x{9fff}]{1,4}\z/u)
  end

  def is_flower_name?(_), do: false
end
