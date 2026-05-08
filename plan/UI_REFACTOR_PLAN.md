# AMB2API 控制面板 UI 重构总规划手册

> 版本：v1.0 · 起草日期：2026-05-04 · 参考项目：AetherBlog-Idea (Aether Codex 设计系统)
> 目标：将 amb2api 现有单文件控制面板（18,582 行 / 805KB）全面重塑为现代化、可维护、视觉精致的管理平台，
> **核心原则：功能/能力 100% 等价，仅 UI/UX 升级。**

---

## 0. 阅读指南

| 角色 | 重点章节 |
|------|---------|
| 项目所有者（你） | §1 总览、§2 设计语言比对、§7 阶段路线图、§9 风险与回滚 |
| 实施者（Claude） | §3 文件结构、§4 设计令牌、§5 组件契约、§6 命名规范、§8 每阶段验收 |

---

## 1. 总览

### 1.1 目标
1. **视觉**：把"Google Blue + 粒子动画 + emoji 图标"的复古面板，升级为"Aether Codex 暖色 + Aurora 渐变 + Lucide 线性图标"的现代后台。
2. **结构**：从 18,582 行单文件 HTML，拆分为可维护的 CSS / JS 模块，并保留单 URL 入口（`GET /ui` 仍然可用）。
3. **交互**：左侧导航 + 主区域 + 顶部工具条 + 命令面板 (Cmd+K)；表格/筛选/日志面板对齐参考项目的优秀样式。
4. **保真**：保留所有 9 个 Tab、所有 Modal、所有 fetch/WebSocket 端点、所有 onclick handler、所有 DOM id。

### 1.2 非目标
- 不改后端 API 路径（`/config/*` `/usage/*` `/api/account/*` 等保持不变）
- 不引入构建工具链（Vite/Webpack）；继续 vanilla JS + CSS，浏览器直接加载
- 不改业务逻辑、不改密码鉴权流程
- 不改文件名 `front/control_panel.html`（FastAPI 仍指向它）

### 1.3 约束
- **必须**：Bearer token 鉴权（`getAuthHeaders()`）保持调用方式不变
- **必须**：所有 API URL 字符串原样保留（grep 现有 `fetch('/'` 出现位置）
- **必须**：所有 `id="..."` 名称在迁移后必须保留或同步重命名
- **可选**：`onclick="fnName()"` 可改为 `addEventListener`，但函数名必须保留全局可调用

---

## 2. 设计语言比对

### 2.1 现状（amb2api）

| 维度 | 现状 |
|------|------|
| 主色 | `#4285f4` Google Blue + 多色 emoji |
| 背景 | 亮 `#f5f5f5` / 暗 `#1a1a2e` |
| 容器 | 亮 `white` / 暗 `#16213e` |
| 字体 | 系统默认 sans-serif |
| 圆角 | 混乱（4px / 8px / 16px 杂用） |
| 图标 | emoji（⛵️ ⛩️ 👁️ 🌙 🪴 🗑️ 📥 🔗 ⛔） |
| 动效 | balancePulse / spendGlow / 粒子 / 涟漪 / 五彩纸屑 / 鼠标光晕 |
| 布局 | 顶部 7 标签页（横向），全屏内容区 |
| 表格 | HTML table + 斑马纹 + sticky 表头 |
| 表单 | 原生 input / 自定义 .custom-select-wrapper |
| Modal | .modal + .modal-content（半透明遮罩） |

### 2.2 目标（Aether Codex）

