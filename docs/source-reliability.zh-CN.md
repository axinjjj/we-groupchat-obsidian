# 来源可靠性：source guard、本地 CAS、mounted backup、可选 Drive API 与 filesystem snapshot

这一层故意拆成五项互不冒充的责任：

1. 可选 WeChat source guard：只负责在安全状态下请求 macOS 正常打开微信；
2. Attachment catalog 与本机私有 content-addressed archive；
3. 默认 no-OAuth selected-resource mounted handoff；
4. 可选 advanced selected-chat Drive API sync；
5. 面向更广 attachment archive 的 provider-neutral filesystem snapshot target。

它们不是一个永不退出的“大守护进程”。`TopicMonitor` 负责读消息与 checkpoint，
不 import、也不调用 source guard。Knowledge transaction 负责登记 attachment mention；
commit 之后的 worker 才负责找 bytes 和复制。Mounted backup 只读本机 immutable archive object，
并且只写自己的 configured filesystem target。两条 selected-chat scanner 各有独立 cursor/selection；
即使消息没有 Knowledge hit，也可以复用同一个本地 CAS。

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

Process-list availability 通过读取 guard 当前进程自身来证明，不依赖普通用户 session
能否看见系统级 `launchd`。Source freshness 只对 cached key inventory 中已知的
`message/` shard 路径及其 WAL/SHM siblings 做 exact stat；它不会递归遍历 DB root 或
微信 hardlink/cache tree，因此 one-shot 在 LaunchAgent context 下仍然有界。

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

## 4. 默认 selected-resource mounted backup

这是默认 Google Drive 路径：把 selected-chat links、selected file occurrences 与共享 CAS bytes
交给已经存在的 mounted filesystem，例如 Google Drive for Desktop。它不创建 Google Cloud project、
不索取 OAuth credentials、不调用 Drive API，也不做 browser automation。

```text
active monitor chats
  intersect resource_backup_selected_chats
  -> 每群 × message-shard occurrence capture
  -> exact URL metadata + 共享本地 SHA-256 CAS
  -> 本地 Obsidian resource index
  -> mounted target objects / catalog snapshots / views
```

Mounted lane 与可选 direct API lane 的 disclosure selection 完全独立：
`resource_backup_selected_chats` 只控制 mounted lane；
`google_drive_file_sync_selected_chats` 只控制可选 API lane。选择一边不会启用另一边。

### Private selection 与本地 policy

Mounted-backup defaults 全部 private、opt-in：

```json
{
  "resource_backup_selected_chats": [],
  "resource_backup_interval_seconds": 300,
  "resource_backup_max_messages_per_scan": 500,
  "resource_backup_min_free_bytes": 1073741824
}
```

先列出 active monitor chats；输出不会打印 raw `@chatroom` identifier。再用列表编号替换 mounted
backup selection：

```bash
.venv/bin/python scripts/resource_backup.py list-chats
.venv/bin/python scripts/resource_backup.py set-selected-chats 1
.venv/bin/python scripts/resource_backup.py clear-selected-chats
```

Target 与 link-export policy 存在独立 private `resource_backup.json` 中。目标目录必须已经存在；
worker 不会在 mount 缺失时把那个路径重新创建成普通本地目录。

```bash
.venv/bin/python scripts/resource_backup.py set-target "<已经存在的挂载目录>"
.venv/bin/python scripts/resource_backup.py set-link-export-mode redacted
.venv/bin/python scripts/resource_backup.py init
.venv/bin/python scripts/resource_backup.py status
.venv/bin/python scripts/resource_backup.py plan
.venv/bin/python scripts/resource_backup.py run --resolve-limit 10
.venv/bin/python scripts/resource_backup.py verify
```

`init` 只初始化 from-now cursors。`run` 捕获 deterministic occurrences、把 due files resolve 到共享
CAS、即使 target 不可用也继续刷新本地 Obsidian index，然后才尝试 mounted handoff。

```text
<target>/wgo-resource-backup/v3/
  objects/sha256/...
  snapshots/<snapshot-id>/{manifest.json,resources.jsonl,COMPLETE}
  views/<chat>/...
```

