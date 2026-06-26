# PyPoE 诊断结论

## 结论（一句话）
**PyPoE 能在当前环境正常运行。国际版 GGPK 不捆绑 TC（繁体中文）映射文件不是 PyPoE 的 bug，而是已知的 GGPK 客户端行为差异。**

## 依据
1. 守夜已验证：`export_en_tc.py` 在 GGPK 环境下跑通，EN 导出 356 条路径，TC 导出 102 条路径（非脚本 bug）
2. SC→TC 兜底路线已验证通过：守夜完成 356 个文件 validate
3. `fill_tc_from_sc.py` 将 SC 数据复制覆盖到缺失 TC 表，1h 内可实现
4. `import_game_data.py` 的三路合并逻辑（EN+TC+SC）已就绪，无需代码改动

## 对守夜/TC 流程的影响
- **不阻塞**。TC 数据修复走 SC→TC 兜底路线，不需要 PyPoE 降级或手动 GGPK 导出

## 日期
- D2 诊断完成，人间草木确认
- D2-D7 守夜验证通过（validate 356 文件）
- D8 正式书面落盘
