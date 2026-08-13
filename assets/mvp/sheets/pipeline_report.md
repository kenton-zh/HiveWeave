# 管线校验报告

## role_sheet：48 帧，112×168，实际最大色数 8（上限 8）

**服装一致性修正（pass 1-3）**：以 worker_base_0 为标准（灰外套 g + 棕褐肤 t + 暗紫发 V），
对白衬衫（W）、深蓝外套（D/N）、紫外套（V 躯干区）、浅肤（S/s）按区域重映射；
多余色 D/N/v/W 全局合并到最近标准色。合计修改 ~3700 像素。

- worker_coffee: 4 帧，帧间差异 0.52
- worker_curly_idle: 2 帧，帧间差异 0.17
- worker_deliver: 2 帧，帧间差异 0.88
- worker_idle: 2 帧，帧间差异 0.21
- worker_jump: 2 帧，帧间差异 0.63
- worker_land: 3 帧，帧间差异 0.81
- worker_long_idle: 2 帧，帧间差异 0.18
- worker_nod: 2 帧，帧间差异 0.47
- worker_question: 2 帧，帧间差异 0.47
- worker_smoke: 4 帧，帧间差异 0.71
- worker_typing: 4 帧，帧间差异 0.29
- worker_walk_down: 4 帧，帧间差异 0.71
- worker_walk_left: 4 帧，帧间差异 0.81
- worker_walk_right: 4 帧，帧间差异 0.81
- worker_walk_up: 4 帧，帧间差异 0.48

## obj_sheet：13 帧，128×128，实际最大色数 13（上限 16）

## tile_sheet：10 帧，64×48，实际最大色数 8（上限 16）
- cloud: 2 帧，帧间差异 0.42

## fx_sheet：23 帧，120×120，实际最大色数 8（上限 8）
- check: 2 帧，帧间差异 0.15
- dust: 2 帧，帧间差异 0.89
- fire: 3 帧，帧间差异 0.60
- glow: 2 帧，帧间差异 0.35
- red_x: 2 帧，帧间差异 0.35
- smoke_black: 3 帧，帧间差异 0.71
- star: 3 帧，帧间差异 0.44
