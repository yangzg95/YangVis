# 短视频 TTS 配音（方舟 /audio/speech + ffmpeg 时间轴混音）

成片合成时可选叠一条 TTS 配音轨：按每一镜的「台词」逐句语音合成，按镜头时间轴摆位、
混成一条 AAC 封进成片。视频片段本身始终静音——人声只有 TTS 一个来源，模型自带音频
（`generate_audio`）继续保持关闭，两套人声不会打架。

整体仍是这个项目一贯的分工：**AI 只生成素材，程序负责装配**。LLM 写台词、方舟 TTS
出语音、ffmpeg 负责所有拼接，没有任何一个模型端到端产成片。

```
分镜 dialogue（逐句）
   └─ TTS 客户端按 base_url 自动选协议（同步，统一产出 mp3 bytes）
         ├─ openspeech 套餐：X-Api-Key + X-Api-Resource-Id，JSON 行流 base64 分块
         └─ OpenAI 兼容：Bearer + /audio/speech，直接回音频二进制
         └─ 临时目录 line_00.mp3、line_01.mp3 …
片段（静音）─concat→ silent.mp4
各句 mp3 ─atempo（超限兜底）→adelay（按镜头起点定位）→amix→ voiceover.m4a
silent.mp4 + voiceover.m4a ─流拷贝封装→ final.mp4
```

## 两种 TTS 协议

`tts.py` 按 `model_config.base_url` 自动分流，配置页没有协议开关：

| | openspeech 套餐协议（Agent Plan 个人版语音模型） | OpenAI 兼容协议 |
| --- | --- | --- |
| 模型地址 | `https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional`（完整地址，系统直接 POST，不再拼路径） | 填到版本号层（如 `…/api/v3`），系统拼 `/audio/speech` |
| 鉴权 | 请求头 `X-Api-Key`（套餐专属 API Key）+ `X-Api-Resource-Id: seed-tts-2.0` | `Authorization: Bearer <key>` |
| 请求体 | `{"req_params": {"text", "speaker", "audio_params": {"format":"mp3","sample_rate":24000}}}`，**无 model 字段** | `{"model", "input", "voice", "response_format", "speed"}` |
| 响应 | 逐行 JSON 流：`code=0` 带 base64 音频块，`code=20000000` 结束，其余非零即业务错误 | 成功直接回音频二进制；JSON 响应一律按错误处理 |
| 音色字段 | `speaker`（如 `zh_female_vv_uranus_bigtts`） | `voice` |

套餐协议是火山官方文档（`agent-plan-personal-voice-model`）规定的用法：该端点**就是设计给
Agent Plan 专属 Key 走 HTTP 调用的**，费用从套餐 AFP 额度抵扣（可开超额后付费），不受设置页
「套餐 key 不能外调」那条限制——那条针对对话模型。`request_extra` 可覆盖 `resource_id`
与 `sample_rate`，默认 `seed-tts-2.0` / 24000。

## 用户可见行为

- **模型与集成**：新增第五种用途「语音」（`model_config.purpose = tts`），按模型地址
  自动识别 openspeech 套餐协议或 OpenAI 兼容协议（见上表）。连通性测试没有免费只读
  接口可用，改为真合成一句两字短音频（`测试`），密钥、资源、默认音色一次验全，
  提示语注明会产生极少量计费；成功消息里会标明实际走了哪种协议。
- **工作台 → 全片 → 渲染参数**：新增「TTS 配音」开关与音色 ID 输入框。音色留空时退到
  服务端环境变量 `TTS_DEFAULT_VOICE`；两者都空，发起合成时后端明确拒绝而不是猜一个音色。
  开关打开但语音模型未测通时，页面在开关下方直接出配置引导，合成按钮同步置灰。
- **合成成片**：入队时把台词、每镜起点/槽位、音色、模型信息快照进 `publish_job.request.tts`，
  之后改分镜不影响这次作业。无台词的镜头不进快照，时间轴上它就是一段静音。
- **台词太长怎么办**：合成后逐句探测时长，配音比镜头槽位长则按倍率 `atempo` 加速
  （先加速再定位，避免把定位静音一起缩短）；倍率超过 `TTS_MAX_ATEMPO`（默认 1.6）
  直接判作业失败，报错点出是第几镜、配音多长、槽位多长，让人删台词或加长镜头——
  不产出加速怪声，也不悄悄截断句子。
- 不开配音时链路与上线前完全一致：`request` 里不带 `tts` 键，产物无音轨，既有的直拼/
  重编码两档行为一个字没动。SRT/ASS 字幕导出不受影响，仍按「屏幕文字」走独立文件。

## 关键取舍

### 为什么逐镜合成，而不是把整条 narration 一次合成

分镜表上每镜有自己的 `dialogue` 与固定槽位（`start_s` / `duration_s`），逐句合成让
每句话天然归属一个镜头：定位用镜头起点，超时长用镜头槽位判定，哪句出问题报错能点到
镜号。整条 narration 一次合成拿不到逐句时间戳（方舟该接口不回 word boundary），
只能再做一次强制对齐，多一个模型依赖还多一层漂移源。代价是 N 次 HTTP 调用——单句
都是几秒返回的短请求，而一个项目最多 12 镜，发布作业本来就是分钟级的后台任务。

### 为什么台词快照在入队，模型配置却在运行时解析