| 维度 | 目标 |
|------|------|
| 主色 | Aurora 渐变 `#6366F1 → #7C6FF1 → #8B84F0 → #A598EC`（暗色明度提升） |
| 背景 | 四层：void `#FAF9F6`(亮)/`#0a0a0f`(暗)、substrate、leaf、raised |
| 文字 | 三层：ink-primary / ink-secondary / ink-muted |
| 字体 | Inter（UI）+ JetBrains Mono（代码与数字） |
| 圆角 | 8 / 12 / 16 / 24 px（sm/md/lg/xl） |
| 间距 | 8px 基线（space-1=4 至 space-10=128） |
| 图标 | Lucide 风格 inline SVG（约 60 个，stroke-width=1.75，1.25rem 标准尺寸） |
| 动效 | Spring 物理（soft / precise / bouncy）+ Expo Out `cubic-bezier(0.16,1,0.3,1)` |
| 布局 | 左侧 256px 可折叠 Sidebar + 顶部 56px AppBar + 主区域 |
| 表格 | 行 hover、StatusBadge、列可见性、密度切换、分页器卡片化 |
| 表单 | 统一 .input / .select / .textarea；3px focus ring (aurora-1 22%) |
| Modal | Portal + spring 入场 + backdrop blur(20px) |
| 玻璃态 | .glass / .glass-high / .glass-premium 三档 |
| 主题 | data-theme="dark" + View Transitions API 切换 |

### 2.3 装饰元素处理

参考项目讲究"克制的装饰"，amb2api 当前装饰元素需要重新评估：

| 元素 | 决策 | 理由 |
|------|------|------|
| 粒子背景 (particleCanvas) | **保留但默认禁用**（移到设置项） | 影响性能；但已有逻辑可保留 |
| 渐变浸染层 (gradientOverlay) | **替换为 aurora 渐变光斑**（CSS only） | 更现代、不需要 canvas |
| 涟漪 / 五彩纸屑 (rippleCanvas / confettiCanvas) | **保留**，仅在登录成功 / 配置保存等特定时刻触发 | 提供成就感反馈 |
| 鼠标光晕 (mouseGlow) | **保留但弱化**（默认 opacity 0.3） | 现代化的微交互 |
| 浮动光点 (floating-orb) ×5 | **删除** | 与新色板冲突（红/绿/黄/蓝原色）|
| 闪烁星星 (twinkle-star) ×6 | **删除** | 同上 |
| balance-amount 红光闪烁 / spend-amount 绿光流动 | **保留但更柔和**（光晕半径减半，频率减慢） | 业务语义信号 |
| 选中账户脉冲 | **保留**（颜色改为 aurora-1） | 有用的视觉反馈 |

---

## 3. 文件与目录结构

### 3.1 重构后目录

```
amb2api/
├── front/
│   ├── control_panel.html              ← 重写为骨架（~2000 行：HTML 结构 + 内联引用）
│   ├── control_panel.legacy.html       ← 备份原 18,582 行旧版（用于回滚 / 比对）
│   ├── control_panel_mobile.html       ← 暂保留旧版（Phase 11 处理）
│   └── assets/
│       ├── css/
│       │   ├── tokens.css              ← 设计令牌（颜色/间距/字号/圆角/阴影/动效）
│       │   ├── base.css                ← 重置 + 排版 + 工具类
│       │   ├── layout.css              ← AppShell（Sidebar + AppBar + Main）
│       │   ├── components.css          ← 按钮/输入/卡片/徽章/对话框/下拉/Toast
│       │   ├── pages.css               ← 各页特定样式（accountTab / configTab ...）
│       │   ├── effects.css             ← 玻璃态 / 光斑 / 主题切换动画
│       │   └── legacy-bridge.css       ← 渐进迁移期：兼容旧 class 名
│       ├── js/
│       │   ├── icons.js                ← Lucide 风格 SVG 图标库（约 60 个）
│       │   ├── ui.js                   ← UI 工具：Toast / Modal / Tooltip / Skeleton 工厂
│       │   ├── theme.js                ← 主题切换 + View Transitions
│       │   ├── command-palette.js      ← Cmd+K 命令面板
│       │   ├── focus-mode.js           ← Cmd+. 聚焦模式
│       │   └── boot.js                 ← 入口：初始化全局 UI + 监听器
│       └── icons/
│           └── README.md               ← 图标列表索引（仅文档，SVG 都在 icons.js）
└── src/api/admin_routes.py             ← 增加 StaticFiles 挂载
```

### 3.2 单 HTML 入口的依赖加载顺序

