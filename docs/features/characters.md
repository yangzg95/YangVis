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

- **人物库页（/office/characters）**：卡片网格展示形象图与三段档案；新建/编辑共用
  一个弹窗（图片 + 角色名必填，人设/衣着/特色可空，各 512 字）。形象图只收图片
  （PNG/JPEG/GIF/WebP/BMP），视频与识别不出的内容在写盘前拒绝。编辑只改文字时
  不重传图片；换图走独立接口，旧图 best-effort 删除。
- **删除是硬删除**：删除确认弹窗先拉引用影响面（哪些短视频项目仍选了它）。确认后
  删行 + 删形象图，不可撤销；项目里残留的 id 不逐条改写，而在项目保存与渲染时按
  「id 已不存在」自然跳过，工作台用「人物已删除」的虚线 chip 占位，下次保存清掉。
- **新建短视频弹窗**：可多选出场角色（带头像与名字的下拉，最多 3 个），随项目一起
  创建，脚本生成的第一次运行就带角色档案。
- **工作台 → 全片 → 出场角色**：角色组 chip 支持移除与改选（`CharacterPicker`，
  带搜索，可跳人物库建档）。提示文案写明两件事：下一次「重新生成脚本」才带新档案；
  形象图在渲染时自动注入。
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

### 为什么形象图只上传、不做 AI 生成

出图链路（`media_job` 图片作业）已经存在，但角色建档是一次轻量的表单动作，AI 生成
要引入作业轮询、重 roll、满意后落库三段交互，与「先把角色资料管起来」的目标不匹配。
需要生成时可以在短视频里渲一张满意的首帧，再走既有的「存为素材」路线，或后续在
人物库弹窗里加「用图片模型生成」入口。

### 为什么硬删除而不是素材库那套回收期

素材有批量删除/分组管理的高频操作，回收期防误删；角色条目少（默认账号上限 50）、
删除是低频且 deliberate 的动作，删除前还有引用影响面确认。残留引用的降级口径与
素材文件丢失完全一致（读取/渲染跳过、保存归一化清掉），不需要额外的软删状态机。

### 参考图预算为什么是 4 张、角色为什么在前

方舟的 `reference_image` 到 4 张左右开始互相干扰，这与镜头素材 `material_ids` 的
上限同口径，合并后共用一份预算（`MAX_REFERENCE_IMAGES = 4`）。人脸/穿搭一致性是
选角色的首要目的，场景素材可以让步，所以截断时角色图优先保留。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/models/entities.py` `VideoCharacter` | 表 `video_character`：name/persona/outfit/features + 形象图 rel_path/mime/size；`VideoProject.character_ids` JSON 列存项目角色组 |
| `backend/app/services/characters/store.py` | 形象图落盘（`characters/<owner_id>/<随机 id>.<ext>`，只收图片、魔数判型、320px WebP 缩略图） |
| `backend/app/services/characters/service.py` | `CharacterService`：owner 收敛的列表/搜索/CRUD、配额闸门、`characters_for` 批量解析、`usage`/`remove` 的引用反查 |
| `backend/app/services/media/store.py` | 路径守卫 `_ROOTS` 增加 `characters` 根；`resolve_url`/`delete_blob`（连带删缩略图）对新树直接生效 |
| `backend/app/routers/characters.py` | `/office/characters` 一组接口；multipart 建档/换图，错误翻译与素材路由同口径 |
| `backend/app/services/videos/service.py` | `_normalize_character_ids`（写入侧校验）、`_characters_note`（档案段落）、`_character_portraits`（形象图路径）、`_merge_reference_images`（预算合并）；接入 create/update、`_start_script_run`、`render_frames`、`render_clips`、`to_detail` |
| `backend/app/services/workflows/builtin.py` | `video-script-generation` 新增可选输入 `characters_note`，两步 prompt 都插 `{{characters_note}}`；启动 seed 自动刷新 |
| `backend/app/bootstrap.py` | 旧库幂等补列 `video_project.character_ids JSON NULL`；新表随 `create_all` 建 |
| `frontend/src/api/characters.ts` | `charactersApi` 与 `MAX_CHARACTERS_PER_PROJECT` |
| `frontend/src/views/Characters.vue` | 人物库页：卡片网格、建档/编辑弹窗、引用影响面删除确认 |
| `frontend/src/components/videos/CharacterPicker.vue` | 项目角色组选择器（多选 3 个、搜索、跳人物库建档） |
| `frontend/src/views/Videos.vue`、`VideoWorkbench.vue` | 创建弹窗与全片 tab 的角色组选择；payload/detail 增加 `character_ids` |

## 接口列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/office/characters?q=&limit=&offset=` | 角色列表，`q` 只搜名字；回 total 与账号条数上限 |
| POST | `/office/characters` | multipart 建档：`file` + `name`（必填）+ persona/outfit/features；非图片或空名走 `code=-1` 业务拒绝 |
| PUT | `/office/characters/{id}` | 改文字档案，全可选；name 给了不能是空白，他人/不存在为 404 |
| PUT | `/office/characters/{id}/portrait` | multipart 换形象图，旧图 best-effort 删 |
| POST | `/office/characters/usage` | 批量查引用：每个角色被哪些项目选进角色组 |
| DELETE | `/office/characters/{id}` | 硬删除，回执带 `references`（仍引用它的项目数，仅提示） |

短视频侧没有新增接口：角色组随 `POST/PUT /office/videos` 的 `character_ids` 落库，
详情接口 `GET /office/videos/{id}` 原样带回（含已删除角色的残留 id，由前端占位）。

## 升级行为

- 新库：`Base.metadata.create_all` 直接建 `video_character` 表与
  `video_project.character_ids` 列。
- 旧库：`migrate_schema` 幂等补 `ALTER TABLE video_project ADD COLUMN
  character_ids JSON NULL`；存量行读到 NULL，服务层一律按空角色组处理，行为与
  上线前完全一致。
- 内置工作流的输入声明与 prompt 模板随启动 seed 就地刷新：`characters_note` 是
  可选输入，没选角色时恒为空串，模板整段不渲染，历史项目跑出来的脚本与之前等价。
- 配置项：`CHARACTER_MAX_PER_OWNER`（默认 50）、`CHARACTER_MAX_PER_PROJECT`
  （默认 3）；形象图大小闸门复用 `MATERIAL_MAX_IMAGE_BYTES`（10MB）。

## 测试

- `backend/tests/test_characters.py`（18 个，store/service/HTTP 三层）：
  - 落盘：账号树路径形状、路径守卫放行、视频/非媒体/超限/空文件拒绝、缩略图生成；
  - 服务：owner 隔离、CRUD 与搜索、空名拒绝、配额闸门、空串清空 vs None 保留、
    换图删旧文件、`characters_for` 丢未知/他人 id、硬删除的引用计数与文件清理、usage；
  - HTTP：multipart 信封、非图片/空名业务拒绝、改档/换图/删除/404/usage。
- `backend/tests/test_videos.py` 新增角色集成用例：角色组写入归一（未知/他人 id
  丢弃、上限 3、空列表清空）、`_characters_note` 档案段落、首帧注入角色图且角色
  在前、合计超 4 张的截断、参考图档注入而首帧档不混图、删角色后渲染自动跳过。
  同文件的 `media_dir` 夹具补了 `video_service.settings.upload_media_path` 的
  property 重定向（链式衔接与成片合成的产物存在性检查需要它）。
