# 来源可靠性：source guard、附件归档与 filesystem backup

这一层故意拆成三项互不冒充的责任：

1. 可选 WeChat source guard：只负责在安全状态下请求 macOS 正常打开微信；
2. Attachment catalog 与本机私有 content-addressed archive；
3. 可选、provider-neutral 的 filesystem snapshot target。

它们不是一个永不退出的“大守护进程”。`TopicMonitor` 负责读消息与 checkpoint，
不 import、也不调用 source guard。Knowledge transaction 负责登记 attachment mention；
commit 之后的 worker 才负责找 bytes 和复制。Backup 命令只读本机 immutable archive object，
并且只写 configured filesystem target。

## 1. 可选 WeChat source guard

Source guard 默认关闭。开启 policy、安装 LaunchAgent plist、加载 LaunchAgent 是三个分开的动作：

```bash
.venv/bin/python scripts/wechat_source_guard.py status
.venv/bin/python scripts/wechat_source_guard.py enable
.venv/bin/python scripts/wechat_source_guard.py install-agent
.venv/bin/python scripts/wechat_source_guard.py install-agent --load-now
```

`install-agent` 写入一个独立的 one-shot `StartInterval` job，plist 没有 `KeepAlive`。
不加 `--load-now` 就不会 bootstrap。每一次调用只做一次 check：尝试获取 non-blocking lock，
写入很小的私有 state，然后退出。构建 plist 时如果设置了
`WE_GROUPCHAT_OBSIDIAN_DATA_DIR`，LaunchAgent 会显式保留这个 override。

只有 process lookup 明确确认微信不在运行时，guard 才会先进入 grace。Grace 结束后，
还必须同时满足 restart budget 与 exponential backoff，唯一可能发出的启动请求等价于：

```text
open -g -a WeChat
```

Guard 不会 kill 微信，不修改或 re-sign app，不提取 key，不操作 UI，不负责登录，不抢 focus，
也不推进 monitor checkpoint。如果当前进程看不到 macOS process list，状态是
`process_lookup_unknown`，guard fail-closed，不会启动任何东西。微信仍在运行但本地数据库 stale 时，
每个 stale episode 只通知一次；一次 fresh observation 会结束该 episode。Stale 不是 restart 授权。

有效状态如下：

| 状态 | 含义 |
| --- | --- |
| `disabled` | Policy 关闭，不可能请求启动。 |
| `healthy` | 已明确确认微信在运行；source freshness 单独报告。 |
| `missing_grace` | 已确认进程不存在，但 grace 还没有结束。 |
| `restart_backoff` | 已请求一次正常启动，或正在等待下一次 backoff。 |
| `paused` | 用户设置的定时/无限 pause 阻止启动。 |
| `degraded` | Restart budget 耗尽，或 launch command 多次失败。 |
| `process_lookup_unknown` | 无法读取 process list；不能把 unknown 当 absent。 |

Pause、resume、单次 check、disable 与卸载都要显式执行：

```bash
.venv/bin/python scripts/wechat_source_guard.py pause --hours 8
.venv/bin/python scripts/wechat_source_guard.py pause --indefinite
.venv/bin/python scripts/wechat_source_guard.py resume
.venv/bin/python scripts/wechat_source_guard.py check
.venv/bin/python scripts/wechat_source_guard.py disable
.venv/bin/python scripts/wechat_source_guard.py uninstall-agent
```

State 与 receipts 位于项目 runtime data directory：目录权限 `0700`，文件权限 `0600`。
Receipt 只含状态跳转、budget/backoff 数值与 error code，不含群名、username、消息内容或附件路径。
Degraded/launch 通知仍按 key 去重并有 cooldown；stale-source 通知按 episode 去重。

## 2. 稳定 message/resource identity

每次读取消息时，代码先 introspect 实际 WeChat table schema，再构建 query。实际存在时，
source envelope 会保留 `local_id`、`server_id`、`sort_seq`、SQLite `rowid`、shard identity、
`create_time` 与 `local_type`；缺失的 optional column 显式保持 null。稳定的
`source_message_id` 由这些值和 chat username 的 hash 派生，不暴露 username 或 `wxid`。

File XML 与 image packed metadata 在 message text cleaning 之前解析。Resource envelope 可以保存
原文件名、declared size、declared MD5/SHA-256、attach id、extension 或 image hash。
AI formatter 只接收 sender 与 cleaned message text；source envelope 和 internal IDs 不会进入 AI prompt。