Plan 与 run 都拒绝 filesystem root、与本地 source 相同/嵌套/祖先关系的 target、configured-target
symlink，以及 app-owned subtree 中的 symlink/non-directory component。第一次复制会边写边 hash，并立即
readback target bytes。后续 scheduled run 只信任本地 delivery receipt、regular-file type 与 logical size，
避免重新 hydrate streamed placeholder；显式 `verify` 才完整 rehash target。

`sync_delegated` 只表示 resolved bytes 已写入 mounted filesystem 并立即验证；它绝不表示 provider-side
upload 或 remote checksum verification。如果仍有 eligible file unresolved，系统可以发布 hash-bound
`COMPLETE` catalog snapshot，但 run state 必须是 `pending_resources`，manifest 记录
`snapshot_completeness=catalog_complete`，CLI 非零退出。`COMPLETE` 绑定 catalog，不会凭空补出缺失 bytes。

一个 manual canary 从另一 Drive surface 验证以后，才显式安装短命 scheduler：

```bash
.venv/bin/python scripts/resource_backup.py install-agent --interval-seconds 300
.venv/bin/python scripts/resource_backup.py agent-status
.venv/bin/python scripts/resource_backup.py uninstall-agent
```

Agent 使用 `RunAtLoad + StartInterval`、`ProcessType=Background`、`LowPriorityIO`，没有 `KeepAlive`；
每次 wake 只运行一个 bounded process 然后退出。安装是 activation action，merge source 不会自动安装。

## 5. 可选 advanced selected-chat Google Drive API 直传

这是为明确需要 Drive-native object/shortcut 的用户保留的可选 advanced transport。它独立于
`TopicMonitor`、Knowledge selection、source guard 与 filesystem snapshot：

```text
用户选定的微信群聊
  -> 每群 × message shard scan cursor + durable file-message queue
  -> 既有精确 resolver + 共享本地 SHA-256 CAS
  -> 每份唯一 bytes 一个 Drive object
  -> 群聊 / source month 的 Drive shortcut projection
```

第一版只接受 source envelope 中 `kind=file` 的真实文件。图片、语音、视频与表情不会进入这条 queue。
Scanner 会跨过缺失文件与非 file 消息继续推进，因此一个暂时没有自动下载到微信 cache 的文件不会堵住
后面的消息。Cursor 是 per-chat × privacy-safe message-shard cursor：某 shard 失败时只把该 shard 记为
`source_degraded` 并保持 cursor 不动，健康 shard 仍可推进；恢复后漏读文件只会入队一次。普通
`WeChatDB.get_messages()` 也使用 strict all-known-shards contract，任何已知 shard 失败都不会返回伪完整的
剩余 rows。Receipt 和 health 只记录 content-free error code 与 degraded shard count，不记录 raw path、
chat ID 或 database name。

`missing_retryable` 只在 due 时按有上限的 exponential backoff 重试；`ambiguous` 不猜测、不上传。

第一次 enable 会把当前已选择群聊的 cursor 初始化到“现在”，不会静默上传全部历史。历史发现严格是另一条
plan/apply action。把某个群聊从 selected set 移除时，其 cursor、queue 与 retry state 都保留，但该群 pending
item 会停止 resolve/upload；重新选中后再继续。

### 本地 config 与独立 ledger

Public 默认全部关闭：

```json
{
  "google_drive_file_sync_enabled": false,
  "google_drive_file_sync_paused": false,
  "google_drive_file_sync_selected_chats": [],
  "google_drive_file_sync_interval_seconds": 300,
  "google_drive_file_sync_max_messages_per_scan": 500,
  "google_drive_file_sync_max_uploads_per_run": 20,
  "google_drive_file_sync_max_bytes_per_run": 536870912,
  "google_drive_file_sync_root_name": "微信群文件归档",
  "google_drive_file_sync_keep_local_objects": true
}
```

Selected chat username 只存在 private local config 和独立本机 ledger。Remote chat identity 是由随机本地
`archive_id` 加盐派生的 SHA-256 key；Drive metadata 永远不写 raw `@chatroom` username。每个群聊 × shard
cursor 保存 timestamp 和该 timestamp 已见过的完整 message identity set，因此同秒分页、restart 都不会漏或重。
`(source_message_id, resource_index)` 全局幂等。这套 DB 与 run receipt 不保存 raw chat body。

