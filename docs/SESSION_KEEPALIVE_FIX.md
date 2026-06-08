# 账户会话保活修复（多账号 token/session 续期）

## 现象

在"账户管理"里登录多个 AssemblyAI 账号后，过几天到一周，**部分账号的信息查不到了，
必须重新登录**才能恢复。

## 根因分析

账户管理通过 AssemblyAI Dashboard（基于 **Stytch** 的会话认证）查询账单/用量/成本/Key。
会话由三部分组成：

| 凭据 | 寿命 | 作用 |
| --- | --- | --- |
| `sessionJWT` | **固定 5 分钟**（Stytch 规定） | 每次请求的 Bearer |
| `session_token` | 长 | Stytch 不透明会话令牌 |
| `aai_extended_session` cookie | **长效（多天）** | 真正承载"保持登录"的滚动会话 |

浏览器保持登录的真实机制是：**每次带 cookie 访问 dashboard，服务端校验 Stytch 会话并通过
`Set-Cookie` 滚动 `aai_extended_session`**（必要时下发新的 `sessionJWT`）。只要会话被周期性
"使用"，它就一直前滚、不失效。

旧实现的后台保活循环 `_keepalive_loop` 有两个致命缺陷：

1. **只对"已存密码"的账号续期，且续期方式是用密码重新登录。**
   - 仅有长效 cookie、未存密码的账号被**完全跳过**——空闲时无人滚动它的 cookie，
     到了服务端寿命就失效。
   - 前端的保活定时器（每 3 分钟）**只 ping 当前账号、且仅在面板标签页打开时运行**，
     关掉面板后其它账号无人保活。

2. **对有密码的账号每 ~5 分钟用密码重新登录一次。**
   - 每账号每天约 288 次登录，一周近 2000 次，会触发 Stytch/AssemblyAI 的风控，
     开始返回 401/403/429。
   - 旧代码**一次 401 就永久删除存储的密码**，此后该账号退化为"仅 cookie"，而保活循环
     又不管"仅 cookie"账号 —— 于是 cookie 也无人滚动，最终失效。

两条路径都指向同一个结局：**空闲一段时间后长效 cookie 不再被滚动 → 服务端会话过期 →
本地 `expires_at`（旧为 7 天）到点把会话删掉 → 账号查不到，必须重新登录。** 这与"几天到
一周"的现象完全吻合。

> 补充：Stytch 的密码无关续期端点是 `POST /v1/sessions/authenticate`（用 `session_token`
> 或已过期的 `session_jwt` 即可换新 JWT 并延长会话）。但那是 Stytch 平台 API，需要
> AssemblyAI 的 Stytch 项目密钥，我们没有。对 AssemblyAI 的探测也证实：`/dashboard/api/*`
> 下除 `auth/authenticate`（密码登录）外没有可直接调用的会话刷新端点。**因此唯一可行的
> 无密码续期手段，就是带长效 cookie 访问 `/dashboard/*` 让服务端滚动 cookie。**

## 修复

核心思路：**把保活从"用密码反复登录"改成"用长效 cookie 无密码滚动"**，密码登录降级为
真正的兜底。

1. **新增 `_refresh_session_via_cookie`**：带长效 cookie、仅用 cookie 认证（不发可能过期的
   Bearer）发一个轻量 RSC 请求（`/dashboard/code`）触发服务端滚动 cookie；成功则捕获滚动后
   的 cookie/JWT 并落盘，失败（401/403）返回 None。

2. **`_renew_session` 改为 cookie 优先、密码兜底**：先尝试 cookie 无密码滚动；失败再用存储的
   密码登录。这样**仅有 cookie 的账号也能被续期**。

3. **`_keepalive_loop` 覆盖所有可续期账号**：只要账号有长效 cookie *或* 存有密码，就在
   JWT 临期、或 cookie 久未滚动（`COOKIE_KEEPALIVE_INTERVAL_SECONDS=30min`，即使 JWT 仍新鲜
   也定期滚动）时续期。让空闲账号的会话长期存活。

4. **抗风控**：密码登录加入退避（`PASSWORD_RENEW_BACKOFF_SECONDS=10min`），并且只有**连续多次**
   失败（`PASSWORD_RENEW_MAX_FAILURES=3`）才丢弃存储的密码，容忍偶发瞬时 401。

5. **扩展滚动 cookie 捕获**：`_capture_rolling_cookies` 现在同时捕获 `session_jwt`/`session_token`，
   并在 JWT 被滚动时同步刷新 `jwt_expires_at_ts`，使续期判定基于新 exp 工作。

6. **请求路径不再多发一次续期**：`_ensure_fresh_session` 对有长效 cookie 的会话不再预先发一次
   续期请求——真实查询本身就会以 cookie 认证发出并滚动 cookie。

7. **放宽本地兜底窗口**：`SESSION_EXPIRY_HOURS` 由 7 天提升到 30 天。真正的失效以服务端实际
   401 为准；本地窗口只防止"短暂离线/重启"后误删仍可恢复的会话。每次成功续期都会前滚该窗口。

## 评审补充（健壮性细化）

- **统一落盘**：`_refresh_session_via_cookie` 只更新内存，持久化统一交给 `_renew_session`，
  避免重置失败计数时重复写一次存储。
- **续期判定一致性**：本轮 cookie 滚动若未拿到可用新 JWT，则丢弃陈旧 JWT、按刷新时间
  （`logged_in_at`）估算新鲜度，避免"刚续期又被立即判为陈旧"导致的反复续期；滚动到无法
  解析 exp 的 JWT 时同步刷新 `logged_in_at`（两个调用方都受益）。
- **按需恢复绕过退避**：密码续期退避只用于给后台保活降频；用户真实请求遇到 401 时
  （`_make_dashboard_request`）以 `force_password=True` 绕过退避做一次按需恢复，避免一次
  瞬时密码 401 + cookie 同时失效时在退避窗口内把用户登出。
- **升级/恢复兼容**：本地兜底窗口到期但仍有恢复手段（长效 cookie / 凭据）时不硬删，而是
  前滚窗口并保留，交给真实请求/保活去验证。这覆盖了"旧版以 7 天 `expires_at` 落盘、cookie
  仍有效"的升级场景，避免这些可恢复会话在保活滚动之前被误删。

## 效果

- 关闭面板后，**所有账号**（含仅 cookie、含多账号）都由后台每 ≤30 分钟无密码滚动一次会话，
  长效 cookie 持续前滚，不再"几天后失效"。
- 不再每 5 分钟用密码登录，杜绝高频登录触发风控/锁号。
- 即使密码偶发失败也不会立即永久关闭自动续期。