```html
<head>
  <link rel="stylesheet" href="/static/css/tokens.css">
  <link rel="stylesheet" href="/static/css/base.css">
  <link rel="stylesheet" href="/static/css/layout.css">
  <link rel="stylesheet" href="/static/css/components.css">
  <link rel="stylesheet" href="/static/css/effects.css">
  <link rel="stylesheet" href="/static/css/pages.css">
  <link rel="stylesheet" href="/static/css/legacy-bridge.css">
  <!-- Inter / JetBrains Mono 通过 <link> 或 self-host -->
</head>
<body>
  <!-- HTML 骨架 -->
  ...
  <!-- 在末尾按依赖顺序加载 -->
  <script src="/static/js/icons.js"></script>
  <script src="/static/js/ui.js"></script>
  <script src="/static/js/theme.js"></script>
  <script src="/static/js/command-palette.js"></script>
  <script src="/static/js/focus-mode.js"></script>
  <!-- 业务 JS（沿用旧 inline 内容，逐步迁出） -->
  <script>...18,000 行业务逻辑（保留）...</script>
  <script src="/static/js/boot.js"></script>
</body>
```

### 3.3 web.py 静态挂载

```python
# web.py（新增）
from fastapi.staticfiles import StaticFiles
import os

_BASE = os.path.dirname(os.path.abspath(__file__))
_ASSETS_DIR = os.path.join(_BASE, "front", "assets")
if os.path.isdir(_ASSETS_DIR):
    app.mount("/static", StaticFiles(directory=_ASSETS_DIR), name="static")
```

---

## 4. 设计令牌（tokens.css 规范）

### 4.1 颜色

```css
:root {
  /* === 文本（亮色） === */
  --ink-primary:   #1C1A14;
  --ink-secondary: #4A463E;
  --ink-muted:     #7A7468;
  --ink-subtle:    #C9C3B5;

  /* === 背景（亮色，四层） === */
  --bg-void:      #FAF9F6;   /* 主画布 - 暖白 */
  --bg-substrate: #F4F2EC;   /* 次级容器 */
  --bg-leaf:      #FFFFFF;   /* 卡片 */
  --bg-raised:    #FFFFFF;   /* 弹层最高 */

  /* === 边框 === */
  --border-default: color-mix(in oklch, var(--ink-primary) 10%, transparent);
  --border-hover:   color-mix(in oklch, var(--aurora-1) 30%, transparent);

  /* === 品牌：Aurora 渐变 === */
  --aurora-1: #6366F1;
  --aurora-2: #7C6FF1;
  --aurora-3: #8B84F0;
  --aurora-4: #A598EC;
  --aurora-grad: linear-gradient(135deg, var(--aurora-1), var(--aurora-4));

  /* === 状态语义 === */
  --color-success: #16a34a;
  --color-warning: #d97706;
  --color-danger:  #dc2626;
  --color-info:    #2563eb;

  /* === 半透明遮罩 === */
  --overlay-soft:   rgba(0,0,0,0.40);
  --overlay-strong: rgba(0,0,0,0.70);
}

[data-theme="dark"] {
  --ink-primary:   #FFFFFF;
  --ink-secondary: #CBD5E1;
  --ink-muted:     #94A3B8;
  --ink-subtle:    #475569;

  --bg-void:      #0a0a0f;
  --bg-substrate: #13131a;
  --bg-leaf:      #1a1a24;
  --bg-raised:    #22222e;

  --border-default: rgba(255,255,255,0.08);
  --border-hover:   rgba(129,140,248,0.40);

  --aurora-1: #818cf8;
  --aurora-2: #a5b4fc;
  --aurora-3: #c7d2fe;
  --aurora-4: #e0e7ff;

  --color-success: #34d399;
  --color-warning: #fbbf24;
  --color-danger:  #f87171;
  --color-info:    #60a5fa;
}
```

### 4.2 间距 / 字号 / 圆角 / 阴影 / 动效

