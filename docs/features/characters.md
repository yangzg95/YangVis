# 人物库（角色档案 + 形象图，接入短视频脚本与渲染）

人物库是账号级的角色管理：每个角色有名字、人设、衣着、外形特色和一张专属形象图。
短视频通过**项目角色组**（`video_project.character_ids`，上限 3 个）认领角色，在两个
环节复用：

1. **脚本生成**——角色的文字档案拼成提示词段落，喂给内置流程
   `video-script-generation` 的策划师与分镜师两步，创作卡、分镜和台词围绕固定角色展开；
2. **视频渲染**——角色形象图自动作为参考图注入首帧作业与「参考图生视频」作业，
   保住跨镜头的人脸与衣着一致性。

与素材库（`material_asset`）的分工：素材是无结构的图/视频文件，角色是**带档案的
人物**。两者都是账号级资产、按 `characters/<owner_id>/`、`materials/<owner_id>/`
分目录落盘，删项目都不碰它们；但角色多出文字档案与项目级角色组两层语义。

## 用户可见行为

- **人物库页签（短视频页 `/office/videos?tab=characters`）**：人物库与素材库已并入
  短视频页的页签，旧路径 `/office/characters` 重定向过来；卡片网格展示形象图与三段档案，卡片角标显示
  「N 项目在用」（0 不显示）；新建/编辑共用一个弹窗（图片 + 角色名必填，人设/衣着/
  特色可空，各 512 字）。形象图只收图片（PNG/JPEG/GIF/WebP/BMP），视频与识别不出的
  内容在写盘前拒绝。编辑只改文字时不重传图片；换图走独立接口，旧图 best-effort 删除。
- **AI 生成三视图形象**：建档/编辑弹窗的形象图区除上传外，还可以写一段角色描述
  （或点「填入档案描述」把人设/衣着/外形拼成初稿再改），由默认「图片」模型同步生成
  一张**正面/侧面/背面三视角合一**的设定图（character turnaround sheet，2K 档）。
  生成是真计费调用，只由按钮显式触发；产物以二进制直接回传、不落盘不写库，前端转成
  File 后与手工上传走同一条 multipart 建档/换图链路——可多次生成挑一张，取消或换图
  不在服务器留任何垃圾。准入门与渲染作业一致：图片模型不存在或未通过连通性测试时
  直接 400 并提示去设置页，不把凭证错误留给上游报错。
- **重名提示**：后端不做名称唯一约束（允许同名），保存时前端对当前库做一次同名检查，
  重名弹二次确认而不是硬拦——人物一多容易撞名，选角色时才需要提醒，不该阻断建档。
- **删除走回收期**：删除确认弹窗先拉引用影响面（哪些短视频项目仍选了它），确认后只写
  `deleted_at` 进回收期，行、形象图与项目引用都原样保留。默认窗口 600 秒
  （`CHARACTER_TRASH_TTL_SECONDS`），过期后懒清理真删行与形象图。工具栏「回收站」带
  角标，抽屉里按剩余秒数倒计时，支持单条与全部恢复；名额满时先恢复更早删除的。
  项目里残留的 id 不逐条改写：回收期内 `characters_for` 仍解析（撤销无损），过期真删后
  在项目保存与渲染时按「id 已不存在」自然跳过，工作台用「人物已删除」虚线 chip 占位。
- **新建短视频弹窗**：可多选出场角色（带头像与名字的下拉，最多 3 个），随项目一起
  创建，脚本生成的第一次运行就带角色档案。
- **工作台 → 全片 → 出场角色**：角色组 chip 支持移除与改选（`CharacterPicker`，
  带搜索）。选择器里带一套**内联简版建档**：名字 + 形象图必填，人设/衣着/特色折叠
  可选，建好就地勾选，不用跳去人物库页签再回来重选；完整档案的维护仍归人物库页签。
  提示文案写明两件事：下一次「重新生成脚本」才带新档案；形象图在渲染时自动注入。
- **渲染注入规则**：首帧作业的参考图 = 角色形象图（在前）+ 该镜图片素材，合计超过
  4 张时保角色、截素材（截断写日志）；片段作业只有「参考图生视频」档注入角色图，
  首帧档只喂本镜首帧产物（角色形象已烤进首帧，混挂参考图会撞方舟
  「首帧禁混参考素材」的 400），纯文生（t2v）不给任何图。

## 关键取舍

### 为什么是项目级角色组，不做每镜勾选

一条片子的角色通常贯穿全片，项目级选择覆盖了「脚本围绕谁写、每张脸长什么样」两个
核心诉求，交互只在创建弹窗与全片 tab 各放一处。每镜勾选需要在镜头表再加一层
关联与一套默认继承逻辑，而分镜师已经能从角色段落里知道哪镜该出现谁；后续真有
多角色分镜的精确控制需求，再在镜头行扩 `character_ids`，项目组作为默认值。

### 为什么 AI 生成走同步预览接口，而不是 media_job 作业

