# AMB2API 控制台 UI · V2 深度精化与适配方案

> 起草日期：2026-05-08  · 作者：Claude / 用户协作
> 目标：在 V1（`UI_REFACTOR_PLAN.md`，已完成）已建立的"Aether Codex 设计系统"基础上，对**已登录后的内部控制台**做一次"对齐顶级管理设计"的深度二刷。
>
> 关键边界：
> - **登录页保持"层林尽染"原版**（V2 期已回滚 `pages.css#1` 与 `legacy-bridge.css#11`，并补充 `#19. #loginSection 例外区`）。
> - **后端契约 100% 不动**（FastAPI 路由、字段、鉴权、ID 命名全部保留）。

---

## 0. 设计参考与立场

| 来源 | 借鉴点 |
|------|--------|
| **Linear** | 极致克制的灰阶 + 单一 accent 色；左侧 256px 可折叠导航；列表行 hover 微提亮；Cmd+K 命令面板 |
| **Vercel Dashboard** | "Surface 用 1px 边框而非阴影"；空状态插画极简；密度紧凑 |
| **Stripe Dashboard** | 表格首列 sticky；状态徽章是文字+点；"金额"始终右对齐 + tabular-nums |
| **Plaid / Notion** | 顶部 AppBar 上下文搜索；快捷键提示 chip；侧边栏 section 折叠 |

**坚持立场：**
- **不引入 React / Vue / 构建工具链** — 仍然是单 HTML + 多 CSS/JS 资产
- **不破坏旧业务 JS** — 所有 onclick / id / class 仍可被 18000 行 inline JS 引用
- **不开第二个浏览器入口** — 仍然 `GET /ui` 单地址

---

## 1. 与 V1 的差异（视觉/结构对照）

| 维度 | V1 现状 | V2 目标 |
|------|---------|---------|
| 容器策略 | 1px hairline + `box-shadow: var(--elev-1)` 双重 | **单一边框**（`border: 1px solid var(--line-default)`），阴影只在 hover/弹层 |
| 主导航 | 顶部 7 标签 + 滑动指示条 | **桌面：左侧 256/64 双态 Sidebar + 顶部 56px AppBar；平板/移动：保留旧顶部 tab，强化滑动指示条** |
| 标题节奏 | 编辑体 H2 + Eyebrow + 金线 | 保留，但 Eyebrow 移到 AppBar 面包屑；H2 字距收紧到 `-0.02em` |
| 按钮 | 黑底白字 .btn 重置 | **三档体系：primary / secondary / ghost / danger / icon**，hover 上抬 -1px |
| 输入 | 36px 高 + 3px focus ring (aurora 22%) | **40px 高**（更靠近 Linear/Vercel）+ 焦点环改为 1px aurora 边 + 2px 22% halo |
| 表格 | `.trace-table` 旧样式 + bridge 统一 | 行高升至 44px、表头大写微字距、列分隔保留 1px hairline、hover 整行 `--bg-soft` |
| Modal | `<div class="modal">` 自管理 + 半透明遮罩 | **改为 `<dialog>` 原生 + backdrop blur(18px) + spring scale 0.96→1**，esc 关闭与 focus trap 由原生处理 |
| 空状态 | 多处直接 `display:none` | 统一 `.empty-state` 组件：图标 / 短句 / 主行动按钮 |
| 加载态 | 全屏 spinner / 文字"加载中" | **Skeleton 占位**（按真实区域形状），spinner 仅用于按钮 |
| 通知 | `showStatus()` + 部分 toast | 全局统一 `window.toast()`（已有），废弃所有 `alert()` |
| 主题切换 | 右下角浮动按钮 + view transitions | 移到 AppBar 末尾的图标按钮，仍保留 view transitions 圆扩散 |
| 命令面板 | 未实现 | **`Cmd/Ctrl + K` 打开**，列出所有 Tab、所有保存按钮、主题切换、登出 |
| 快捷键 | 仅密码框 Enter | 全局：`g a` → 账户管理；`g c` → 配置；`?` → 快捷键面板 |

---

## 2. AppShell 重构

### 2.1 桌面布局