`drive_scan_state` 保存 enable-time chat seed；canonical 增量位置保存在 `drive_scan_shards`。其余主要表是
`drive_sync_items`、`drive_objects`、`drive_placements`、`drive_folders` 和 content-free
`drive_sync_runs`。Menu timer、CLI 与 app startup recovery 都调用同一个
带 non-blocking process lock 的 one-shot worker，不新增 infinite loop。Disable/pause 后不开始新的 scan/upload；
如果开关在一个文件处理中改变，当前单文件安全结束，worker 不再取下一项。

`attempt_count` 只计算 retry failure；`uploading -> shortcut_pending -> complete` 等成功状态转换不再增加
exponential backoff。成功跨过 resolve、object 或 shortcut phase 时会重置该 phase 的连续失败计数。

### OAuth 与 credential boundary

Auth 使用 Installed desktop app OAuth 2.0、system browser、loopback callback 和 PKCE。唯一 scope 是：

```text
https://www.googleapis.com/auth/drive.file
```

用户自己的 OAuth client JSON 会被 normalize 到 private runtime directory，权限 `0600`；它不进入普通
config 或 tracked source。Refresh token 只存 macOS Keychain，access token 只在内存里；请求 offline access。
不支持 service account。`auth-status` 会实际验证 refresh token，并区分 `token_present` 与 `connected`；
`invalid_grant` 会清除已经无效的 Keychain token，之后不会继续报告 connected。Item 进入
`auth_required`，该 episode 只通知一次，queue 不丢。`disconnect` 只删除 Keychain refresh token，不删除
config、queue、CAS 或 Drive 文件。

### Drive identity 与 projection

第一次成功 remote run 会创建或 adopt 一个 app-owned root，默认可见名称是 `微信群文件归档`。Drive file ID
才是 authority：用户改名或移动不影响。已知 root 被 trashed、丢失、invalid 或权限不可读时进入
`remote_degraded`，不会偷偷建第二个 root。

```text
微信群文件归档/
  群聊/<stable-local-alias>/<source YYYY-MM>/<Drive shortcut>
  _系统/objects/<sha256-prefix>/<full-sha256>--<safe-original-name>
```

本地 SHA-256 是 canonical byte identity。上传前先查 local object ledger；ledger 不确定时再按
`appProperties` 搜索。因此 remote create 成功、local commit 前 crash，下次会 adopt 已存在 object/shortcut，
不会重复。多个 remote object 命中同 digest 时选择一个 canonical Drive ID，记录
`remote_duplicate_detected`，不上传第三份。Verification 优先比较 `sha256Checksum`；字段缺失时必须同时
匹配 size 与 `md5Checksum`，才写 `uploaded_verified`。

超过 5 MiB 的 object 使用真正的 resumable session：每个 `308` 都按 response `Range` 的 server-confirmed
offset 续传；network/429/5xx 后先用空 PUT 与 `Content-Range: bytes */<total>` 查询 session。Probe 若表明
lost final response 实际已完成，会直接采用完成 response；404 expired session 在同一 one-shot run 中最多
重建一次。Malformed/missing/regressing Range 明确失败，不会把整 chunk 猜成已接收。Session URI 不跨进程
持久化：进程中断后的下一 run 重新建 session，并仍通过 remote `appProperties` adopt 已完成 object；这是
明确的 bounded restart policy，不是 blind chunk restart。

同一 object 在全局只上传一次。Placement identity 是
`(hashed_chat_key, source_month, sha256)`：同 bytes 出现在另一群或另一月份只加 shortcut。同名不同 hash
使用 `stem--<hash8>.ext`。普通 child folder 或 shortcut 被删除后，`reconcile` 可重建；程序永不自动删除或
trash Drive item。

Remote `appProperties` 只含 schema version、随机 archive ID、role，以及相应 SHA-256、hashed chat key 和
source month。完整 source-message provenance 只留在本机。Google Drive 会收到被选群聊的 configured
alias、原始/安全文件名与文件 bytes；不会通过这条 lane 收到 raw chat username、raw XML/body、
`source_message_id`、`wxid` 或本机 WeChat cache path。

### CLI 与菜单控制

Auth、选群、enable、历史 backfill 与真实 upload 是五个分开的动作：

