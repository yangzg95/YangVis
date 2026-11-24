# 素材库（账号级图片/视频资产与回收期）

素材库是账号级的文件资产：图片与视频按账号归在 `materials/<owner_id>/` 树下，
跨所有短视频项目复用，删项目不碰素材。镜头通过 `video_shot.material_ids`
（JSON 字符串 id 列表，一镜最多 4 条）引用素材，库里那一行是文件与名字的唯一事实源。

与人物库（[characters.md](characters.md)）的分工：素材是**无结构的图/视频文件**，
角色是带文字档案的人物；两者都是账号级资产、按 owner 分目录落盘，但角色多出档案与
项目角色组两层语义。

## 用户可见行为

- **素材库页签（短视频页 `/office/videos?tab=materials`）**：素材库与人物库已并入
  短视频页的页签，旧路径 `/office/materials` 重定向过来；图片/视频筛选 + 名称与分组关键字本地即时搜；
  打了标的按标签聚组、没打的按天归组（今天/昨天/日期）。列表图一律走后端生成的
  320px WebP 缩略图，缺缩略图时回退原图。
- **引用可见性与未使用清理**：每条素材带镜头引用计数（`ref_count`），被引用的卡片
  显示「N 镜引用」角标，0 引用在 meta 区标「未使用」；工具栏「只看未使用」一键筛出
  没有任何镜头引用的素材，撞 200 条上限时先清这批。
- **预览灯箱**：点缩略图打开大图/视频预览，支持在当前筛选结果里上一张/下一张（边界
  禁用而不是循环）、显示「第 N / 共 M 条」与引用数，并提供直接下载。
- **上传**：按钮多选上传，每条一行实时进度；按文件头魔数判型（PNG/JPEG/GIF/WebP/BMP、
  MP4/WebM），不认识的内容在写盘前拒绝，图片闸门 10MB、视频 50MB，账号上限 200 条。
- **整理**：单条改名、单条/批量/整组打分组标签，标签同时是检索键；勾选跨分组保留，
  批量动作按 id 走。
- **删除走回收期**：删除先只写 `deleted_at`，列表与计数立刻看不见它，但行、文件与
  镜头引用都原样留着。默认窗口 600 秒（`MATERIAL_TRASH_TTL_SECONDS`），过期后由
  懒清理真删——删行、删文件（含缩略图）、解开镜头引用。
- **回收站入口**：工具栏「回收站」按钮带角标（列表响应里的 `trash_count`，为 0 时
  角标隐藏）。抽屉里只列窗口内的素材，按删除时刻倒序，每行显示距真删的倒计时
  （服务端按请求时刻算 `expires_in_seconds`，前端只做减法），支持单条恢复与
  「全部恢复」。素材库名额已满时，恢复先放回更早删除的那批，其余继续留在回收期。
- **删除前影响面**：删除确认弹窗先拉 `/usage`，按项目聚合出「《片名》第 N 镜」；
  回收期内引用保持不变，过期清理解绑。删除成功的提示上直接挂「撤销」，
  与回收站走同一个 `batch-restore` 接口。
- **镜头侧选择器（MaterialPicker）**：工作台「参考素材」打开，只回 id 列表；
  弹窗内可直接上传新素材，选中有一镜 4 条的上限。**视频素材这一期只记录引用，
  不参与渲染**——参考视频要公网可达地址，进首帧请求的只有图片。

## 关键取舍

### 为什么删除先进回收期而不是直接删

素材有批量删除与分组整理的高频操作，误删一条被多镜引用的素材会直接破坏在产项目。
软删把「删除」降级成可撤销的状态翻转：所有读路径、计数与上传闸门只看
`deleted_at IS NULL`，删一条立刻腾出名额；真删（删文件、解引用）推迟到过期清理，
撤销就只剩把这一列清回去。懒清理搭在列表读取上，不引入后台任务，过期项最迟在
下次打开素材库/回收站时消失，清理失败只记日志，不挂读路径。

### 为什么回收站要做成持久入口而不只是 toast 撤销

撤销提示只存活几秒，错过之后即使还在 TTL 窗口内也没有任何恢复入口——回收期机制
对用户等于不存在。回收站把同一批软删行显式化：角标告诉用户「有东西可恢复」，
抽屉给出每条的剩余时间，恢复与 toast 撤销共用 `batch-restore`，没有第二条写路径。

### 为什么倒计时由服务端给剩余秒数