```
┌─────────────────────────────────────────────────────────────┐
│ ┌──────────┐ ┌────────────────────────────────────────────┐ │
│ │ Sidebar  │ │ AppBar:  Eyebrow › Page Title    🔍  ⌘K  ☾ ⏻│ │ 56px
│ │ (256px)  │ ├────────────────────────────────────────────┤ │
│ │          │ │                                            │ │
│ │ ⌂ Account│ │  Main content (scroll y)                   │ │
│ │ ⚙ Config │ │   max-width: 1280px, mx-auto               │ │
│ │ ⏱ Limits │ │                                            │ │
│ │ 📊 Usage │ │                                            │ │
│ │ ▶ Play   │ │                                            │ │
│ │ ⚡ Perf   │ │                                            │ │
│ │ 📜 Logs  │ │                                            │ │
│ └──────────┘ └────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

- Sidebar 图标使用 `data-icon`（已加载的 Lucide）
- 折叠态：256 → 64 px，文字消失，hover 时 popover label
- AppBar 中央：当前 Tab 的 Eyebrow + 标题（替代之前页内 H2）
- AppBar 右侧：搜索（占位）/ 命令面板按钮 (`⌘K`) / 主题切换 / 登出
- **关键约束**：保留 `<button class="tab" onclick="switchTab(...)">` 按钮，把它们藏在 sidebar nav-item 的 click handler 里转发

### 2.2 平板/移动

- ≤1024px：sidebar 自动折叠（仅 64px icon），点击展开 overlay
- ≤768px：完全隐藏 sidebar，恢复**顶部 segmented bar**（原 `.tabs` 样式更新到 V2）
- AppBar 标题 + 操作折叠为 `⋮` 菜单

### 2.3 实现策略

新增：
- `front/assets/css/v2/shell.css`（替代部分 `layout.css` 的旧 placeholder）
- `front/assets/js/v2/shell.js`（接管 Sidebar 折叠、当前 Tab 同步、AppBar Eyebrow 注入）

不动：原 `.tabs / #mainTabs` markup 仍存在但通过 CSS `position: absolute; visibility: hidden` 离屏，业务 JS 仍可调用 `switchTab()`。

---

## 3. 各 Tab 深化方案

### 3.1 配置管理（configTab）

| 现状 | V2 |
|------|-----|
| 表单 + 浮动保存按钮 + 导出/导入 modal | **左右分栏：左 60% 表单分组卡片（账户/网络/流式/配额/UI），右 40% 实时预览**：勾选项以 chip 显示当前值；改动未保存时整页右上角 sticky 浮窗"3 项未保存 [取消] [保存]" |
| 字段杂糅在长表单 | 按 section 拆 `.field-group`，每组顶部 mini eyebrow + 折叠按钮（默认展开） |
| switch 看起来像 checkbox | 真 toggle switch（已在 components.css，强化 spring 动效） |
| 提示信息 (.config-note) 灰色斜体 | callout：`info / warn / hint` 三色 + icon |

### 3.2 账户管理（accountTab）

| 现状 | V2 |
|------|-----|
| 顶部账户列表 + 子 tabs（详情/限制/账单/凭证 4 项） | **保留子 tabs 但改为 underline-style segmented bar**；账户列表改为左侧 280px 抽屉（可折叠），主区显示选中账户 |
| balance/spend 数字带红光闪烁 / 绿光流动 | 弱化：仅在数字变化时 200ms 颜色 pulse，不持续闪烁 |
| 卡片粒度大 | 拆为 KPI 行（4 个 metric cards：余额 / 当月消耗 / 请求数 / 成功率） |
| 账单图表 | 用现有 chart 但改主色为 aurora；hover tooltip 用玻璃态 |

### 3.3 速率限制（ratelimitTab）

| 现状 | V2 |
|------|-----|
| 表格 + 自动刷新开关 | KPI 头部（剩余/已用/限额）+ 卡片化 key 状态网格（每个 key 一个圆形进度环 + 状态徽章） |
| 自动刷新通过 setInterval | 加倒计时进度条："下次刷新 3s" |

### 3.4 使用统计（usageTab）

| 现状 | V2 |
|------|-----|
| 表格 + 筛选 select + 分页 + 导出按钮 | **头部 KPI 4 卡 + 双面板**：左 model breakdown donut（已有），右 daily trend line；表格作为详情区放在下方 |
| 筛选 select 自定义 wrapper | 改为统一 `.select` 组件（trigger + popover），快捷过滤 chip："本日 / 本周 / 本月 / 自定义" |
| 表格行 hover 平淡 | 整行 hover `--bg-soft`，"详情"按钮改为右侧悬浮 `→` 箭头 |
| trace 详情弹窗 | 改 `<dialog>` |

### 3.5 性能分析（performanceTab）

| 现状 | V2 |
|------|-----|
| KPI + 表格 + 单 trace 瀑布 modal | **保留三件套，但 KPI 加缓存创建 5m/1h（本期已加）；瀑布 modal 改为右侧抽屉（slide in）** |
| 段时间分布柱 | 改 sparkline，平滑过渡 |

### 3.6 操练场（playgroundTab）

| 现状 | V2 |
|------|-----|
| 大型表单：消息行编辑器 + JSON 编辑器 + 工具定义 + 模型选择器 + 流式开关 | **类 Vercel AI SDK Playground**：左 50% 输入区（消息时间线 + tool config 抽屉），右 50% 流式输出（diff 高亮 + 复制按钮 + 用量徽章） |
| 流式日志混在 textarea | 用 `.message-stream` 容器，role 标识 + thinking pulse + 工具调用折叠卡 |
| Token 计数 | 实时 sticky 显示在右上角 |