出图链路（`media_job` 图片作业）是为镜头渲染设计的：入队、轮询、重 roll、产物按项目
落盘，一条作业占一个并发位。但人物建档是一次轻量表单动作，用户在弹窗里的预期是
「等十几秒看到图、不满意就再来一次」，没有「离线跑完回来看」的场景。因此三视图生成
对齐 TTS 单句试听的形态：`POST /portrait-preview` 同步调用方舟文生图、下载后以二进制
直接回浏览器（`Cache-Control: no-store`），**不建作业、不写库、不落盘**；只有用户点
保存，那张图才随既有 multipart 建档/换图接口过魔数判定落进 `characters/<owner_id>/`。
提示词模板（三视角、同一人、纯色背景、无文字水印）放在后端
`services/characters/prompts.py` 集中维护，迭代措辞不需要发版前端。

### 为什么删除也走回收期而不是直接硬删

角色形象图会注入所有在产项目的渲染请求，误删一个被多项目引用的角色，影响面比删一条
素材更大。角色库因此与素材库对齐成同一套软删语义：删除只翻转 `deleted_at`，所有读路径、
计数与建档闸门只看 `IS NULL` 的行，删一个角色立刻腾名额；撤销就是把这一列清回去，
回收期内 `characters_for` 仍能解析它，项目角色组与渲染注入完全无损。真删（删行、删图）
推迟到过期懒清理，过期后残留引用的降级口径与素材文件丢失一致（读取/渲染跳过、保存
归一化清掉）。懒清理搭在列表/回收站读取上，不引入后台任务，清理失败只记日志。

### 为什么允许重名，只在前端提示

名称是展示标签而不是引用键——项目角色组存的是数字 id，同名不会造成引用歧义。加唯一
约束反而会挡住「同一角色的不同造型」这类合理场景，所以后端不拦，由前端在保存时给一次
同名确认，把「选角色时可能分不清」的风险显式化，决定权留给用户。

### 参考图预算为什么是 4 张、角色为什么在前

方舟的 `reference_image` 到 4 张左右开始互相干扰，这与镜头素材 `material_ids` 的
上限同口径，合并后共用一份预算（`MAX_REFERENCE_IMAGES = 4`）。人脸/穿搭一致性是
选角色的首要目的，场景素材可以让步，所以截断时角色图优先保留。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `VideoCharacter` | 表 `video_character`：name/persona/outfit/features + 形象图 rel_path/mime/size；`VideoProject.character_ids` JSON 列存项目角色组 |
| `backend/app/services/characters/store.py` | 形象图落盘（`characters/<owner_id>/<随机 id>.<ext>`，只收图片、魔数判型、320px WebP 缩略图） |
| `backend/app/services/characters/prompts.py` | 三视图提示词模板 `build_turnaround_prompt()`：正面/侧面/背面、同一人三视角、纯色背景与无文字水印约束，角色描述原样拼接 |
| `backend/app/services/characters/service.py` | `CharacterService`：owner 收敛的列表/搜索/CRUD、配额闸门（只算正常行）、`characters_for` 批量解析（含回收期内行）、`usage`/`project_usage_map` 引用反查、`remove` 软删、`restore_many`/`purge_expired`/`list_trash`/`trash_count` 回收站 |
| `backend/app/services/media/store.py` | 路径守卫 `_ROOTS` 增加 `characters` 根；`resolve_url`/`delete_blob`（连带删缩略图）对新树直接生效 |
| `backend/app/routers/characters.py` | `/office/characters` 一组接口；multipart 建档/换图、列表带项目引用数与回收站角标、`GET /trash` 与 `POST /batch-restore`；`POST /portrait-preview` 同步 AI 三视图（默认图片模型 + `ark.generate_image`/`download`，二进制 no-store 回传，`sniff_media` 二次验型） |
| `backend/app/services/videos/service.py` | `_normalize_character_ids`（写入侧校验）、`_characters_note`（档案段落）、`_character_portraits`（形象图路径）、`_merge_reference_images`（预算合并）；接入 create/update、`_start_script_run`、`render_frames`、`render_clips`、`to_detail` |
| `backend/app/services/workflows/builtin.py` | `video-script-generation` 新增可选输入 `characters_note`，两步 prompt 都插 `{{characters_note}}`；启动 seed 自动刷新 |
| `backend/app/bootstrap.py` | 旧库幂等补列 `video_project.character_ids JSON NULL` 与 `video_character.deleted_at DATETIME NULL`；新表随 `create_all` 建 |
| `frontend/src/api/characters.ts` | `charactersApi` 与 `MAX_CHARACTERS_PER_PROJECT`，含 `trash`/`restore`/`portraitPreview`（blob 响应，失败体用 `blobErrorMessage` 拆） |
| `frontend/src/components/videos/CharactersPanel.vue` | 人物库页签：卡片网格与项目引用角标、建档/编辑弹窗（重名二次确认、AI 三视图生成区）、回收站抽屉与倒计时 |
| `frontend/src/utils/errors.ts` | `blobErrorMessage()`：blob 错误响应里的 JSON 信封拆信，TTS 试听与 AI 生图共用 |
| `frontend/src/components/videos/CharacterPicker.vue` | 项目角色组选择器（多选 3 个、搜索、内联简版建档并自动勾选） |
| `frontend/src/views/Videos.vue`（壳）、`components/videos/VideoProjectsPanel.vue`、`VideoWorkbench.vue` | 三页签合并壳与「片子」页签的创建弹窗、全片 tab 的角色组选择；payload/detail 增加 `character_ids` |