## 3. Attachment catalog 与本地 archive

一次 Knowledge 命中里，`attachment_mentions` 与对应 `events` row 在同一个 SQLite transaction
里插入。`attachment_objects` 是 canonical object catalog，`attachment_attempts` 是 content-free
attempt history。Event commit 后，app 可以启动 daemon thread 消费 pending mentions；
process-level archive lock 防止两个 worker 重复复制。Fresh `pending` 永远先于 retry；可自动重试的
失败会写入 `attempt_count` 与 `next_retry_at`，使用有上限的 exponential backoff，并且只有 due row
才会再被选中。每个 trigger 在抢 lock 前持久化 wake generation；active worker 会持续 drain batch，
直到没有 due work 或未消费 wake。

这条分离边界保证：

- cache resolver 或 copy 失败不能回滚已提交 event；
- 失败不会再次调用 AI；
- 失败不会倒退或额外推进 monitor checkpoint；
- retry 只修改 catalog state。

本地 archive 默认关闭。需要显式 enable 并选择资源类型；图片仍是单独 opt-in：

```json
{
  "attachment_archive_enabled": true,
  "attachment_archive_kinds": ["file", "image"],
  "attachment_archive_max_object_bytes": 536870912,
  "attachment_archive_min_free_bytes": 1073741824,
  "attachment_archive_retry_base_seconds": 300,
  "attachment_archive_retry_max_seconds": 21600
}
```

较新的 WeChat V2 image object 还需要本机捕获的 `image_aes_key`；这个 key 不会写入 attachment
catalog、archive receipt、backup manifest 或文档。

### 文件 resolver

Resolver 只搜索消息声明月份下的 WeChat `msg/file` cache，候选仅限 exact name 和常见
`name (N).ext` duplicate variant。有 declared size 或 MD5/SHA-256 时，先用 metadata 筛选。

- 只有一个有效候选：选中；
- 多个候选 bytes 完全相同：视为 equivalent duplicates，可 deterministic 选中；
- 多个候选 bytes 不同：`ambiguous`；
- 没有有效候选：`missing_retryable`。

目录顺序与 modification time 从不承担 identity。Symlink、non-regular file、以及 cache root
之外的候选都会被拒绝。

### 图片 resolver

图片只按 structured image hash、hashed chat directory 和 source month 定位，优先 full/original
suffix，再看 thumbnail suffix；完全没有 mtime fallback。结果明确区分：

- `original_archived`；
- `thumbnail_only`；
- `decode_unavailable`；
- `missing_retryable`；
- `ambiguous` 或对应 source rejection/failure 状态。

### Content-addressed storage

Object 存在：

```text
~/.we-groupchat-obsidian/attachment_archive/
  objects/sha256/<first-two-hex>/<sha256>--<safe-original-name>
  tmp/
```

Root 与 object directory 是 `0700`，final object 和 worker lock 是 `0600`。Copy 先写私有 partial，
边写边算 SHA-256，读取前后检查 source identity/size/mtime，随后 `fsync` 并 atomic rename。
Crash 后如果 final object 已存在但 catalog row 还没提交，下次可按 digest 复用；worker-owned partial
不会被当作 object。

SHA-256 是 identity，所以不同文件名引用同一份 bytes 时只保留一个 immutable object。
Markdown 的资源区会显示 catalog status、可用时的本地 object link，以及既有月份目录 hint。

复制前，worker 会拒绝大于 `attachment_archive_max_object_bytes` 的 object，并保证拟写入之后仍
保留至少 `attachment_archive_min_free_bytes`。Oversized object 需要显式调整 policy 后 manual retry；
low-space failure 进入正常的 due-only retry schedule。

一个很重要的 storage boundary：archive **不会删除或 prune 微信自己的 cache**。它避免在 archive
里重复存同一份 bytes，却不会自动回收 source cache 占用。破坏性的 cache retention/pruning 不属于
这一 tranche，必须另行设计并审查。Backup target 也会再占用一份 target storage。

### Archive CLI

```bash
.venv/bin/python scripts/attachment_archive.py status
.venv/bin/python scripts/attachment_archive.py run --limit 50
.venv/bin/python scripts/attachment_archive.py retry --mention-id <id>
.venv/bin/python scripts/attachment_archive.py retry --mention-id <id> --run
```

历史回填严格 plan/apply 分离：