`request.tts.lines` 与 `request.sources` 同一口径：合成进行中用户改台词不该改变这次
作业拼什么。模型配置相反，只快照 id/name 留痕，运行时重新解析用途 `tts` 的默认项并
要求 `last_test_ok`——配置被删或密钥失效时让作业明确失败，而不是拿历史 id 调一条
已经不可用的密钥，或静默退回无声片。

### 为什么配音放在视频拼接之后，而不是并进 concat

concat demuxer 拼的是同参数片段，音频塞进去会让「同编码直拼」这条秒级常态路径失效。
三段式（无声拼接 → 混音 → 封装）下视频流只编/拷一次，最后一步是纯 remux（`-c copy`），
失败时还能从中间片 `silent.mp4` / `voiceover.m4a` 分清是拼接还是混音的锅。

### amix 为什么显式 normalize=0

ffmpeg 的 `amix` 默认按输入数把音量等比调小（防削顶的老默认值），单人配音各句在时间轴
上互不重叠，这个默认只会把人声整体压闷。`normalize=0` 关掉它，再用 `apad + atrim`
把音轨垫到、也截到与视频完全等长，封装时的 `-shortest` 只是双保险。

## 实现结构

| 位置 | 职责 |
| --- | --- |
| `backend/app/services/media/tts.py` | 双协议 TTS 客户端：`_is_plan_endpoint` 按 base_url 分流；套餐分支处理 X-Api-Key/X-Api-Resource-Id 头、`req_params` 请求体与 JSON 行流（base64 分块拼回、终止码 20000000）；OpenAI 分支处理 Bearer 鉴权与二进制/JSON 双态响应；中文错误翻译与大小闸门 |
| `backend/app/services/publish/composer.py` | `VoiceoverLine/VoiceoverPlan`、`probe_duration`（无 ffprobe，解析 `ffmpeg -i` 的 stderr）、`build_voiceover`（atempo→adelay→amix→apad/atrim）、`mux_voiceover`（流拷贝封装）；无配音走原 `_concat_silent` |
| `backend/app/services/publish/runner.py` | `_synthesize_voiceover`：读快照、运行时解析 TTS 默认配置、逐句合成落临时目录、构造时间轴计划 |
| `backend/app/services/videos/service.py` | `_tts_snapshot`：入队门槛（配置存在/已测通/音色非空/至少一句台词）与 `request.tts` 快照 |
| `backend/app/routers/settings.py` | tts 用途的探测分支与 `/settings/models/status` 的 `tts` 就绪位（不参与短视频页面准入，只在工作台开关处提示） |
| `backend/app/models/schemas.py` | `ModelPurpose.TTS`、`VideoParams.tts_enabled/tts_voice`、`ReadinessStatus.tts` |
| `backend/app/config.py` | `TTS_HTTP_TIMEOUT` / `TTS_DEFAULT_VOICE` / `TTS_AUDIO_MAX_BYTES` / `TTS_MAX_ATEMPO` / `TTS_SAMPLE_RATE` |
| `frontend/src/views/Settings.vue` | 用途「语音」的筛选、表单、型号提示与 request_extra 透传入口 |
| `frontend/src/views/VideoWorkbench.vue` | 配音开关、音色输入、未就绪告警与合成按钮前置门槛 |
| `frontend/src/api/{settings,videos}.ts` | `ModelPurpose` / `ReadinessStatus.tts` / `VideoParams` 两个新字段 |

零 DDL：`model_config.purpose` 与 `video_project.params` 本来就是 VARCHAR/JSON，
新值不需要迁移。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TTS_DEFAULT_VOICE` | 空 | 项目未指定音色时的兜底音色 ID；与项目字段都空时入队被拒 |
| `TTS_HTTP_TIMEOUT` | 60 | 单句合成的 HTTP 超时（秒） |
| `TTS_AUDIO_MAX_BYTES` | 10 MB | 单句音频落盘前闸门，超出判失败（防把错误页当音频存盘） |
| `TTS_MAX_ATEMPO` | 1.6 | 台词超槽位时的最大加速倍率，超出即作业失败 |
| `TTS_SAMPLE_RATE` | 44100 | 配音轨统一重采样率，避免各句 mp3 采样率不一被 amix 拒绝 |

## 测试

- `backend/tests/test_media_tts.py`（MockTransport，不出网）：OpenAI 分支的请求体形状、
  `request_extra` 合并且显式参数优先、空文本/空音色/坏格式前置拒绝、各 HTTP 状态码的
  错误翻译、200 包 JSON 视为错误、空音频与超限闸门；openspeech 套餐分支的端点识别、
  X-Api-Key/X-Api-Resource-Id 请求头与 `req_params` 形状（无 model）、base64 分块拼接到
  终止码、业务错误码行、HTTP 401、无终止码断流的容忍、探测消息标明协议。
- `backend/tests/test_publish_compose.py` 新增：入队四道门（无配置/未测通/无音色/
  全空台词）、服务端默认音色兜底、快照只保留有台词的镜头且起点按时间轴累加、runner
  逐句合成并构造计划、配置入队后被删则作业失败、TTS 异常落作业行；合成器侧覆盖
  Duration 解析、放得下不加速、超限按倍率加速且滤镜顺序正确、超上限失败、三段式
  六次 ffmpeg 调用的形状与流拷贝封装。
- 既有无声链路的测试全部保留原断言（`voiceover is None`、回退档 `-an`），保证开关
  关闭时行为零变化。