### 3.7 实时日志（logsTab）

| 现状 | V2 |
|------|-----|
| 表格行 + 4 级颜色标签 | **单列 monospace 行**（VSCode terminal 感）+ 等级前缀 chip + 时间戳灰；行尾 hover 显示"复制 / 跳转 trace"按钮 |
| 自动滚动 / 暂停按钮 | 沿用，按钮风格统一到 V2 |
| 过滤 | 顶部 filter chip 行：`DEBUG / INFO / WARN / ERROR` 互斥点亮 |

### 3.8 凭证 / 上传（manageTab + uploadTab）

| 现状 | V2 |
|------|-----|
| upload 拖拽区 + manage 表格 | 合并为单 Tab（accountTab 内子 tab "凭证"），上方拖拽区，下方表格；表格行支持批量勾选 + 批量启用/禁用/删除 |
| 备注、限制 modal | 改 `<dialog>` |

---

## 4. 组件升级清单

### 4.1 按钮（btn）

```html
<button class="btn btn--primary"   data-icon="save">保存</button>
<button class="btn btn--secondary" data-icon="upload">导入</button>
<button class="btn btn--ghost"     data-icon="refresh-cw">刷新</button>
<button class="btn btn--danger"    data-icon="trash-2">删除</button>
<button class="btn btn--icon"      data-icon="x" aria-label="关闭"></button>
<button class="btn btn--primary btn--loading">…</button>
```

- 高度 36 / 28 / 44（sm/default/lg），padding `0 var(--s-4)`
- primary 默认仍是 `--ink-pure` 黑底（坚持 Linear 风），danger 改用 `--sig-danger` 实色
- hover：`translateY(-1px)` + `box-shadow: var(--elev-1)`
- active：`translateY(0)` + `transform-origin: center`
- loading：spinner 16px 替换图标位

### 4.2 输入（input / select / textarea）

- 统一 40px 高（textarea auto）
- focus 环：`outline: 0; border-color: var(--aurora-1); box-shadow: 0 0 0 3px var(--aurora-halo)`（已有 `--elev-focus`）
- 错误：`border-color: var(--sig-danger); box-shadow: 0 0 0 3px var(--sig-danger-halo)`
- prefix/suffix slot：`<span data-icon="search">` 在前缀位

### 4.3 卡片（card / KPI）

```html
<section class="card">
  <header class="card__header">
    <h4 class="card__title">缓存读取 Tokens</h4>
    <span class="card__trend" data-trend="up">+12.4%</span>
  </header>
  <div class="card__body">
    <p class="card__metric">128,492</p>
    <p class="card__caption">最近 24h</p>
  </div>
</section>
```

- card：`background: var(--bg-pure); border: 1px solid var(--line-default); border-radius: var(--r-3); padding: var(--s-5)`
- hover：`border-color: var(--line-strong); transform: translateY(-1px)`（仅 interactive 卡片）
- metric 字号 `--fs-h3`，monospace 字体

### 4.4 表格（table v2）

- 表头：大写 `--fs-micro`，字距 `--tracking-widest`，灰色 `--ink-muted`，sticky top
- 行：高度 44px，border-bottom 1px hairline，hover `--bg-soft`
- 首列可 sticky；最后一列 sticky 到右（操作列）
- 排序：表头 `cursor: pointer` + `data-sort` 切换图标
- 密度切换：右上 `[紧凑/标准/宽松]`，绑定 `body[data-density="compact|cozy|comfy"]`

### 4.5 Modal → Dialog

- 全部 `.modal` 改为 `<dialog class="dialog">`
- 入场：`@keyframes dialog-in { from { transform: scale(0.96); opacity: 0 } to { transform: scale(1); opacity: 1 } }`，`var(--dur-fast)` `var(--ease-spring)`
- backdrop：`backdrop-filter: blur(18px); background: rgba(0,0,0,0.40)`
- esc 关闭原生支持，focus trap 原生支持

### 4.6 Toast 通知

已有 `window.toast({...})`，V2 增强：
- 同类多条合并（去重 by `key`）
- action 按钮："撤销" / "查看"
- 持久化（不自动关闭）选项给 error 类

### 4.7 命令面板（Cmd+K）

新增 `front/assets/js/v2/cmdk.js`：
- 全局 `Cmd/Ctrl + K` 打开
- 顶部搜索框，下方分组列表
- 命令来源：所有 Tab 切换、所有顶层操作（保存配置、刷新限速、清空日志、导出用量、登出、主题切换）
- 搜索算法：fuzzy match（实现 ~30 行）

### 4.8 快捷键面板（?）

按 `?` 弹出 dialog 列出全局快捷键。

---

