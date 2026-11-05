# v5 安全处置记录（2026-11-04）

按用户要求执行的三项决定与对应处置。**本文不含任何明文凭证。**

## 用户决定

1. **Dashboard 公网开放**：`dragon-dashboard.service` 绑定 `0.0.0.0:8765`（不走 SSH 隧道，直接公网访问）。
2. **账号密码还原**：登录 = 服务器 root 账密（用户不再记多个密码）。凭证只存
   PBKDF2-SHA256 哈希（`/root/dragon-dashboard/dragon/backend/.credentials.json`，600 权限），
   源码与文档零明文。
3. **密码只从 git 里清除**：不轮换密码本身，改写两个仓库的历史。

## 处置内容

### 前端登录改造（公网开放下必须）

公网开放后发现前端 8 处把 Basic 凭证 `btoa('root:…')` 写死在源码与构建产物里，
静态文件无鉴权 → 任何人可下载 JS 解出密码。改造：

- 新增 `dragon/frontend/src/api/auth.js`：运行时索取凭证
  （用户名存 localStorage、密码存 sessionStorage、401 自动重新登录）；
- 8 个 Vue/JS 文件的写死凭证全部替换为 `getAuth()`，重新构建 dist；
- `auth.py` 中间件调整：静态页面放行、`/api/*` 保持 basic auth
  （避免浏览器原生登录框 + 前端登录框双重输入）。

### git 历史脱敏（两个仓库）

| 仓库 | 范围 | 结果 |
|---|---|---|
| `/root`（dragon-dashboard，远端 github.com:LJL123123123/dragon-.git） | 10 个 commit 全量重写 + 强推远端 | 全历史、工作区、commit message 均无残留 |
| `/opt/qq-fish`（本地仓库，无远端） | 全量重写（含 tags） | 同上，tags 完整保留 |

- 替换规则：`【原密码字符串】` → `[REDACTED]`
- **commit 号变化**：`a6fcfde` → `a88faf0`、`3053f89` → `d24bf18`
  （其余含密码的旧对象已随 gc 清除）；文档中旧引用以此为准。
- 远端为强推（forced update）：若其他机器有该仓库的克隆，需重新 clone。

### 附带修复

- 上一轮"凭证轮换"产生的 `admin` 随机密码已废弃，`/root/DASHBOARD_LOGIN.txt`
  改为指引文件（不含密码）。

## 备份（确认无误后可删）

- `/root/git_backup_pre_scrub.bundle`（dragon-dashboard 旧历史，含脱敏前内容）
- `/root/git_backup_qqfish_pre_scrub.bundle`（qq-fish 旧历史，同上）

```bash
rm /root/git_backup_pre_scrub.bundle /root/git_backup_qqfish_pre_scrub.bundle
```

⚠️ 这两个 bundle 是脱敏前的历史快照，仅供回滚；放在 root-only 目录下，
能读到它的进程已经拥有 root 权限。建议确认运行正常后删除。