```css
:root {
  /* 间距（8px 基线） */
  --space-1: .25rem; --space-2: .5rem; --space-3: .75rem;
  --space-4: 1rem;   --space-5: 1.5rem; --space-6: 2rem;
  --space-7: 3rem;   --space-8: 4rem;   --space-9: 6rem;  --space-10: 8rem;

  /* 字号 */
  --fs-micro: .6875rem; --fs-caption: .8125rem; --fs-body: 1rem;
  --fs-reading: 1.125rem; --fs-lede: 1.25rem;
  --fs-h4: 1.5rem; --fs-h3: 1.875rem; --fs-h2: 2.5rem; --fs-h1: 3.5rem;

  /* 行高 */
  --lh-tight: 1.1; --lh-snug: 1.25; --lh-normal: 1.5; --lh-relaxed: 1.75;

  /* 圆角 */
  --radius-sm: .5rem; --radius-md: .75rem; --radius-lg: 1rem; --radius-xl: 1.5rem;
  --radius-full: 9999px;

  /* 阴影 */
  --shadow-xs: 0 1px 2px rgba(0,0,0,.04);
  --shadow-sm: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.06);
  --shadow-md: 0 4px 6px rgba(0,0,0,.07), 0 2px 4px rgba(0,0,0,.06);
  --shadow-lg: 0 10px 15px rgba(0,0,0,.10), 0 4px 6px rgba(0,0,0,.05);
  --shadow-aurora: 0 8px 24px color-mix(in oklch, var(--aurora-1) 35%, transparent);

  /* 动效 */
  --ease-out:  cubic-bezier(.16, 1, .3, 1);   /* Expo Out */
  --ease-in:   cubic-bezier(.7, 0, .84, 0);
  --ease-soft: cubic-bezier(.4, 0, .2, 1);

  --dur-instant: .12s;
  --dur-quick:   .26s;
  --dur-flow:    .52s;
  --dur-ambient: 1.8s;

  /* z-index 层级 */
  --z-base:    1;
  --z-fixed:   100;
  --z-overlay: 1000;
  --z-modal:   1100;
  --z-popover: 1200;
  --z-toast:   1300;
}
```

---

## 5. 核心组件契约

### 5.1 按钮（.btn）

```html
<button class="btn btn--primary" data-icon="check">保存</button>
<button class="btn btn--secondary">取消</button>
<button class="btn btn--ghost" data-icon="refresh-cw">刷新</button>
<button class="btn btn--danger" data-icon="trash-2">删除</button>
<button class="btn btn--icon" data-icon="x" aria-label="关闭"></button>

<!-- 尺寸 -->
<button class="btn btn--primary btn--sm">…</button>
<button class="btn btn--primary btn--lg">…</button>

<!-- 加载态 -->
<button class="btn btn--primary" data-loading="true">…</button>
```

特征：
- 默认 `padding: var(--space-2) var(--space-4)`，圆角 `--radius-md`
- Primary：背景 `var(--aurora-grad)`，文字白色，hover 上浮 `translateY(-1px)` + `--shadow-aurora`
- Secondary：背景 `var(--bg-leaf)`，边框 `var(--border-default)`
- Ghost：透明背景，hover 显示 `var(--bg-substrate)`
- Danger：背景 `var(--color-danger)`，文字白色
- 图标插槽：`data-icon="<lucide-name>"` 由 `ui.js` 在 DOMContentLoaded 自动渲染

### 5.2 输入（.input）

```html
<div class="field">
  <label class="field__label">API Key</label>
  <div class="input-wrapper">
    <span class="input-prefix" data-icon="key"></span>
    <input class="input" type="text" placeholder="sk-…">
    <button class="input-suffix" data-icon="eye"></button>
  </div>
  <p class="field__help">保留为空将使用环境变量值</p>
</div>
```

特征：
- 高度 40px，圆角 `--radius-md`，边框 `var(--border-default)`
- Focus：`box-shadow: 0 0 0 3px color-mix(in oklch, var(--aurora-1) 22%, transparent)` + 边框变 aurora-1
- 错误态：`.field--error` 把边框/聚焦环换成 `--color-danger`

### 5.3 卡片（.card）