## 接口列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/office/characters?q=&limit=&offset=` | 角色列表，`q` 只搜名字；每条带 `project_count`，响应带回收站 `trash_count`/`trash_ttl_seconds` |
| POST | `/office/characters` | multipart 建档：`file` + `name`（必填）+ persona/outfit/features；非图片或空名走 `code=-1` 业务拒绝 |
| POST | `/office/characters/portrait-preview` | AI 三视图：JSON `{prompt}`（1-1000 字），同步返回图片二进制（`no-store`）；无图片模型/未测通/空白/上游错误/非图片产物均 400 |
| PUT | `/office/characters/{id}` | 改文字档案，全可选；name 给了不能是空白，他人/不存在为 404 |
| PUT | `/office/characters/{id}/portrait` | multipart 换形象图，旧图 best-effort 删 |
| POST | `/office/characters/usage` | 批量查引用：每个角色被哪些项目选进角色组 |
| DELETE | `/office/characters/{id}` | 移进回收期，回执带 `references`（仍引用它的项目数，仅提示） |
| GET | `/office/characters/trash` | 回收站一屏：软删行按删除时刻倒序，每条带 `deleted_at` 与 `expires_in_seconds` |
| POST | `/office/characters/batch-restore` | 撤销删除，超额时只放回预算内那批 |

短视频侧没有新增接口：角色组随 `POST/PUT /office/videos` 的 `character_ids` 落库，
详情接口 `GET /office/videos/{id}` 原样带回（含已删除角色的残留 id，由前端占位）。

## 升级行为

- 新库：`Base.metadata.create_all` 直接建 `video_character` 表与
  `video_project.character_ids` 列。
- 旧库：`migrate_schema` 幂等补 `ALTER TABLE video_project ADD COLUMN
  character_ids JSON NULL` 与 `ALTER TABLE video_character ADD COLUMN deleted_at
  DATETIME NULL`；存量行读到 NULL，服务层一律按正常角色/空角色组处理，行为与
  上线前完全一致。
- 内置工作流的输入声明与 prompt 模板随启动 seed 就地刷新：`characters_note` 是
  可选输入，没选角色时恒为空串，模板整段不渲染，历史项目跑出来的脚本与之前等价。
- 配置项：`CHARACTER_MAX_PER_OWNER`（默认 50）、`CHARACTER_MAX_PER_PROJECT`
  （默认 3）、`CHARACTER_TRASH_TTL_SECONDS`（默认 600）；形象图大小闸门复用
  `MATERIAL_MAX_IMAGE_BYTES`（10MB）。
- AI 三视图是纯增量接口（`POST /office/characters/portrait-preview`）：无 DDL、
  无新配置项，复用用途为 `image` 的默认模型与 `MEDIA_IMAGE_MAX_BYTES` 下载闸门；
  没配图片模型的账号只在点生成时收到 400 提示，建档/上传等既有行为完全不变。

## 测试

- `backend/tests/test_characters.py`（store/service/HTTP 三层）：
  - 落盘：账号树路径形状、路径守卫放行、视频/非媒体/超限/空文件拒绝、缩略图生成；
  - 服务：owner 隔离、CRUD 与搜索、空名拒绝、配额闸门、空串清空 vs None 保留、
    换图删旧文件、`characters_for` 丢未知/他人 id、软删进回收站（行/文件/引用保留）、
    恢复、过期真删行与文件、恢复受配额限制、usage；
  - HTTP：multipart 信封、非图片/空名业务拒绝、改档/换图/软删/404/usage、
    `GET /trash` 的剩余秒数与 `batch-restore` 整条恢复；
  - AI 三视图：提示词模板含三视角关键词与描述、预览接口回图片二进制且 `no-store`、
    无模型/未测通/空白描述三道前置门、`ArkError` 翻译成 400 人话、非图片产物被
    `sniff_media` 拦下（ark 的 `generate_image`/`download` 全程 monkeypatch，不打真实上游）。
- `backend/tests/test_videos.py` 的角色集成用例：角色组写入归一（未知/他人 id
  丢弃、上限 3、空列表清空）、`_characters_note` 档案段落、首帧注入角色图且角色
  在前、合计超 4 张的截断、参考图档注入而首帧档不混图；软删角色在回收期内仍注入、
  过期真删后渲染自动跳过。
  同文件的 `media_dir` 夹具补了 `video_service.settings.upload_media_path` 的
  property 重定向（链式衔接与成片合成的产物存在性检查需要它）。
