"""件3（agent→用户发图 2026-09-05）单元测试：message_user 的 images 参数。

落库路径取证：message_user 直接写 chat_messages（用户 Chat 面板消息源），
不经 inbox 中转 —— images 直接落该消息的 images 列（前端 MessageBubble
已支持渲染），并同步进 WebSocket 推送。

报告截图直传（2026-09-05）：`.hiveweave/reports/` 下验收截图路径由平台
读文件转 data URL 内联（CEO 无 bash/SOURCE_WRITE，转不了 base64）。
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hiveweave.tools.misc_tools import MessageUserParams, message_user_tool

_SMALL_IMG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
_SMALL_IMG2 = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="

# 伪 PNG（魔数头 + 填充）——本层不校验图片魔数，前端 <img> 才消费
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-payload"


@pytest.fixture
def _msg_env():
    saved = AsyncMock(return_value={"id": "m1"})
    chat_cls = MagicMock()
    chat_cls.return_value.save_message = saved
    bus = MagicMock()
    bus.publish_chat_message = AsyncMock()
    # fixplan #8：本夹具原先 patch `_ceo_exit_assertion_block`（8 词完结断言
    # 词表）。该判据已下线（改为 project_meta.delivery_state 状态位），函数
    # 物理删除 ⇒ 不再需要 patch。这里的 agent_id 不是真 agent、无项目库 ⇒
    # `delivery_badge_metadata` 返回 None ⇒ payload 不带交付徽章，下面的
    # images 断言（metadata == {"source": "agent_to_user"}）逐字不变。
    with patch(
        "hiveweave.services.chat_message.ChatMessageService", chat_cls
    ), patch(
        "hiveweave.realtime.event_bus.status_event_bus", bus
    ):
        yield saved, bus


@pytest.fixture
def _reports_env(_msg_env, tmp_path):
    """项目根 = tmp_path，含 .hiveweave/reports/38709daf/shot-1.png 取证树。

    另在 reports 外放 secret.png（存在但不可被 images 引用）。
    """
    reports = tmp_path / ".hiveweave" / "reports"
    att = reports / "38709daf"
    att.mkdir(parents=True)
    shot = att / "shot-1.png"
    shot.write_bytes(_PNG_BYTES)
    (att / "notes.txt").write_text("not an image", encoding="utf-8")
    (tmp_path / "secret.png").write_bytes(b"outside-reports")
    data_url = (
        "data:image/png;base64,"
        + base64.b64encode(_PNG_BYTES).decode("ascii")
    )
    return _msg_env, tmp_path, shot, data_url


@pytest.mark.asyncio
async def test_images_land_on_user_visible_chat_message(_msg_env):
    saved, bus = _msg_env
    result = await message_user_tool(
        MessageUserParams(message="效果图如下", images=[_SMALL_IMG, _SMALL_IMG2]),
        "ceo-uuid",
        "/ws",
        None,
    )
    assert result.success is True, result.error
    assert saved.await_count == 1
    payload = saved.call_args[0][0]
    # 落库到用户可见的 chat_messages：images 列 + 来源标记
    assert payload["images"] == [_SMALL_IMG, _SMALL_IMG2]
    assert payload["metadata"] == {"source": "agent_to_user"}
    assert payload["role"] == "assistant"
    assert payload["content"] == "效果图如下"
    # WebSocket 实时推送同样带图
    _, kwargs = bus.publish_chat_message.call_args
    assert kwargs["message"]["images"] == [_SMALL_IMG, _SMALL_IMG2]


@pytest.mark.asyncio
async def test_bare_string_image_coerced_to_list(_msg_env):
    saved, _bus = _msg_env
    params = MessageUserParams(message="截图", images=_SMALL_IMG)  # 裸字符串
    assert params.images == [_SMALL_IMG]
    result = await message_user_tool(params, "ceo-uuid", "/ws", None)
    assert result.success is True, result.error
    payload = saved.call_args[0][0]
    assert payload["images"] == [_SMALL_IMG]


@pytest.mark.asyncio
async def test_too_many_images_rejected_with_remedy(_msg_env):
    saved, _bus = _msg_env
    result = await message_user_tool(
        MessageUserParams(
            message="太多图", images=[_SMALL_IMG] * 6
        ),
        "ceo-uuid",
        "/ws",
        None,
    )
    assert result.success is not True
    err = str(result.error or "")
    assert "5 张上限" in err and "分多条" in err  # 处方
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_image_rejected_with_remedy(_msg_env):
    saved, _bus = _msg_env
    big = "data:image/png;base64," + "A" * 2_800_001
    result = await message_user_tool(
        MessageUserParams(message="巨图", images=[big]),
        "ceo-uuid",
        "/ws",
        None,
    )
    assert result.success is not True
    err = str(result.error or "")
    assert "2MB" in err and "压缩" in err  # 处方
    saved.assert_not_called()


# ── 备注②：前缀白名单（data:image/ 或 http(s)://）──────


@pytest.mark.asyncio
async def test_bare_base64_without_data_prefix_rejected(_msg_env):
    """裸 base64（缺 data:image/ 前缀）→ 拒绝并给补前缀处方。"""
    saved, _bus = _msg_env
    result = await message_user_tool(
        MessageUserParams(message="裸base64", images=["iVBORw0KGgoAAAANSUhEUg=="]),
        "ceo-uuid",
        "/ws",
        None,
    )
    assert result.success is not True
    err = str(result.error or "")
    assert "data:image/" in err and "处方" in err
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_local_path_image_rejected(_msg_env):
    saved, _bus = _msg_env
    result = await message_user_tool(
        MessageUserParams(message="本地路径", images=["C:\\Users\\pics\\a.png"]),
        "ceo-uuid",
        "/ws",
        None,
    )
    assert result.success is not True
    assert "http" in str(result.error or "")
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_https_image_url_accepted(_msg_env):
    saved, bus = _msg_env
    url = "https://example.com/render.png"
    result = await message_user_tool(
        MessageUserParams(message="线上效果图", images=[url]),
        "ceo-uuid",
        "/ws",
        None,
    )
    assert result.success is True, result.error
    payload = saved.call_args[0][0]
    assert payload["images"] == [url]
    _, kwargs = bus.publish_chat_message.call_args
    assert kwargs["message"]["images"] == [url]


@pytest.mark.asyncio
async def test_no_images_regression(_msg_env):
    """不带图回归：payload 不含 images/metadata 键（既有纯文本路径不变）。"""
    saved, bus = _msg_env
    result = await message_user_tool(
        MessageUserParams(message="普通汇报"), "ceo-uuid", "/ws", None
    )
    assert result.success is True, result.error
    payload = saved.call_args[0][0]
    assert "images" not in payload
    assert "metadata" not in payload
    _, kwargs = bus.publish_chat_message.call_args
    assert "images" not in kwargs["message"]


# ── 报告截图直传（.hiveweave/reports/ 路径 → 平台内联 data URL）──────


@pytest.mark.asyncio
async def test_reports_relative_path_inlined_as_data_url(_reports_env):
    """reports 相对路径 → 平台读文件转 data URL 落库（与 data URL 同形态）。"""
    (saved, _bus), tmp_path, _shot, data_url = _reports_env
    result = await message_user_tool(
        MessageUserParams(
            message="验收截图如下",
            images=[".hiveweave/reports/38709daf/shot-1.png"],
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is True, result.error
    payload = saved.call_args[0][0]
    assert payload["images"] == [data_url]
    assert payload["metadata"] == {"source": "agent_to_user"}
    _, kwargs = _bus.publish_chat_message.call_args
    assert kwargs["message"]["images"] == [data_url]


@pytest.mark.asyncio
async def test_reports_absolute_path_inlined(_reports_env):
    """reports 内绝对路径同样内联成功。"""
    (saved, _bus), tmp_path, shot, data_url = _reports_env
    result = await message_user_tool(
        MessageUserParams(
            message="终验截图", images=[str(shot)]
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is True, result.error
    payload = saved.call_args[0][0]
    assert payload["images"] == [data_url]


@pytest.mark.asyncio
async def test_reports_dot_slash_prefix_accepted(_reports_env):
    """`./` 前缀变体（LLM 常见书写）正常解析。"""
    (saved, _bus), tmp_path, _shot, data_url = _reports_env
    result = await message_user_tool(
        MessageUserParams(
            message="截图",
            images=["./.hiveweave/reports/38709daf/shot-1.png"],
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is True, result.error
    assert saved.call_args[0][0]["images"] == [data_url]


@pytest.mark.asyncio
async def test_ctx_project_root_wins_over_workspace(_reports_env):
    """ctx.extra.project_root 注入优先于 workspace 推导（worktree 场景）。"""
    (saved, _bus), tmp_path, _shot, data_url = _reports_env
    ctx = SimpleNamespace(extra={"project_root": str(tmp_path)})
    result = await message_user_tool(
        MessageUserParams(
            message="截图", images=[".hiveweave/reports/38709daf/shot-1.png"]
        ),
        "leaf-uuid",
        "/some/leaf-worktree",
        ctx,
    )
    assert result.success is True, result.error
    assert saved.call_args[0][0]["images"] == [data_url]


@pytest.mark.asyncio
async def test_reports_traversal_dotdot_rejected(_reports_env):
    """`..` 穿越 → 直接拒绝（fail-closed），不读 reports 外文件。"""
    (saved, _bus), tmp_path, _shot, _data_url = _reports_env
    assert (tmp_path / "secret.png").exists()  # 越界目标真实存在也不可读
    result = await message_user_tool(
        MessageUserParams(
            message="偷图",
            images=[".hiveweave/reports/../secret.png"],
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is not True
    err = str(result.error or "")
    assert ".." in err and "防穿越" in err
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_absolute_path_outside_reports_whitelist_rejected(_reports_env):
    """reports 外绝对路径 → 白名单拒绝（非报告路径形态），提示 reports 通道。"""
    (saved, _bus), tmp_path, _shot, _data_url = _reports_env
    result = await message_user_tool(
        MessageUserParams(
            message="reports 外", images=[str(tmp_path / "secret.png")]
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is not True
    err = str(result.error or "")
    assert "不是可渲染的图片串" in err and ".hiveweave/reports" in err
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_report_path_escape_via_symlink_rejected(_reports_env, tmp_path):
    """reports 内符号链接指向外部 → resolve 后越出，拒绝（防逃逸）。"""
    (saved, _bus), _tmp, _shot, _data_url = _reports_env
    link = tmp_path / ".hiveweave" / "reports" / "38709daf" / "leak.png"
    try:
        link.symlink_to(tmp_path / "secret.png")
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    result = await message_user_tool(
        MessageUserParams(
            message="软链逃逸",
            images=[".hiveweave/reports/38709daf/leak.png"],
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is not True
    err = str(result.error or "")
    assert "越出" in err or "防逃逸" in err
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_report_non_image_ext_rejected(_reports_env):
    """后缀不在图片类型白名单 → 拒绝。"""
    (saved, _bus), tmp_path, _shot, _data_url = _reports_env
    result = await message_user_tool(
        MessageUserParams(
            message="非图片",
            images=[".hiveweave/reports/38709daf/notes.txt"],
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is not True
    assert "不是图片文件" in str(result.error or "")
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_missing_report_screenshot_error_with_remedy(_reports_env):
    """截图文件不存在 → 报错附处方（确认路径在 .hiveweave/reports/ 下）。"""
    (saved, _bus), tmp_path, _shot, _data_url = _reports_env
    result = await message_user_tool(
        MessageUserParams(
            message="还没截图",
            images=[".hiveweave/reports/38709daf/nope.png"],
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is not True
    err = str(result.error or "")
    assert "不存在" in err and ".hiveweave/reports" in err and "处方" in err
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_report_oversized_file_rejected(_reports_env):
    """截图超过单张 ~2MB 上限 → 拒绝附压缩处方（限幅沿用既有软上限）。"""
    (saved, _bus), tmp_path, _shot, _data_url = _reports_env
    big = tmp_path / ".hiveweave" / "reports" / "38709daf" / "big.png"
    big.write_bytes(b"x" * 64)
    with patch(
        "hiveweave.tools.misc_tools._MESSAGE_USER_REPORT_MAX_BYTES", 16
    ):
        result = await message_user_tool(
            MessageUserParams(
                message="巨图", images=[".hiveweave/reports/38709daf/big.png"]
            ),
            "ceo-uuid",
            str(tmp_path),
            None,
        )
    assert result.success is not True
    err = str(result.error or "")
    assert "2MB" in err and "压缩" in err
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_data_url_and_report_path(_reports_env):
    """data URL 与 reports 路径混用：各自解析、顺序保持。"""
    (saved, _bus), tmp_path, _shot, data_url = _reports_env
    result = await message_user_tool(
        MessageUserParams(
            message="混合附图",
            images=[_SMALL_IMG, ".hiveweave/reports/38709daf/shot-1.png"],
        ),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is True, result.error
    assert saved.call_args[0][0]["images"] == [_SMALL_IMG, data_url]


@pytest.mark.asyncio
async def test_image_count_cap_counts_report_paths(_reports_env):
    """张数限幅把路径条目一并计数（5 张 reports 路径 + 1 张 data URL = 6）。"""
    (saved, _bus), tmp_path, _shot, _data_url = _reports_env
    images = [".hiveweave/reports/38709daf/shot-1.png"] * 5 + [_SMALL_IMG]
    result = await message_user_tool(
        MessageUserParams(message="超张", images=images),
        "ceo-uuid",
        str(tmp_path),
        None,
    )
    assert result.success is not True
    err = str(result.error or "")
    assert "5 张上限" in err and "分多条" in err
    saved.assert_not_called()