## 5. 微交互与动效目录

| 场景 | 动效 |
|------|------|
| Tab 切换 | 内容区 fade + slight slide-up 4px，120ms |
| Sidebar 折叠 | 宽度 spring，文字 opacity fade 80ms 提前 |
| KPI 数字变化 | counting up 380ms（rAF），变化方向用绿/红色边 100ms pulse |
| 按钮 click | 转 `transform: translateY(0)` 50ms 内瞬时反馈 |
| 表格行 hover | bg 变化 80ms，transition only `background-color` 避免 layout |
| 弹窗入场 | spring scale + opacity 220ms |
| 主题切换 | 既有 view transitions 圆扩散 |
| 登录成功 | 既有彩带（保留） + view transition 切换主面板 |
| 错误状态 | shake 仅在 input 错误，其余用 toast |

---

## 6. 阶段路线图

| # | 阶段 | 工件 | 验收 |
|---|------|------|------|
| **本计划已完成** | 登录页层林尽染还原 | `pages.css` 删 510 行；`legacy-bridge.css` 装饰元素仅登录态显示 + #loginSection 例外区 | 浏览器登录页粒子/浸染/星点/浮光球可见，登录后全部消失 |
| 1 | **基础视觉令牌微调** | tokens.css 增加 v2 派生变量（`--bg-soft`、`--line-strong`、`--aurora-halo` 等） | 控制台所有页面 hairline 风格统一 |
| 2 | **AppShell（Sidebar + AppBar）** | `assets/css/v2/shell.css`、`assets/js/v2/shell.js`；HTML 注入新骨架包裹 #mainSection | 桌面端 sidebar 可见可折叠，旧 .tabs 仍可工作 |
| 3 | **按钮 / 输入 / 卡片 v2** | `components.css` 升级 selectors；新增 `.card__metric` 等子元素 | 所有 form / KPI 视觉一致 |
| 4 | **表格 v2** | 表头大写 + 行高 44 + hover；密度切换 toolbar | usage / performance / logs / manage 四张表风格一致 |
| 5 | **Modal → Dialog 迁移** | 逐 modal 替换为 `<dialog>`；hide()/show() 改为 `.showModal()/.close()` | 所有弹窗（密钥/限制/备注/确认/trace 详情）用原生 dialog |
| 6 | **配置管理重构** | configTab 左右分栏 + 未保存浮窗 | 改动检测、保存与原 fetch 一致 |
| 7 | **账户管理重构** | accountTab 抽屉 + KPI 4 卡 + 子 tab segment | 多账户切换、4 子页加载、账单导出 |
| 8 | **速率限制 + 使用统计 + 性能分析** | KPI 头部统一 + chip 过滤 + 双面板 | 数据展示无回归，导出 CSV/JSON OK |
| 9 | **实时日志 v2** | 单列 monospace 行 + chip 过滤 + 行操作 | WebSocket 仍连，4 级过滤 OK |
| 10 | **操练场 v2** | 左右分栏 + message timeline + 流式高亮 | 流式/非流式请求、工具调用、JSON 编辑 |
| 11 | **命令面板 + 快捷键面板** | `assets/js/v2/cmdk.js`、`assets/js/v2/hotkeys.js` | Cmd+K / ? 可用 |
| 12 | **响应式 + QA** | 媒体查询补全；E2E 手测清单 | 1280 / 1024 / 768 / 414 全断点视觉无损 |

---

## 7. 验收清单

### 7.1 功能等价（每阶段必跑）

- [ ] `python -m pytest -q` ≥ 187 passed
- [ ] `grep -c "fetch(" front/control_panel.html` 与基线相等
- [ ] 所有 onclick handler 仍可触发（`grep -c "onclick=" front/control_panel.html`）
- [ ] 浏览器控制台 0 报错
- [ ] 主题切换、登录/登出、tab 切换、保存配置、查看用量、查看日志：手动通跑

### 7.2 视觉一致

- [ ] 全部数字 / 代码使用 JetBrains Mono / Geist Mono
- [ ] 全部图标为 SVG（无遗留 emoji 在主面板，登录页除外）
- [ ] 全部按钮属于 `.btn--*` 五类之一
- [ ] 全部输入 40px 高，焦点环统一
- [ ] 所有卡片 1px hairline，无随机阴影
- [ ] 所有 hover/active 反馈在 80-220ms 内完成

### 7.3 性能

- [ ] LCP < 1.5s（本地）
- [ ] tab 切换帧率 60fps
- [ ] 实时日志 1000 条不卡顿

---

## 8. 回滚

- 每阶段 commit 前缀 `[ui-v2:phase-N]`
- 单阶段 `git revert`
- 全量回到 V1：`git revert <range>`；登录页若需回到 V1 极光：恢复 `pages.css` 的 78-590 段（保留在 git 历史中）