```html
<section class="card">
  <header class="card__header">
    <h3 class="card__title">配置概览</h3>
    <div class="card__actions"><button class="btn btn--ghost btn--sm">…</button></div>
  </header>
  <div class="card__body">…</div>
  <footer class="card__footer">…</footer>
</section>
```

特征：圆角 `--radius-lg`，背景 `--bg-leaf`，padding `--space-5`，边框 `--border-default`

### 5.4 徽章（.badge）

```html
<span class="badge badge--success" data-icon="check-circle">已启用</span>
<span class="badge badge--warning">手动禁用</span>
<span class="badge badge--danger">自动禁用</span>
<span class="badge badge--info">轮询</span>
<span class="badge badge--neutral">DEBUG</span>
```

### 5.5 对话框（.modal）

```html
<dialog class="modal" id="keyManagementModal">
  <div class="modal__panel">
    <header class="modal__header">
      <h3 class="modal__title">多密钥管理</h3>
      <button class="btn btn--icon" data-icon="x" data-action="close-modal"></button>
    </header>
    <div class="modal__body">…</div>
    <footer class="modal__footer">
      <button class="btn btn--secondary" data-action="close-modal">取消</button>
      <button class="btn btn--primary">确认</button>
    </footer>
  </div>
</dialog>
```

特征：使用原生 `<dialog>`；遮罩 `backdrop-filter: blur(20px)`；入场 spring 动画

### 5.6 Toast / 通知

`window.toast({ kind: 'success'|'error'|'warning'|'info', title, description, duration })`

替换原有的 `alert()` 与 `showStatus()`（保留 showStatus 作为 backwards 适配层）

### 5.7 表格（.table）

```html
<div class="table-card">
  <header class="table-toolbar">
    <div class="table-toolbar__filters">…</div>
    <div class="table-toolbar__actions">…</div>
  </header>
  <div class="table-scroll">
    <table class="table">
      <thead><tr><th>…</th></tr></thead>
      <tbody><tr class="table__row"><td>…</td></tr></tbody>
    </table>
  </div>
  <footer class="table-pagination">…</footer>
</div>
```

特征：sticky 表头 + sticky 首列（继承自旧 .trace-table 经验）；行 hover 高亮 `--bg-substrate`；密度可切换 (`.table--compact` / `.table--cozy`)

### 5.8 下拉菜单（.menu）

替换 `.custom-select-wrapper`：
```html
<div class="select" data-select>
  <button class="select__trigger" type="button">
    <span class="select__value">全部状态</span>
    <span data-icon="chevron-down"></span>
  </button>
  <div class="select__popover" hidden>
    <button class="select__option" data-value="all">全部状态</button>
    <button class="select__option" data-value="enabled">已启用</button>
    <button class="select__option" data-value="disabled">已禁用</button>
  </div>
</div>
<input type="hidden" id="modalKeyStatusFilter" value="all">
```

迁移策略：旧 `.custom-select-wrapper` 通过 `legacy-bridge.css` 重新映射到 `.select` 视觉，让 JS 不需要改动；后续逐步替换 markup

---

## 6. 图标系统（icons.js 规范）

### 6.1 设计原则
- Lucide 1.0+ 同款 24×24 viewBox，stroke-width=1.75，line-cap=round
- 颜色继承 `currentColor`
- 提供 `<Icon name="key" />` 等价的 vanilla 方案：`window.icons.render(el)` 把 `[data-icon]` 节点替换为 SVG

### 6.2 必需图标清单（约 60 个）

**导航**：layout-dashboard, key, server, activity, gauge, terminal, send, line-chart, file-text
**操作**：plus, edit, trash-2, copy, refresh-cw, save, download, upload, check, x, search, filter
**状态**：check-circle, x-circle, alert-triangle, info, loader-2, ban, shield
**箭头**：chevron-down, chevron-up, chevron-left, chevron-right, arrow-up-right, external-link
**主题**：sun, moon, monitor
**面板**：sidebar, panels-left, panels-right, expand, minimize-2, command, settings, log-out, eye, eye-off
**业务**：dollar-sign, credit-card, zap, sparkles, brain-circuit, bot, message-square, lock, unlock
**系统**：cpu, hard-drive, wifi, wifi-off, signal, clock, calendar

