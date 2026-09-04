"""构建 file:// 全兼容的单文件版：内联 three + OrbitControls + 应用代码。

架构：一个 <script type="module"> 内依序拼接
  1) three.module.js 源码（剥掉末尾 export 语句 → 生成 const THREE = {...}）
  2) OrbitControls.js 源码（剥掉 import 'three' 与 export）
  3) 应用代码（剥掉两行 import）
产物零外部依赖（无 importmap/shims/node_modules 引用），file:// 双击可验。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(r"D:\PC_AI\Project\HiveTestProject\TEST_DSH_40")
OUT = ROOT / "town-standalone.html"

html = (ROOT / "index.html").read_text(encoding="utf-8")

# 1. 应用模块体（第一个 <script type="module"> 到对应 </script>）
m_app = re.search(r'<script type="module">(.*?)</script>', html, re.S)
app_body = m_app.group(1)

# 2. 剥应用的两行 import
app_body = re.sub(r"import \* as THREE from 'three';\s*\n", "", app_body)
app_body = re.sub(
    r"import \{ OrbitControls \} from 'three/addons/controls/OrbitControls\.js';\s*\n",
    "",
    app_body,
)

# 3. three.module.js：剥 export 语句，收集导出名
three_src = (ROOT / "node_modules/three/build/three.module.js").read_text(
    encoding="utf-8"
)
export_m = re.search(r"export\s*\{([^}]+)\}\s*;?\s*$", three_src, re.S)
assert export_m, "three.module.js 末尾未找到 export 块"
names = [n.strip().split(" as ")[-1] for n in export_m.group(1).split(",") if n.strip()]
three_src = three_src[: export_m.start()] + three_src[export_m.end():]

# 4. OrbitControls：捕获其 import 列表（= 它依赖的 three 名字集合，ESM import
#    是穷尽的），剥 import 与 export；IIFE 用参数注入这些名字（绑 THREE.*），
#    否则裸名引用在隔离作用域里 ReferenceError（实测：Ray is not defined）。
orbit_src = (ROOT / "node_modules/three/examples/jsm/controls/OrbitControls.js").read_text(
    encoding="utf-8"
)
m_orbit_imp = re.search(r"import\s*\{([^}]*)\}\s*from\s*'three';", orbit_src)
assert m_orbit_imp, "OrbitControls 未找到 import 'three'"
orbit_deps = [
    n.strip().split(" as ")[-1]
    for n in m_orbit_imp.group(1).split(",")
    if n.strip()
]
orbit_src = re.sub(r"import\s*\{[^}]*\}\s*from\s*'three';\s*\n", "", orbit_src)
orbit_src = re.sub(r"export\s*\{[^}]*\}\s*;?\s*$", "", orbit_src, flags=re.S)

# 5. 组装：THREE 命名空间对象（在 three 源码之后、app 之前）
# 5. 组装：three 全源码包 IIFE（隔离其顶层 helper——smoothstep 等与 app 撞名），
#    命名空间由 IIFE 返回值生成；OrbitControls 同样包 IIFE（内部 helper 与
#    three 顶层重名——_ray 等）。裸名（Vector2 等）在各 IIFE 内部自洽；app
#    代码使用 THREE.* 与 OrbitControls。
three_iife = (
    "const THREE = (function(){\n"
    + three_src
    + "\nreturn { "
    + ", ".join(names)
    + " };\n})();"
)

# 5b. 自纠错扫描：裸引用 three 导出名但未被注入/别名的 → 自动补齐。
#     排除 THREE.* 成员访问（负向断言）与更长标识符的一部分。
bare_pat = re.compile(r"(?<![\w$.])([A-Z][A-Za-z0-9_]*)\b")
names_set = set(names)

oc_extra = {n for n in bare_pat.findall(orbit_src) if n in names_set} - set(orbit_deps)
if oc_extra:
    orbit_deps = sorted(set(orbit_deps) | oc_extra)

app_extra = {
    n
    for n in bare_pat.findall(app_body)
    if n in names_set
}
combined = (
    "/* ===== 内联 three.module.js (r160，IIFE 隔离) ===== */\n"
    + three_iife
    + "\n\n/* ===== 内联 OrbitControls（IIFE 隔离 + 依赖注入）===== */\n"
    + "const OrbitControls = (function(THREE, "
    + ", ".join(orbit_deps)
    + "){\n"
    + orbit_src
    + "\nreturn OrbitControls;\n})(THREE, "
    + ", ".join(f"THREE.{n}" for n in orbit_deps)
    + ");"
    + "\n\n/* ===== 应用代码作用域补齐（裸名 → THREE 解构）===== */\n"
    + "const { "
    + ", ".join(sorted(app_extra))
    + " } = THREE;"
    + "\n\n/* ===== 应用代码 ===== */\n"
    + app_body
    + "\n"
)

# 6. 重组 HTML：去 importmap、去 es-module-shims、原 module 脚本替换为 combined
out = html
out = re.sub(r'<script src="\.\/node_modules\/es-module-shims[^>]*></script>\s*\n', "", out)
out = re.sub(r'<script type="importmap">.*?</script>\s*\n', "", out, flags=re.S)
out = out.replace(m_app.group(0), "<script type=\"module\">\n" + combined.replace("\\", "\\\\")[:0] + "<!--BUILD-->" )
# 上面 replace 占位符方式太绕，直接用 position 拼装
start = m_app.start(0)
end = m_app.end(0)
out = html[:start] + '<script type="module">\n' + combined + "\n</script>" + html[end:]
# 重新去掉 importmap/shims（对最终 out 再做一遍，确保覆盖）
out = re.sub(r'<script src="\.\/node_modules\/es-module-shims[^>]*></script>\s*\n?', "", out)
out = re.sub(r'<script type="importmap">.*?</script>\s*\n?', "", out, flags=re.S)

OUT.write_text(out, encoding="utf-8")

# 7. 抽取 combined 做语法检查
mod_path = Path(r"D:\PC_AI\Project\HiveWeave\tasks\_standalone_check.mjs")
mod_path.write_text(combined, encoding="utf-8")
print("BUILT:", OUT.name, f"{OUT.stat().st_size/1024:.0f} KB")
print("module check file:", mod_path)