```bash
.venv/bin/python scripts/attachment_archive.py backfill
.venv/bin/python scripts/attachment_archive.py backfill --apply
.venv/bin/python scripts/attachment_archive.py backfill --apply --run
```

第一条只读历史 `events.files_json` 并报告 counts，不插入 mention，也不复制 bytes。
`--apply` 才显式插入缺失的历史 catalog row；再加 `--run` 才继续消费 pending rows。
`--limit` 是 batch size，不是本次总上限；worker 一旦获得 lock，会在退出前 drain 所有 fresh 与
当前已 due 的 rows。

## 4. 可选 filesystem backup target

Backup layer 只认识 filesystem path。它没有 Google Drive、Dropbox、iCloud 或其他 provider API，
不保存 OAuth token/provider credential。Target 可以位于 provider desktop sync folder 内，
但真正 provider upload 在本项目 authority 之外。

显式设置或清除 target：

```bash
.venv/bin/python scripts/attachment_backup.py set-target "<filesystem-target>"
.venv/bin/python scripts/attachment_backup.py clear-target
```

Target layout 完全 provider-neutral：

```text
<target>/v2/
  objects/sha256/<first-two-hex>/<sha256>
  snapshots/<snapshot-id>/manifest.json
  snapshots/<snapshot-id>/catalog.json
  snapshots/<snapshot-id>/COMPLETE
  receipts/<snapshot-id>.json
```

`plan` 对 `attachment_objects` 与 privacy-bounded attachment catalog 取 stable read view，检查
target object 是 missing、`target_verified` 还是 `target_failed`，不写 target。`run` 用 partial file
复制缺失 immutable object、验证 source hash 并 atomic publish target object。只有全部 object 成功，
才写 `manifest.json` 与 `catalog.json`，最后发布 `COMPLETE`。Catalog 包含 object SHA-256/size、原始
名称/类型、`source_message_id`、numeric topic/event binding、status 与 resolution method；明确不含
raw chat body、`wxid` 或 WeChat cache/archive path。失败时仍会写 content-free receipt 供
reconciliation。发现 target 上同 digest path 的 bytes 冲突时不会覆盖，unmanaged target files
也永不 prune。

`plan` 与 `run` 都会拒绝 filesystem root，以及与本地 archive/database 相同、位于其内部或作为其
ancestor 的 target。检查同时比较 lexical path 与 resolved path，因此 symlink escape 在任何 target
写入之前也会被拒绝。

```bash
.venv/bin/python scripts/attachment_backup.py status
.venv/bin/python scripts/attachment_backup.py plan
.venv/bin/python scripts/attachment_backup.py run
.venv/bin/python scripts/attachment_backup.py verify
.venv/bin/python scripts/attachment_backup.py verify --snapshot-id <id>
.venv/bin/python scripts/attachment_backup.py restore-plan
.venv/bin/python scripts/attachment_backup.py restore-plan --snapshot-id <id>
```

`verify` 重新 hash target 当前可见的 objects，只报告 `target_verified` 或 `target_failed`；不会报告或暗示
cloud-upload verification。`restore-plan` 是 read-only，只统计 target 上已验证、但本机缺失/损坏
的 object 数量与 bytes；它读取 snapshot catalog 并直接扫描本地 CAS，因此本地 Knowledge
DB/catalog 缺失时仍能工作。这一 tranche 故意没有 automatic restore 或删除功能。

## 5. Health 与安全 rollout

Redacted health check 现在会报告 source guard effective state、last result、剩余 restart budget、
source freshness、attachment catalog counts/object count，以及 optional backup target 是否存在
complete snapshot：

```bash
.venv/bin/python scripts/health_check.py
```

第一次安全 rollout 建议按下面顺序：

1. 先看 `wechat_source_guard.py status` 和 `attachment_archive.py status`；
2. 在 policy values 没审完前保持 source guard disabled；
3. 历史附件只跑 `backfill` plan；
4. 配置 backup target 后先跑 `attachment_backup.py plan`；
5. 逐项审查本机隐私和计划结果，再分别执行明确的 apply/run/load；
6. 激活任何变化后，重新跑 health 与 backup `verify`。

安装/加载 source guard、历史 backfill apply、写真实 backup target、清理微信 cache、以及断言 provider
上传成功，始终是五个不同的 operational actions。Source availability、本机保存、target-byte
verification 与 remote sync 也是四个不同事实。