### 6.3 用法

```html
<!-- 静态：data-icon 属性 -->
<button class="btn" data-icon="save">保存</button>

<!-- 动态：JS 接口 -->
<script>
  // 渲染单个图标
  el.appendChild(window.icons.create('refresh-cw', { size: 16, class: 'spin' }));
  // 替换占位符
  window.icons.render(document.body);
</script>
```

---

## 7. 阶段路线图（与任务一一对应）

| # | 阶段 | 工件 | 验收 |
|---|------|------|------|
| 1 | **本规划手册** | `plan/UI_REFACTOR_PLAN.md` | 用户认可整体方向 |
| 2 | **静态资源骨架** | `front/assets/{css,js,icons}/`, `web.py` 挂载，`control_panel.legacy.html` 备份 | `GET /static/css/tokens.css` 200 OK，旧面板仍可正常打开 |
| 3 | **设计令牌 + 基础样式** | `tokens.css` `base.css` | 控制台审查可见 CSS 变量；body 字体/底色已切到新色板 |
| 4 | **图标库 + 组件库** | `icons.js` `components.css` `ui.js` | 测试页可渲染所有 60 个图标；按钮/输入/卡片符合规范 |
| 5 | **登录 + AppShell** | `layout.css` 替换 `.fixed-header` + `.tabs` + `.tab-content-wrapper` 结构为 Sidebar + AppBar + Main | 登录、tab 切换、主题切换全部正常 |
| 6 | **配置管理 (configTab)** | `pages.css#config` 段落 | 所有保存/加载/字段验证通过；后端 `/config/save` 字段无丢失 |
| 7 | **账户管理 (accountTab)** | `pages.css#account` | 多账户切换、子页 4 个、登录账户 modal、账单图表 |
| 8 | **数据展示三件套** | `pages.css#ratelimit/usage/performance` | 表格/筛选/分页/导出无回归 |
| 9 | **实时日志** | `pages.css#logs` | WebSocket 连接、四级颜色、下载、过滤 |
| 10 | **操练场** | `pages.css#playground` | 流式/非流式请求、消息行编辑、JSON 编辑器 |
| 11 | **Modal 全量** | `components.css` 已涵盖；逐 modal 替换 markup | 所有弹窗（密钥/限制/备注/确认）符合新规范 |
| 12 | **响应式 + 动效** | `effects.css` + media queries + `theme.js` | 移动端可用；主题切换有 view-transition；命令面板可用 |
| 13 | **QA 回归** | 检查表 §8.2 | 用户在测试环境验证所有功能 |

---

## 8. 命名 / 编码规范

### 8.1 CSS

- 使用 BEM-lite：`.block`、`.block__element`、`.block--modifier`
- 状态用 `data-*` 属性：`data-loading`、`data-active`、`data-disabled`
- 全部尺寸 / 颜色 / 阴影 / 动效引用 CSS 变量，禁止硬编码 hex / px（除全局令牌定义外）
- 关键过渡用 `transition: all var(--dur-quick) var(--ease-out)` 或更精细字段列表

### 8.2 JS

- 新代码用 ES2022+；浏览器兼容现代 Chromium / Safari 17+ / Firefox ESR
- 不引入构建器；模块化用 `<script type="module">` 和命名空间对象（`window.icons`、`window.ui`）
- 旧业务函数全部保留为全局，`onclick="fn()"` 不强制改写
- 新工具加 JSDoc 注解

### 8.3 验收（每阶段必跑）
- 浏览器控制台 0 报错
- 后端日志无 4xx/5xx 异常
- `grep -n "fetch(" front/control_panel.html` 数量与 baseline 一致
- 旧 DOM id 在新 HTML 中可被 `document.getElementById` 找到
- 主题切换、登录、tab 切换、保存配置、查看用量、查看日志：手动各跑一遍

---

## 9. 风险与回滚策略

### 9.1 主要风险