```bash
.venv/bin/python scripts/google_drive_file_sync.py auth --client-secrets "<installed-desktop-client.json>"
.venv/bin/python scripts/google_drive_file_sync.py auth-status
.venv/bin/python scripts/google_drive_file_sync.py status
.venv/bin/python scripts/google_drive_file_sync.py enable
.venv/bin/python scripts/google_drive_file_sync.py disable
.venv/bin/python scripts/google_drive_file_sync.py pause
.venv/bin/python scripts/google_drive_file_sync.py resume
.venv/bin/python scripts/google_drive_file_sync.py scan
.venv/bin/python scripts/google_drive_file_sync.py run
.venv/bin/python scripts/google_drive_file_sync.py reconcile
.venv/bin/python scripts/google_drive_file_sync.py backfill --from YYYY-MM-DD
.venv/bin/python scripts/google_drive_file_sync.py backfill --from YYYY-MM-DD --apply
.venv/bin/python scripts/google_drive_file_sync.py disconnect
```

`backfill` 不带 `--apply` 时完全只读。`enable` 只初始化当前 selected chat cursor seed，不会 auth、选群、运行
backfill 或上传。菜单栏的 **Google Drive 群文件备份** submenu 提供 status、enable/disable、pause/resume、
立即同步、选择群聊、打开 root 与重新授权；选择群聊本身也不会 enable 或 upload。

CLI 的 `auth-status` 会验证 refresh token；`auth_required`、`retry_wait`、`remote_degraded`、
`source_degraded` 和 failed one-shot result 返回非零 exit status，避免 shell 或 scheduler 把错误 JSON 当成功。

## 6. 可选 filesystem backup target

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

Archive root 现在持有 provider-neutral `cas_catalog.db`，记录唯一 SHA-256 object identity 与
content-bounded source binding；Knowledge attachment lane 与 selected-chat Drive lane 都写同一个 catalog，
不复制 bytes、不创建第二种 object identity。Filesystem snapshot 对该 catalog 与 Knowledge attachment
catalog 取 authoritative union，因此从未命中 KnowledgeStore 的 Drive-only selected-chat object 也会进入
plan/run/verify 和 DB-free restore plan，同时保留原有 topic/event binding 与 privacy-bounded export。

`plan` 对 provider-neutral CAS objects 与 privacy-bounded attachment catalog 取 stable read view，检查
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

## 7. Health 与安全 rollout

Redacted health check 会报告 source guard、source freshness、attachment catalog、optional attachment
snapshot 与 privacy-safe optional direct-Drive state。Mounted resource backup 当前有独立 status surface：

```bash
.venv/bin/python scripts/health_check.py
.venv/bin/python scripts/resource_backup.py status
```

第一次安全启用 mounted backup 建议按下面顺序：

1. 保持 optional direct API lane disabled；
2. 用 `list-chats` 查看 active chats，按编号只选择一个无敏感 canary，再执行 `init`；
3. 在 mounted provider root 下选择一个已经存在的目录；
4. 先跑 `plan`，然后在 from-now cursor 之后发送一个无敏感 link 与一个 small file；
5. 显式运行一次 `run` 与 `verify`，检查 occurrence/CAS、Obsidian index、mounted object 与 catalog snapshot；
6. 从另一个 Drive surface 确认 provider-side arrival，因为 `sync_delegated` 不是 remote verification；
7. 最后才安装 300 秒短命 resource-backup agent。

可选 advanced Direct Drive API rollout 仍然独立：

1. 准备自己的 Google Cloud Installed desktop app OAuth client JSON；
2. 只执行 `auth`，用 `auth-status` 确认，不 enable；
3. 在菜单选择群聊，检查将显示到 Drive 的 stable alias；
4. 如果需要历史，只跑一次 `backfill --from ...` dry plan；
5. 执行 `enable`，再手动 `run` 一次，检查本地 `status` 与 Drive root；
6. 审查 counts 后才决定是否运行历史 `--apply`；
7. 激活后重新跑 redacted health check。

Source guard 与更广 attachment filesystem snapshot 各有自己的 rollout：先看 status/plan，未单独审查前
保持 disabled 或 unconfigured。

安装/加载 source guard、选择 mounted-backup chats、写真实 mounted target、安装 resource agent、为可选
API lane 做 Google auth/选群/enable、执行任何历史 backfill、清理微信 cache 与删除 Drive 文件，始终是
分开的 operational actions。Source availability、本机保存、mounted target-byte verification、File
Provider upload state 与已验证的 Drive API object/shortcut state 也是不同事实。