`expires_in_seconds` 在请求时刻按服务端时钟算好，前端只随本地心跳做减法，不自己
拼 `deleted_at + TTL`。浏览器时钟与服务器差几分钟时，前端自算的倒计时会凭空多出
或短掉一截，而真删时刻只由服务端的懒清理决定。

### 为什么引用在真删时才解，而不是进回收期就解

回收期内镜头引用原样保留，撤销才能恢复到「和删之前一模一样」。提前解绑的话，
撤销时要把 id 一条条补回镜头，还无法区分期间用户是否手动改过引用。残留引用的
降级口径与文件丢失一致：读取/渲染跳过，保存时归一化清掉。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `MaterialAsset` | 表 `material_asset`：kind/name/tag/mime/size/rel_path + `deleted_at` 软删列、owner 隔离 |
| `backend/app/services/materials/store.py` | 魔数判型落盘（`materials/<owner_id>/<随机 id>.<ext>`）、320px WebP 缩略图、路径守卫 |
| `backend/app/services/materials/service.py` | `MaterialService`：`list_assets`/`list_trash`/`trash_count`、上传闸门、打标、`remove_many`/`restore_many`、`purge_expired` 懒清理、`usage`/`ref_usage_map` 引用反查、`materials_for` 批量解析 |
| `backend/app/routers/materials.py` | `/office/materials` 一组接口；列表带每条 `ref_count`；`GET /trash` 拼缩略图并按服务端时钟算剩余秒数；错误翻译 |
| `backend/app/services/videos/service.py` | 镜头保存归一化 `material_ids`、渲染入队时 `materials_for` 解析、只把图片并入参考图预算 |
| `frontend/src/api/materials.ts` | `materialsApi`：list/upload/标签/删除/恢复/`trash` |
| `frontend/src/components/videos/MaterialsPanel.vue` | 素材库页签：分组网格、上传进度、批量条、删除影响面、回收站抽屉与倒计时 |
| `frontend/src/components/videos/MaterialPicker.vue` | 镜头级选择器：勾选上限、弹窗内直传、只回 id |

## 接口列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/office/materials?kind=&q=&limit=&offset=` | 正常素材一页；每条带 `ref_count` 镜头引用数；响应带 `bytes_total`、`max_per_owner`、`trash_ttl_seconds` 与 `trash_count` |
| POST | `/office/materials` | multipart 上传，魔数判型；非媒体或超限走 `code=-1` 业务拒绝 |
| PUT | `/office/materials/{id}/name` | 改名，空名拒绝 |
| PUT | `/office/materials/{id}/tag` | 改单条分组，空串摘标签 |
| POST | `/office/materials/batch-tag` | 一批改成同一分组 |
| POST | `/office/materials/usage` | 批量查引用影响面（项目 + 镜号） |
| POST | `/office/materials/batch-delete` | 进回收期，回执带 `undo_seconds` 与引用镜头数 |
| POST | `/office/materials/batch-restore` | 撤销/回收站恢复，超额时只放回预算内那批 |
| GET | `/office/materials/trash?kind=&q=&limit=&offset=` | 回收站一页：软删行按删除时刻倒序，每条带 `deleted_at` 与 `expires_in_seconds` |
| DELETE | `/office/materials/{id}` | 单条删除，同样进回收期 |

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MATERIAL_MAX_PER_OWNER` | 200 | 一个账号的正常素材条数上限（回收期内的不占名额） |
| `MATERIAL_MAX_IMAGE_BYTES` | 10 MB | 图片落盘前闸门 |
| `MATERIAL_MAX_VIDEO_BYTES` | 50 MB | 视频落盘前闸门 |
| `MATERIAL_TRASH_TTL_SECONDS` | 600 | 回收期时长，过期后下次列表/回收站读取触发真删 |

零 DDL：回收站复用既有 `deleted_at` 列，新接口不加表也不加列。

## 测试

- `backend/tests/test_materials.py`：落盘判型/缩略图、账号隔离、筛选分页、
  回收期删除/恢复/过期真删（含文件与引用）、配额与恢复预算；回收站部分覆盖
  `list_trash` 的排序、类别/关键字筛选、过期先清理、owner 隔离，以及
  `GET /trash` 的信封形状、剩余秒数与列表角标 `trash_count`；`ref_usage_map`
  按镜头聚合引用数（同一镜头引用多条各算一次、未引用报 0）。