| 风险 | 缓解 |
|------|------|
| 大幅改动单文件结构导致 DOM id 引用失效 | Phase 2 即建立 `legacy-bridge.css`；新旧 markup 共存期通过双 class 名兼容 |
| 业务 JS 内联在 HTML 内（约 9000 行）依赖旧 class 选择器 | grep 所有 `querySelector(`/`getElementsByClassName(`，迁移时同步更新 |
| 主题变量改名（`--bg-color` → `--bg-void`）冲击旧样式 | `legacy-bridge.css` 用 fallback：`background: var(--bg-color, var(--bg-void))` |
| 静态文件 mount 路径冲突 | 使用 `/static/*`；与 `/v1/*` `/api/*` `/config/*` `/usage/*` 不冲突 |
| 旧粒子/涟漪 canvas 与新 layout 冲突 | Phase 5 把它们移入主区域 z-index=0，sidebar 与 AppBar 在 z-index=100+ |
| FastAPI 文件读取仍指向 `control_panel.html` | 不改路径，直接重写文件内容 |

### 9.2 回滚

- 每阶段完成后 `git commit`，commit 信息含 `[ui-refactor:phase-X]` 前缀
- 回滚命令：`git revert <commit>` 单阶段、`cp front/control_panel.legacy.html front/control_panel.html` 全量
- 保留 `.legacy.html` 至少到全部阶段完成 + 用户验收通过

### 9.3 渐进式发布

- 不做 Feature Flag（单用户工具，无需）
- 改动直接合到 main 分支；每阶段一个 PR / commit 便于 bisect
- 阶段间可暂停接受用户反馈，调整后续阶段优先级

---

## 10. 验收检查清单（Phase 13 用）

### 10.1 功能等价检查

| 模块 | 检查项 |
|------|--------|
| 登录 | 输入密码登录成功 / 失败提示 / 记住会话 / 登出 |
| 账户管理 | 添加账户 / 切换账户 / 删除账户 / 4 个子页加载 / 导出账单 |
| 配置管理 | 加载现有配置 / 修改 / 保存 / 校验 / 与环境变量优先级 |
| 速率限制 | 实时刷新 / 自动刷新开关 / 单 key 状态 |
| 使用统计 | 模型/Key 聚合 / 请求轨迹分页 / CSV+JSON 导出 / 重置 |
| 操练场 | Stream/non-stream 请求 / 工具定义 / 模型与 Key 选择 / 预览模式 |
| 性能分析 | 总览 / 单请求瀑布 / 过滤分页 |
| 实时日志 | WebSocket 连接 / 4 级过滤 / 自动滚动 / 下载 / 清空 |
| 凭证管理 | 上传 .json / .zip / 启用 / 禁用 / 删除 / 备注 / 限制 |
| 多密钥 | 添加 / 状态 / 自动禁用 / 失效检测 |

### 10.2 视觉一致性检查
- 全部页面文字使用 Inter；数字/代码使用 JetBrains Mono
- 全部按钮符合 §5.1 五种形态
- 全部图标为 SVG（`grep "alt=\"\\|emoji" front/control_panel.html` 应仅出现在文档/历史区域）
- 主题切换时四层背景平滑过渡
- 焦点环 3px aurora 22%

### 10.3 性能检查
- 首屏 LCP < 1.5s（本地）
- 切换 tab 无明显卡顿
- 实时日志 1000 条不阻塞

---

## 11. 后续可选增强（Out of Scope）

以下不在本次重构范围，但建议作为后续迭代：

- 引入 Vite 构建管线（HMR、代码分割、TypeScript）
- 用 Solid / Preact 信号式重写状态管理
- 引入 i18n（中/英文切换）
- 引入正式的图表库（ECharts / Recharts via CDN）
- 增加桌面通知（成本告警 / 配额告警）
- 将 control_panel_mobile.html 与桌面版合并为同一响应式 HTML

---

## 12. 进度跟踪

进度通过 Claude TaskList 维护：当前共 13 个 Task（含本手册）。
完成后会在 `MEMORY.md` 中备注最终状态。
