<template>
  <div class="workbench">
    <header class="wb-head">
      <div class="wb-head-main">
        <a-input
          v-model:value="title"
          class="wb-title-input"
          :maxlength="128"
          :bordered="false"
          @change="dirtyProject = true"
        />
        <a-tag :color="statusColor(state)">{{ statusText(state) }}</a-tag>
        <span class="muted wb-head-meta">
          {{ platformText(platform) }} · {{ shots.length }} 镜 · {{ totalDuration }} 秒
        </span>
        <span v-if="dirty" class="dirty-hint">未保存</span>
      </div>
      <a-space :size="8" wrap>
        <a-button size="small" @click="goToMaterials">
          <PictureOutlined /> 素材库
        </a-button>
        <a-button size="small" :loading="generating" @click="onRegenerate">
          <ReloadOutlined /> 重新生成脚本
        </a-button>
        <a-tooltip :title="renderBlockHint">
          <span>
            <a-button
              size="small"
              type="primary"
              :disabled="Boolean(renderBlockHint)"
              @click="openRender"
            >
              <VideoCameraOutlined /> 渲染首帧
            </a-button>
          </span>
        </a-tooltip>
        <a-tooltip :title="clipBlockHint">
          <span>
            <a-button
              size="small"
              type="primary"
              :disabled="Boolean(clipBlockHint)"
              @click="openClipRender"
            >
              <PlayCircleOutlined /> 渲染片段
            </a-button>
          </span>
        </a-tooltip>
        <a-dropdown trigger="click">
          <a-button size="small" :disabled="!shots.length">
            <DownloadOutlined /> 导出字幕
          </a-button>
          <template #overlay>
            <a-menu @click="onExportSubtitle">
              <a-menu-item key="srt">SRT（剪映 / PR 通用）</a-menu-item>
              <a-menu-item key="ass">ASS（自带竖屏样式）</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
        <a-button size="small" :loading="saving" type="primary" @click="onSave">
          <SaveOutlined /> 保存
        </a-button>
      </a-space>
    </header>

    <a-alert v-if="loadError" type="error" show-icon class="wb-alert" :message="loadError" />
    <a-alert
      v-if="!loadError && !imageReady"
      type="warning"
      show-icon
      class="wb-alert"
      message="渲染首帧要先配好用途为「图片」的模型"
      description="分镜可以先改；入队渲染时后端会当场拒绝没有默认图片模型的提交，免得留下一批停在「排队中」的作业。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>
    <a-alert
      v-if="!loadError && !videoReady"
      type="warning"
      show-icon
      class="wb-alert"
      message="渲染片段要先配好用途为「视频」的模型"
      description="首帧与分镜不受影响；片段入队时后端会当场拒绝没有默认视频模型的提交。出片按秒计费，配置要先通过连通性测试。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <main class="wb-body">
      <!-- 分镜列表：顺序即成片顺序，时间轴由这个顺序累加出来 -->
      <aside class="wb-shots">
        <div class="pane-title">
          分镜 {{ shots.length }}
          <a-button
            size="small"
            type="text"
            :disabled="shots.length >= MAX_SHOTS"
            :title="`一个项目最多 ${MAX_SHOTS} 镜`"
            @click="addShot"
          >
            <PlusOutlined /> 加一镜
          </a-button>
        </div>
        <ul v-if="shots.length" class="shot-list">
          <li
            v-for="(shot, index) in shots"
            :key="shot.id || `new-${index}`"
            :class="['shot-item', { active: index === selected }]"
            @click="selected = index"
          >
            <span class="shot-no">{{ index + 1 }}</span>
            <span class="shot-text">
              <span class="shot-name">{{ shot.scene || shot.image_prompt || '（还没写画面）' }}</span>
              <span class="shot-sub">{{ shot.duration_s }} 秒 · {{ shotJobsText(shot) }}</span>
            </span>
            <span class="shot-ops">
              <a-button
                size="small"
                type="text"
                :disabled="index === 0"
                title="上移"
                @click.stop="moveShot(index, -1)"
              >
                <ArrowUpOutlined />
              </a-button>
              <a-button
                size="small"
                type="text"
                :disabled="index === shots.length - 1"
                title="下移"
                @click.stop="moveShot(index, 1)"
              >
                <ArrowDownOutlined />
              </a-button>
              <a-popconfirm
                title="删除这一镜？已渲出的首帧与片段文件会留在服务器上，删掉整个项目时才一并清理。"
                ok-text="删除"
                cancel-text="取消"
                @confirm="removeShot(index)"
              >
                <a-button size="small" type="text" danger title="删除" @click.stop>
                  <DeleteOutlined />
                </a-button>
              </a-popconfirm>
            </span>
          </li>
        </ul>
        <a-empty
          v-else
          :image="simpleEmpty"
          :description="generating ? '正在生成分镜…' : '还没有分镜，点上方「重新生成脚本」'"
        />
      </aside>

      <section class="wb-editor">
        <a-tabs v-model:activeKey="editorTab" size="small" class="wb-tabs">
          <a-tab-pane key="shot" tab="这一镜">
            <a-form v-if="current" layout="vertical" class="pane-form">
              <div class="form-section">
                <a-row :gutter="12">
                  <a-col :span="12">
                    <a-form-item label="本镜时长">
                      <a-input-number
                        v-model:value="current.duration_s"
                        :min="SHOT_MIN_S"
                        :max="SHOT_MAX_S"
                        :step="1"
                        style="width: 100%"
                        @change="markShotDirty"
                      />
                      <div class="hint">
                        {{ SHOT_MIN_S }}-{{ SHOT_MAX_S }} 秒（按最低一档模型的渲染窗口定的）；
                        超出会被服务端夹回这个区间。
                      </div>
                    </a-form-item>
                  </a-col>
                  <a-col :span="12">
                    <a-form-item label="生成方式">
                      <a-select
                        v-model:value="current.gen_mode"
                        :options="GEN_MODE_OPTIONS"
                        style="width: 100%"
                        @change="markShotDirty"
                      />
                      <div class="hint">{{ genModeHint(current.gen_mode) }}</div>
                    </a-form-item>
                  </a-col>
                </a-row>
              </div>

              <div class="form-section">
                <div class="form-section-title">画面</div>
                <a-form-item label="场景">
                  <a-input
                    v-model:value="current.scene"
                    :maxlength="512"
                    placeholder="在哪、有谁"
                    @change="markShotDirty"
                  />
                </a-form-item>
                <a-form-item label="首帧提示词">
                  <a-textarea
                    v-model:value="current.image_prompt"
                    :rows="4"
                    :maxlength="PROMPT_MAX"
                    show-count
                    placeholder="这一镜第一帧画面长什么样：主体、动作、构图、光线"
                    @change="markShotDirty"
                  />
                  <div class="hint">
                    {{ PROMPT_MAX }} 字以内。渲染首帧用的就是这一段；空着这一镜无法入队。
                  </div>
                </a-form-item>
                <a-form-item label="运镜">
                  <a-input
                    v-model:value="current.camera"
                    :maxlength="256"
                    placeholder="例如：缓慢推近 / 手持跟拍"
                    @change="markShotDirty"
                  />
                </a-form-item>
                <a-form-item label="主体动作">
                  <a-input
                    v-model:value="current.action"
                    :maxlength="256"
                    placeholder="主体在这一镜里做什么"
                    @change="markShotDirty"
                  />
                </a-form-item>
                <a-form-item label="运动提示词">
                  <a-textarea
                    v-model:value="current.video_prompt"
                    :rows="2"
                    :maxlength="PROMPT_MAX"
                    placeholder="这一段视频里怎么动起来：主体动作、镜头运动、节奏"
                    @change="markShotDirty"
                  />
                  <div class="hint">渲染片段用的就是这一段；空着这一镜无法入队。</div>
                </a-form-item>
                <a-form-item label="参考素材">
                  <div v-if="currentMaterials.length" class="material-chips">
                    <span
                      v-for="item in currentMaterials"
                      :key="item.id"
                      class="material-chip"
                      :class="{ 'is-missing': item.missing }"
                      :title="item.missing ? '素材库里已经没有这条' : item.name"
                    >
                      <img
                        v-if="item.kind === 'image' && item.url"
                        :src="item.url"
                        :alt="item.name"
                        loading="lazy"
                      />
                      <video v-else-if="item.url" :src="item.url" preload="metadata" muted />
                      <span class="chip-name">{{ item.name }}</span>
                      <a-button
                        size="small"
                        type="text"
                        class="chip-close"
                        @click="dropMaterial(item.id)"
                      >
                        <CloseOutlined />
                      </a-button>
                    </span>
                  </div>
                  <a-button size="small" class="pick-material" @click="pickerOpen = true">
                    <PictureOutlined /> {{ currentMaterials.length ? '改选' : '从素材库选择' }}
                  </a-button>
                  <div class="hint">
                    人物与场景的一致性靠它：最多 {{ MAX_REFS_PER_SHOT }} 条，来自账号级的素材库。
                    这一期只有图片类素材真的进首帧请求，视频类记下引用还不参与渲染。
                  </div>
                </a-form-item>
              </div>

              <div class="form-section">
                <div class="form-section-title">文字</div>
                <a-form-item label="屏幕文字（字幕）">
                  <a-input
                    v-model:value="current.on_screen_text"
                    :maxlength="256"
                    placeholder="这一镜屏幕上打出来的那句话"
                    @change="markShotDirty"
                  />
                  <div class="hint">
                    不烧进画面：预览按它叠加，导出 SRT / ASS 也按它生成。
                  </div>
                </a-form-item>
                <a-form-item label="台词">
                  <a-textarea
                    v-model:value="current.dialogue"
                    :rows="2"
                    :maxlength="512"
                    placeholder="这一镜说的话（本期不配音，留给后续或剪辑）"
                    @change="markShotDirty"
                  />
                </a-form-item>
              </div>
            </a-form>
            <a-empty v-else :image="simpleEmpty" description="左边还没有镜头" />
          </a-tab-pane>

          <a-tab-pane key="film" tab="全片">
            <a-form layout="vertical" class="pane-form">
              <div class="form-section">
                <div class="form-section-title">输入</div>
                <a-form-item label="想法">
                  <a-textarea
                    v-model:value="idea"
                    :rows="2"
                    :maxlength="4000"
                    @change="dirtyProject = true"
                  />
                </a-form-item>
                <a-form-item label="发布平台">
                  <a-radio-group
                    v-model:value="platform"
                    size="small"
                    button-style="solid"
                    @change="dirtyProject = true"
                  >
                    <a-radio-button value="douyin">抖音</a-radio-button>
                    <a-radio-button value="kuaishou">快手</a-radio-button>
                    <a-radio-button value="other">通用</a-radio-button>
                  </a-radio-group>
                </a-form-item>
                <a-form-item label="补充要求">
                  <a-input
                    v-model:value="requirement"
                    :maxlength="512"
                    placeholder="重新生成脚本时喂给策划师的那句话"
                    @change="dirtyProject = true"
                  />
                  <div class="hint">
                    这里是改，「重新生成脚本」按钮用的是这一刻的值：每次整包重跑，不留「上次要求还挂着」。
                  </div>
                </a-form-item>
              </div>

              <div class="form-section">
                <div class="form-section-title">创作卡</div>
                <a-row :gutter="12">
                  <a-col :span="12">
                    <a-form-item label="开头钩子">
                      <a-input
                        v-model:value="brief.hook"
                        :maxlength="128"
                        @change="dirtyProject = true"
                      />
                    </a-form-item>
                  </a-col>
                  <a-col :span="12">
                    <a-form-item label="钩子类型">
                      <a-input
                        v-model:value="brief.hook_type"
                        :maxlength="16"
                        placeholder="悬念 / 反差 / 数字…"
                        @change="dirtyProject = true"
                      />
                    </a-form-item>
                  </a-col>
                </a-row>
                <a-row :gutter="12">
                  <a-col :span="12">
                    <a-form-item label="给谁看">
                      <a-input
                        v-model:value="brief.audience"
                        :maxlength="256"
                        @change="dirtyProject = true"
                      />
                    </a-form-item>
                  </a-col>
                  <a-col :span="12">
                    <a-form-item label="要换来什么">
                      <a-input
                        v-model:value="brief.goal"
                        :maxlength="256"
                        placeholder="涨粉 / 进店 / 报名…"
                        @change="dirtyProject = true"
                      />
                    </a-form-item>
                  </a-col>
                </a-row>
                <a-form-item label="成片时长">
                  <a-input-number
                    v-model:value="brief.duration_s"
                    :min="5"
                    :max="60"
                    :step="5"
                    style="width: 100%"
                    @change="dirtyProject = true"
                  />
                  <div class="hint">秒。重新生成脚本时，策划师按这一档分配每一镜的时长。</div>
                </a-form-item>
                <a-form-item label="讲几段">
                  <div
                    v-for="(item, index) in brief.structure"
                    :key="index"
                    class="structure-row"
                  >
                    <a-input
                      :value="item"
                      :maxlength="64"
                      @update:value="setSegment(index, String($event))"
                    />
                    <a-button size="small" type="text" danger @click="removeSegment(index)">
                      <MinusOutlined />
                    </a-button>
                  </div>
                  <a-button
                    size="small"
                    :disabled="brief.structure.length >= MAX_SEGMENTS"
                    @click="addSegment"
                  >
                    <PlusOutlined /> 加一段
                  </a-button>
                  <div class="hint">
                    顺序就是片子的推进顺序，最多 {{ MAX_SEGMENTS }} 段。
                  </div>
                </a-form-item>
                <a-form-item label="风险提示">
                  <a-textarea
                    v-model:value="brief.risk_notes"
                    :rows="2"
                    :maxlength="512"
                    placeholder="别写公司名 / 数据别引原文…"
                    @change="dirtyProject = true"
                  />
                </a-form-item>
              </div>

              <div class="form-section">
                <div class="form-section-title">文案与风格</div>
                <a-form-item label="口播文案">
                  <a-textarea
                    v-model:value="script"
                    :rows="6"
                    :maxlength="20000"
                    @change="dirtyProject = true"
                  />
                  <div class="hint">
                    这一期不做口播：这段是内容底稿，字幕用每一镜的「屏幕文字」。
                  </div>
                </a-form-item>
                <a-form-item label="画面风格">
                  <a-input
                    v-model:value="style.visual_style"
                    :maxlength="256"
                    placeholder="例如：纪实手持、暖色低饱和"
                    @change="dirtyProject = true"
                  />
                </a-form-item>
                <a-form-item label="人物设定">
                  <a-input
                    v-model:value="style.persona"
                    :maxlength="128"
                    placeholder="出镜的人是谁、什么状态"
                    @change="dirtyProject = true"
                  />
                </a-form-item>
                <a-form-item label="色调">
                  <a-input
                    v-model:value="style.palette"
                    :maxlength="128"
                    @change="dirtyProject = true"
                  />
                </a-form-item>
                <div class="hint style-hint">
                  这三项是给策划师与分镜师的约束，改动要在下一次「重新生成脚本」才落到分镜上；
                  已经生成的镜头不会自动跟着变。
                </div>
              </div>

              <div class="form-section">
                <div class="form-section-title">渲染参数</div>
                <a-row :gutter="12">
                  <a-col :span="12">
                    <a-form-item label="画幅">
                      <a-select
                        v-model:value="params.ratio"
                        :options="RATIO_OPTIONS"
                        style="width: 100%"
                        @change="dirtyProject = true"
                      />
                      <div class="hint">
                        片段按它出画幅。默认 9 : 16：「自适应」跟着首帧的比例走，
                        出成方图就整条跟着方，竖屏不靠这个。
                      </div>
                    </a-form-item>
                  </a-col>
                  <a-col :span="12">
                    <a-form-item label="首帧分辨率">
                      <a-select
                        v-model:value="params.image_size"
                        :options="IMAGE_SIZE_OPTIONS"
                        style="width: 100%"
                        @change="dirtyProject = true"
                      />
                      <div class="hint">首帧与封面按它出图，与片段分辨率是两档。</div>
                    </a-form-item>
                  </a-col>
                </a-row>
                <a-row :gutter="12">
                  <a-col :span="12">
                    <a-form-item label="视频分辨率">
                      <a-select
                        v-model:value="params.resolution"
                        :options="RESOLUTION_OPTIONS"
                        style="width: 100%"
                        @change="dirtyProject = true"
                      />
                      <div class="hint">
                        上限由型号决定：fast 与 mini 只到 720p，参考图场景不支持 1080p。
                      </div>
                    </a-form-item>
                  </a-col>
                  <a-col :span="12">
                    <a-form-item label="水印">
                      <a-switch v-model:checked="params.watermark" @change="dirtyProject = true" />
                      <div class="hint">默认关，首帧与片段都按它。</div>
                    </a-form-item>
                  </a-col>
                </a-row>
                <a-form-item label="生成音频">
                  <a-switch
                    v-model:checked="params.generate_audio"
                    @change="dirtyProject = true"
                  />
                  <div class="hint">
                    默认关：方舟那一侧默认开，留着就会拿到一条模型自己编的人声或背景乐，
                    而这一期定的是「不做口播、只做字幕」。
                  </div>
                </a-form-item>
              </div>

              <div class="form-section">
                <div class="form-section-title">发布</div>
                <a-form-item label="标题候选">
                  <a-select
                    v-model:value="publish.title_candidates"
                    mode="tags"
                    :token-separators="[',', '，']"
                    :maxlength="128"
                    placeholder="回车添加，最多 6 条"
                    style="width: 100%"
                    @change="dirtyProject = true"
                  />
                </a-form-item>
                <a-form-item label="话题标签">
                  <a-select
                    v-model:value="publish.tags"
                    mode="tags"
                    :token-separators="[',', '，']"
                    :maxlength="32"
                    placeholder="回车添加，最多 8 个"
                    style="width: 100%"
                    @change="dirtyProject = true"
                  />
                </a-form-item>
                <a-form-item label="封面描述">
                  <a-input
                    v-model:value="publish.cover_prompt"
                    :maxlength="512"
                    placeholder="竖屏封面要什么画面"
                    @change="dirtyProject = true"
                  />
                  <div class="hint">
                  <a-button size="small" :disabled="!imageReady" @click="onRenderCover">
                    <PictureOutlined /> 生成封面
                  </a-button>
                  <span class="cover-meta">封面按「最新一次成功的封面作业」显示，不占镜头位。</span>
                  <img v-if="coverUrl" class="cover-mini" :src="coverUrl" alt="封面" />
                </div>
                </a-form-item>
                <a-form-item label="成片合成">
                  <a-space wrap>
                    <a-button
                      type="primary"
                      :loading="composing"
                      :disabled="Boolean(composeProblem)"
                      :title="composeProblem || '把各镜片段按镜头顺序拼成一个 MP4'"
                      @click="onCompose"
                    >
                      <VideoCameraOutlined /> 合成成片
                    </a-button>
                    <span v-if="composeProblem" class="hint">{{ composeProblem }}</span>
                  </a-space>
                  <div v-if="publishJob" class="job-meta publish-job">
                    <div class="job-kind">发布作业 #{{ publishJob.id }}</div>
                    <div class="job-row">
                      <span class="muted">状态</span>
                      <a-tag :color="publishStatusColor(publishJob.status)">
                        {{ publishStatusText(publishJob.status) }}
                      </a-tag>
                    </div>
                    <div class="job-row">
                      <span class="muted">用时</span>
                      <span>{{ publishJob.elapsed_ms ? fmtElapsed(publishJob.elapsed_ms) : '—' }}</span>
                    </div>
                    <div class="job-row">
                      <span class="muted">大小</span>
                      <span>{{ publishJob.file_size ? fmtSize(publishJob.file_size) : '—' }}</span>
                    </div>
                    <a-alert
                      v-if="publishJob.error_msg"
                      type="error"
                      show-icon
                      class="job-error"
                      :message="publishJob.error_msg"
                    />
                    <a-space
                      v-if="publishJob.status === 'succeeded'"
                      :size="8"
                      wrap
                      class="job-ops"
                    >
                      <a-button size="small" type="primary" @click="onDownloadFinal">
                        <DownloadOutlined /> 下载成片 MP4
                      </a-button>
                      <a-button v-if="coverUrl" size="small" @click="onDownloadCover">
                        <PictureOutlined /> 下载封面
                      </a-button>
                      <a-button size="small" @click="onCopyPublishText">
                        <CopyOutlined /> 复制标题与话题
                      </a-button>
                      <a-button
                        size="small"
                        href="https://creator.douyin.com"
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <CloudUploadOutlined /> 去抖音创作者平台
                      </a-button>
                    </a-space>
                    <div v-if="publishJob.status === 'succeeded'" class="hint">
                      手动发布包：下载成片与封面、复制文案，再到创作者平台上传——这一期不代发，账号凭据不经过本站。
                    </div>
                  </div>
                </a-form-item>
              </div>
            </a-form>
          </a-tab-pane>
        </a-tabs>
      </section>

      <!-- 预览：9:16 竖屏框 + 字幕叠加 + 这一镜最近一次作业的现场 -->
      <section class="wb-preview">
        <div class="pane-title">
          预览
          <span class="muted">9 : 16</span>
        </div>
        <div class="preview-body">
          <div class="phone">
            <video
              v-if="clipUrl"
              class="phone-frame"
              :src="clipUrl"
              controls
              playsinline
              preload="metadata"
            />
            <img
              v-else-if="frameUrl"
              class="phone-frame"
              :src="frameUrl"
              alt="本镜首帧"
              loading="lazy"
            />
            <div v-else class="phone-empty">
              <LoadingOutlined v-if="currentActive" spin />
              <span>{{ previewHint }}</span>
            </div>
            <p
              v-if="current?.on_screen_text"
              :class="['phone-subtitle', { 'phone-subtitle--video': clipUrl }]"
            >
              {{ current.on_screen_text }}
            </p>
          </div>

          <!-- 脚本生成是两步串行：网关日志按调用分行，这里给整条的分解 -->
          <div v-if="scriptRun" class="job-meta">
            <div class="job-kind">
              脚本生成
              <a-button type="link" size="small" class="job-kind-link" @click="goToScriptRun">
                运行详情
              </a-button>
            </div>
            <div v-for="step in scriptSteps" :key="step.key" class="job-row">
              <span class="muted">{{ step.label }}</span>
              <span class="job-step">
                <a-tag :color="runStatusColor(step.status)">{{ runStatusText(step.status) }}</a-tag>
                {{ step.elapsed }}
              </span>
            </div>
            <div class="job-row">
              <span class="muted">合计</span><span>{{ scriptTotalText }}</span>
            </div>
            <div class="hint">
              先定钩子与结构，再照它切镜，两步串行。网关日志一次调用一行，只看一行会少算前一步。
            </div>
          </div>

          <!-- 两类作业各一块：状态与产物都只在作业行上，这里不存第二份 -->
          <div v-for="item in currentJobs" :key="item.kind" class="job-meta">
            <div class="job-kind">{{ item.label }}</div>
            <div class="job-row">
              <span class="muted">状态</span>
              <a-tag :color="jobColor(item.job)">{{ jobTextOf(item.job, item.kind) }}</a-tag>
            </div>
            <template v-if="item.job">
              <div class="job-row">
                <span class="muted">模型</span><span>{{ item.job.model_name || '—' }}</span>
              </div>
              <div class="job-row">
                <span class="muted">用时</span>
                <span>{{ item.job.elapsed_ms ? fmtElapsed(item.job.elapsed_ms) : '—' }}</span>
              </div>
              <div class="job-row">
                <span class="muted">大小</span>
                <span>{{ item.job.file_size ? fmtSize(item.job.file_size) : '—' }}</span>
              </div>
              <a-alert
                v-if="item.job.error_msg"
                type="error"
                show-icon
                class="job-error"
                :message="item.job.error_msg"
              />
            </template>
            <a-space :size="8" wrap class="job-ops">
              <a-button
                v-if="isActiveJob(item.job)"
                size="small"
                danger
                @click="onCancelJob(item.job?.id)"
              >
                取消
              </a-button>
              <template v-else-if="item.kind === 'image'">
                <a-button size="small" :disabled="!canRenderOne" @click="onRenderOne">
                  <VideoCameraOutlined /> {{ frameUrl ? '重做这一镜' : '渲这一镜' }}
                </a-button>
                <a-button
                  v-if="frameUrl && current?.image_job?.id"
                  size="small"
                  :loading="storing"
                  title="复制一份进素材库，删掉这个项目它也还在"
                  @click="onSaveToMaterial"
                >
                  <PlusOutlined /> 存为素材
                </a-button>
              </template>
              <template v-else>
                <!-- antd 的 Checkbox 不把 title 透到 label 上，禁用原因得挂在外层。 -->
                <span :title="referencePreviousHint">
                  <a-checkbox
                    v-model:checked="referencePrevious"
                    :disabled="!videoReferenceReady"
                  >
                    参考上一版
                  </a-checkbox>
                </span>
                <a-button
                  size="small"
                  :disabled="!canRenderClipOne"
                  :title="clipProblem(current)"
                  @click="onRenderClipOne"
                >
                  <PlayCircleOutlined /> {{ clipUrl ? '重做片段' : '渲片段' }}
                </a-button>
              </template>
            </a-space>
          </div>

          <div class="pane-title">时间轴</div>
          <div class="timeline">
            <span
              v-for="(shot, index) in shots"
              :key="`t-${shot.id || index}`"
              :class="['timeline-seg', { active: index === selected }]"
              :style="{ flexGrow: shot.duration_s || 1, flexBasis: '0' }"
              :title="`第 ${index + 1} 镜 · ${shot.duration_s} 秒`"
              @click="selected = index"
            >
              {{ index + 1 }}
            </span>
            <span v-if="!shots.length" class="muted">还没有镜头</span>
          </div>
          <div class="hint timeline-hint">
            起点由镜头顺序累加：{{ totalDuration }} 秒。一镜一段，整片合成在「全片 · 发布」里。
          </div>
        </div>
      </section>
    </main>

    <!-- 这一镜要引用的素材：库是账号级的，这里只挑 id，文件的上传与删除都在素材库页 -->
    <MaterialPicker
      v-model:open="pickerOpen"
      :selected="current?.material_ids ?? []"
      :max="MAX_REFS_PER_SHOT"
      @confirm="onPickMaterials"
    />

    <!-- 渲染前那屏确认：这次发几条、账户里还有几个位子、有没有镜头缺提示词 -->
    <a-modal
      v-model:open="renderModal"
      title="渲染首帧"
      ok-text="开始渲染"
      cancel-text="取消"
      width="720px"
      :ok-button-props="{ disabled: renderOkDisabled }"
      @ok="onRenderConfirm"
    >
      <a-alert
        type="info"
        show-icon
        class="render-alert"
        :message="`这次发 ${renderTargets.length} 条图片作业，一镜一张`"
        :description="capacityHint"
      />
      <a-alert
        v-if="missingPromptInTarget.length"
        type="warning"
        show-icon
        class="render-alert"
        :message="`第 ${missingPromptInTarget.join('、')} 镜还没有首帧提示词`"
        description="整批里有一个镜头缺提示词就会被服务端拒掉（不是跳过它），先补上或只勾选有提示词的镜头。"
      />
      <div class="toolbar toolbar--between toolbar--snug">
        <span class="muted">不勾选表示渲染全部 {{ shots.length }} 镜</span>
        <a-button v-if="picked.length" size="small" type="text" @click="picked = []">
          清空勾选
        </a-button>
      </div>
      <a-table
        :columns="renderColumns"
        :data-source="shots"
        row-key="id"
        size="small"
        :pagination="false"
        :row-selection="{ selectedRowKeys: picked, onChange: onPick }"
        :scroll="{ y: 320 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'scene'">
            <span>{{ record.scene || record.image_prompt || '（还没写画面）' }}</span>
          </template>
          <template v-else-if="column.key === 'prompt'">
            <a-tag v-if="record.image_prompt" color="success">已写</a-tag>
            <a-tag v-else color="error">缺提示词</a-tag>
          </template>
          <template v-else-if="column.key === 'job'">
            <span class="muted">{{ jobText(record) }}</span>
          </template>
        </template>
      </a-table>
    </a-modal>

    <!-- 片段渲染前那屏确认：这次发几条、视频位子还剩几个、每一镜这一档能不能发 -->
    <a-modal
      v-model:open="clipModal"
      title="渲染片段"
      ok-text="开始渲染"
      cancel-text="取消"
      width="760px"
      :ok-button-props="{ disabled: clipOkDisabled }"
      @ok="onClipConfirm"
    >
      <a-alert
        type="info"
        show-icon
        class="render-alert"
        :message="`这次发 ${renderTargets.length} 条视频作业，一镜一段`"
        :description="clipCapacityHint"
      />
      <a-alert
        v-if="clipResolutionWarning"
        type="warning"
        show-icon
        class="render-alert"
        message="这一档分辨率可能超出型号上限"
        :description="clipResolutionWarning"
      />
      <a-alert
        v-if="clipProblemsInTarget.length"
        type="warning"
        show-icon
        class="render-alert"
        message="这些镜头现在发不出去"
        :description="clipProblemsText"
      />
      <div class="toolbar toolbar--snug">
        <a-checkbox v-model:checked="referencePrevious" :disabled="!videoReferenceReady">
          参考上一版片段
        </a-checkbox>
        <span class="muted">{{ referencePreviousHint }}</span>
      </div>
      <div class="toolbar toolbar--between toolbar--snug">
        <span class="muted">不勾选表示渲染全部 {{ shots.length }} 镜</span>
        <a-button v-if="picked.length" size="small" type="text" @click="picked = []">
          清空勾选
        </a-button>
      </div>
      <a-table
        :columns="clipColumns"
        :data-source="shots"
        row-key="id"
        size="small"
        :pagination="false"
        :row-selection="{ selectedRowKeys: picked, onChange: onPick }"
        :scroll="{ y: 320 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'scene'">
            <span>{{ record.scene || record.image_prompt || '（还没写画面）' }}</span>
          </template>
          <template v-else-if="column.key === 'mode'">
            <span>{{ genModeLabel(record.gen_mode) }}</span>
          </template>
          <template v-else-if="column.key === 'ready'">
            <a-tag v-if="!clipProblem(record)" color="success">可发</a-tag>
            <a-tag v-else color="error">{{ clipProblem(record) }}</a-tag>
          </template>
          <template v-else-if="column.key === 'job'">
            <span class="muted">{{ jobTextOf(record.video_job, 'video') }}</span>
          </template>
        </template>
      </a-table>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CloseOutlined,
  CloudUploadOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  LoadingOutlined,
  MinusOutlined,
  PictureOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons-vue'
import { Empty, Modal, message } from 'ant-design-vue'
import {
  materialsApi,
  settingsApi,
  videosApi,
  type MaterialAsset,
  type MaterialKind,
  type MediaCapacityOut,
  type MediaJob,
  type MediaJobKind,
  type MediaJobStatus,
  type PublishJob,
  type PublishJobStatus,
  type VideoBrief,
  type VideoGenMode,
  type VideoParams,
  type VideoPlatform,
  type VideoProjectDetail,
  type VideoProjectStatus,
  type VideoPublish,
  type VideoScriptRun,
  type VideoScriptStep,
  type VideoShot,
  type VideoShotInput,
  type VideoStyle,
} from '@/api'
import MaterialPicker from '@/components/videos/MaterialPicker.vue'
import { copyText } from '@/utils/clipboard'
import { saveTextFile } from '@/utils/download'
import { errorText } from '@/utils/errors'
import { fmtElapsed, fmtSize, runStatusColor, runStatusText } from '@/utils/format'
import { createPollLoop } from '@/utils/polling'
import { buildAss, buildSrt, subtitleCues } from '@/utils/videoSubtitles'

// 短视频工作台：左边分镜（顺序即成片顺序）、中间两套提示词与全片参数、右边竖屏预览。
//
// 三段进度在这里各看各的：脚本阶段是项目状态（从认领本项目的运行翻译过来），
// 渲染阶段是这一镜最近那两条 media_job（首帧 / 片段）的状态，成片阶段是项目最近
// 一条 publish_job（本机 ffmpeg 拼接）。详情接口轮询一次就把这几件事都说清了。
//
// 渲染分两步：先出首帧，再由首帧出片段（t2v 与参考图档不需要首帧）；各镜片段都出片后，
// 在「全片 · 发布」里合成一个 MP4。字幕不烧进画面——预览按屏幕文字叠加，导出的是
// 旁边的 SRT / ASS 文件。

const route = useRoute()
const router = useRouter()
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

// 这些数字与服务端同源：镜头数/提示词长度/时长档口都在 config.py 与 service.py 里定，
// 界面上提前拦一条，就少一次「提交后被裁掉却看不见」的困惑。
const MAX_SHOTS = 12
const PROMPT_MAX = 480
const SHOT_MIN_S = 4
const SHOT_MAX_S = 15
const MAX_REFS_PER_SHOT = 4
const MAX_SEGMENTS = 8

const GEN_MODE_OPTIONS: { value: VideoGenMode; label: string; disabled?: boolean }[] = [
  { value: 'first_frame', label: '首帧生视频（默认）' },
  { value: 't2v', label: '文生视频' },
  { value: 'first_last_frame', label: '首尾帧生视频（还没开）', disabled: true },
  { value: 'reference', label: '参考图生视频' },
]
const GEN_MODE_HINTS: Record<VideoGenMode, string> = {
  first_frame: '先出这一镜的首帧，再由首帧生成片段：长什么样由首帧定，怎么动由运动提示词定。',
  t2v: '直接从提示词生成视频，不需要首帧——画面一致性强依赖参考图时不建议选它。',
  first_last_frame:
    '给定首尾两帧，中间由模型补。这一档还没开：尾帧要另起一条产物链路，选了会在入队时被拒。',
  reference: '用参考图当主体，模型按参考图里的形象生成：人物一致性靠这一档。只认图片类素材。',
}
const RATIO_OPTIONS = [
  { value: 'adaptive', label: '自适应' },
  { value: '9:16', label: '9 : 16（竖屏）' },
  { value: '3:4', label: '3 : 4' },
  { value: '1:1', label: '1 : 1' },
  { value: '4:3', label: '4 : 3' },
  { value: '16:9', label: '16 : 9' },
  { value: '21:9', label: '21 : 9' },
]
const RESOLUTION_OPTIONS = ['480p', '720p', '1080p'].map((value) => ({ value, label: value }))
const IMAGE_SIZE_OPTIONS = ['1K', '2K', '4K'].map((value) => ({ value, label: value }))

/** 作业还没落定：这三档要接着轮询，且界面上不允许说「已完成」。 */
const ACTIVE_JOB_STATUS: MediaJobStatus[] = ['queued', 'running', 'downloading']

const projectId = ref(Number(route.params.id) || 0)

const title = ref('')
const platform = ref<VideoPlatform>('douyin')
const state = ref<VideoProjectStatus | string>('analyzing')
const idea = ref('')
const requirement = ref('')
const script = ref('')
const brief = reactive<VideoBrief>({
  hook_type: '',
  hook: '',
  audience: '',
  goal: '',
  structure: [],
  duration_s: 15,
  risk_notes: '',
})
const style = reactive<VideoStyle>({ visual_style: '', persona: '', palette: '' })
const params = reactive<VideoParams>({
  ratio: '9:16',
  resolution: '720p',
  watermark: false,
  generate_audio: false,
  image_size: '2K',
})
const publish = reactive<VideoPublish>({ title_candidates: [], tags: [], cover_prompt: '' })

const shots = ref<VideoShot[]>([])
// 素材库的本地缓存：镜头行上只有 id，这一份用来把 id 翻回缩略图与名字。
// 详情接口几秒轮一次，所以它按项目取一次、选完素材再取一次，不跟着轮询走。
const materials = ref<MaterialAsset[]>([])
/** 封面无镜头行可挂：详情接口只把它作为「最新一次成功的封面作业」的 URL 带回来。 */
const coverUrl = ref('')
/** 最近一次脚本生成的现场：两步串行，各自的状态与耗时都由运行行给。 */
const scriptRun = ref<VideoScriptRun | null>(null)
/** 最近一次成片合成作业；从没合成过为 null，历史在服务端表里。 */
const publishJob = ref<PublishJob | null>(null)
/** 合成按钮自己的点击态：请求回来前防重复点，作业本身的进度看 publishJob。 */
const composing = ref(false)
/** 只在脚本生成时走动的秒级钟，让「已跑 3 分 12 秒」自己走，不必等下一次轮询。 */
const nowMs = ref(Date.now())

/** 发布作业还没落定的两档：与后端 PUBLISH_ACTIVE_STATES 同义。 */
const PUBLISH_ACTIVE: PublishJobStatus[] = ['queued', 'running']

const selected = ref(0)
const editorTab = ref<'shot' | 'film'>('shot')
const dirtyProject = ref(false)
const dirtyShots = ref(false)
const saving = ref(false)
const loadError = ref('')
const imageReady = ref(true)
const videoReady = ref(true)
/** 默认视频模型的名字：确认屏上按它提示分辨率档位，认不出型号时以上游报错为准。 */
const videoModel = ref('')

const pickerOpen = ref(false)
const renderModal = ref(false)
const clipModal = ref(false)
const picked = ref<number[]>([])
const capacity = ref<MediaCapacityOut | null>(null)
const storing = ref(false)
/** 重做片段时把上一版成片当参考视频送上去。默认关：开着就拿不到「完全重来一版」了。 */
const referencePrevious = ref(false)

const current = computed(() => shots.value[selected.value])
const dirty = computed(() => dirtyProject.value || dirtyShots.value)
const generating = computed(() => state.value === 'analyzing')
const totalDuration = computed(() =>
  shots.value.reduce((sum, shot) => sum + (shot.duration_s || 0), 0),
)
const currentActive = computed(
  () => isActiveJob(current.value?.image_job) || isActiveJob(current.value?.video_job),
)
const frameUrl = computed(() => frameUrlOf(current.value))
const clipUrl = computed(() => clipUrlOf(current.value))
const rendering = computed(() =>
  shots.value.some((shot) => isActiveJob(shot.image_job) || isActiveJob(shot.video_job)),
)
/** 预览区那两块作业现场：没渲过也要摆出来，两个「渲」按钮得有地方待。 */
const currentJobs = computed(() =>
  current.value
    ? [
        { label: '首帧作业', kind: 'image' as const, job: current.value.image_job },
        { label: '片段作业', kind: 'video' as const, job: current.value.video_job },
      ]
    : [],
)
/** 脚本生成那两步的 key 长在流程定义里，文案在这里翻。 */
const SCRIPT_STEP_LABELS: Record<string, string> = { brief: '创作卡', script: '分镜脚本' }
const scriptSteps = computed(() =>
  (scriptRun.value?.steps ?? []).map((step) => ({
    key: step.key,
    label: SCRIPT_STEP_LABELS[step.key] ?? step.key,
    status: step.status,
    elapsed: scriptElapsedText(step),
  })),
)
/** 合计取运行行的墙上时间，不是把两步相加——口径与运行详情页一致。 */
const scriptTotalText = computed(() => {
  const run = scriptRun.value
  if (!run) return '—'
  if (run.elapsed_ms != null) return fmtElapsed(run.elapsed_ms)
  return run.started_at ? `已跑 ${fmtElapsed(liveMs(run.started_at))}` : '—'
})
/** 这次要发出去的镜头：一个都没勾就是全部——与后端「shot_ids 空表示全部」同一口径。 */
const renderTargets = computed(() =>
  picked.value.length
    ? shots.value.filter((shot) => picked.value.includes(shot.id))
    : shots.value,
)
/** 只拦「这一批里真会发出去的」镜头：缺提示词时服务端整批拒，不是跳过它。 */
const missingPromptInTarget = computed(() =>
  renderTargets.value.filter((shot) => !shot.image_prompt.trim()).map((shot) => shot.sort_order),
)
const renderBlockHint = computed(() => {
  if (!shots.value.length) return '先生成脚本拿到分镜'
  if (dirtyShots.value) return '分镜还有未保存的改动，先保存再渲染'
  if (generating.value) return '脚本还在生成'
  return ''
})
const renderOkDisabled = computed(
  () => Boolean(renderBlockHint.value) || missingPromptInTarget.value.length > 0,
)
/** 片段这一档的门槛：首帧那几条之外，还要有验证过的视频模型。 */
const clipBlockHint = computed(() =>
  videoReady.value ? renderBlockHint.value : '先配好用途为「视频」的模型',
)
/** 余量取不到时不拦：真正的门槛在服务端入队那一步，与 loadReadiness 同一取舍。 */
const videoReferenceReady = computed(() => capacity.value?.video_reference_ready ?? true)
const referencePreviousHint = computed(() =>
  videoReferenceReady.value
    ? '把本镜上一版成片当参考视频一起送上去，沿用它的主体形象与运镜；只有已出片的镜头能发。'
    : '服务端还没配 PUBLIC_MEDIA_BASE_URL：方舟的视频参考只收公网地址，配好才能用这一档。',
)
const clipProblemsInTarget = computed(() =>
  renderTargets.value
    .map((shot) => ({ sort_order: shot.sort_order, problem: clipProblem(shot) }))
    .filter((item) => item.problem),
)
const clipProblemsText = computed(
  () =>
    clipProblemsInTarget.value
      .map((item) => `第 ${item.sort_order} 镜：${item.problem}`)
      .join('；') +
    '。整批里有一镜不合格就会被服务端拒掉（不是跳过它），先补齐或只勾选能发的镜头。',
)
const clipOkDisabled = computed(
  () => Boolean(clipBlockHint.value) || clipProblemsInTarget.value.length > 0,
)
const clipCapacityHint = computed(() => {
  const video = (capacity.value?.jobs ?? []).find((item) => item.kind === 'video')
  const base = video
    ? `视频作业在途 ${video.inflight}/${video.limit}：超出上限的会排在本地队列里依次提交。`
    : '并发余量没取到，入队后按排队处理。'
  const model = videoModel.value ? `当前视频模型：${videoModel.value}。` : ''
  return `${base}${model}出片按秒计费，一条几十秒到几分钟，这一屏关掉也不影响渲染。`
})
/** 型号认得出来才提示：接入点 ID（ep-…）看不出家族，那种情况以上游报错为准。 */
const clipResolutionWarning = computed(() => {
  const name = videoModel.value.toLowerCase()
  if (params.resolution !== '1080p' || !(name.includes('fast') || name.includes('mini'))) return ''
  return `「${videoModel.value}」是 fast/mini 档，只支持到 720p，1080p 会被上游拒掉；先在「全片 · 渲染参数」里调回 720p。`
})
/** 成片合成还占着位子：这一档要继续轮询，也不允许再发一条。 */
const publishActive = computed(
  () => Boolean(publishJob.value) && PUBLISH_ACTIVE.includes(publishJob.value!.status),
)
/**
 * 合成按钮的前置门槛，与服务端 ``compose_publish`` 的拒绝一一对应：
 * 预检先把人话摆在按钮旁边，服务端那道是绕过界面时的同一口径。
 */
const composeProblem = computed(() => {
  if (!shots.value.length) return '还没有镜头，先生成脚本'
  if (dirtyProject.value || dirtyShots.value) return '有未保存的改动，先保存再合成'
  if (generating.value) return '脚本还在生成'
  if (rendering.value) return '还有首帧或片段在渲染'
  const missing = shots.value
    .filter((shot) => shot.video_job?.status !== 'succeeded' || !shot.video_job?.url)
    .map((shot) => shot.sort_order)
  if (missing.length) return `第 ${missing.join('、')} 镜还没有可用的视频片段，先把片段渲完`
  if (publishActive.value) return '成片正在合成中，等它结束再发起'
  return ''
})
const canCompose = computed(() => !composeProblem.value && !composing.value)
/** 手动发布包要复制的文案：首条标题候选（没有就项目名）+ 统一补 # 的话题。 */
const publishCopyText = computed(() => {
  const heading = publish.title_candidates[0] || title.value
  const tags = publish.tags
    .map((tag) => tag.trim())
    .filter(Boolean)
    .map((tag) => (tag.startsWith('#') ? tag : `#${tag}`))
    .join(' ')
  return [heading, tags].filter(Boolean).join('\n')
})
/** 一格引用：库里还留着的是素材本身，删掉了的也要占个位子，否则人看不出引用去了哪儿。 */
type MaterialChip = {
  id: string
  name: string
  kind: MaterialKind | null
  url: string
  missing: boolean
}

const materialById = computed(
  // 镜头上存的是字符串 id（后端 JSON 里一直是字符串），库里的主键是数字：按字符串对齐。
  () => new Map(materials.value.map((item) => [String(item.id), item])),
)
const currentMaterials = computed<MaterialChip[]>(() =>
  (current.value?.material_ids ?? []).map((id) => {
    const asset = materialById.value.get(id)
    return {
      id,
      name: asset?.name ?? '素材已删除',
      kind: asset?.kind ?? null,
      url: asset?.url ?? '',
      missing: !asset,
    }
  }),
)
const canRenderOne = computed(
  () =>
    Boolean(current.value) &&
    !dirtyShots.value &&
    Boolean(current.value?.image_prompt.trim()) &&
    !isActiveJob(current.value?.image_job),
)
const canRenderClipOne = computed(
  () =>
    Boolean(current.value) &&
    !dirtyShots.value &&
    !clipProblem(current.value) &&
    !isActiveJob(current.value?.video_job),
)
const capacityHint = computed(() => {
  const jobs = capacity.value?.jobs ?? []
  const image = jobs.find((item) => item.kind === 'image')
  const base = image
    ? `图片作业在途 ${image.inflight}/${image.limit}：超出上限的会排在本地队列里依次提交，不会一次性撞给上游。`
    : '并发余量没取到，入队后按排队处理。'
  const transport =
    capacity.value?.image_transport === 'public_url'
      ? '参考图按公网地址送出。'
      : '参考图与首帧都落在服务器本地，送出时内联进请求体。'
  return `${base}${transport}出片需要几十秒到几分钟，这一屏关掉也不影响渲染。`
})
const previewHint = computed(() => {
  if (!current.value) return '左边还没有镜头'
  if (!current.value.image_prompt.trim()) return '这一镜还没有首帧提示词'
  const job = current.value.image_job
  if (!job) return '还没有渲过这一镜'
  if (ACTIVE_JOB_STATUS.includes(job.status)) return jobStatusText(job.status)
  if (job.status === 'failed' || job.status === 'expired') return job.error_msg || '上一次渲染失败'
  return '这一镜还没有出图'
})

// ---- 载入与轮询 --------------------------------------------------------------

function blankShot(): VideoShot {
  // 新镜头的 id 由服务端发放：本地用 0 占位，保存时转成「不带 id = 新建」。
  return {
    id: 0,
    sort_order: 0,
    start_s: 0,
    duration_s: 5,
    scene: '',
    camera: '',
    action: '',
    dialogue: '',
    on_screen_text: '',
    image_prompt: '',
    video_prompt: '',
    gen_mode: 'first_frame',
    material_ids: [],
    image_job: null,
    video_job: null,
  }
}

function applyDetail(detail: VideoProjectDetail) {
  title.value = detail.title
  platform.value = detail.platform
  state.value = detail.status
  idea.value = detail.idea
  requirement.value = detail.requirement
  script.value = detail.script
  Object.assign(brief, detail.brief)
  if (!Array.isArray(brief.structure)) brief.structure = []
  Object.assign(style, detail.style)
  Object.assign(params, detail.params)
  Object.assign(publish, detail.publish)
  shots.value = detail.shots
  coverUrl.value = detail.cover_url
  scriptRun.value = detail.script_run
  publishJob.value = detail.publish_job
  if (selected.value >= detail.shots.length) selected.value = Math.max(0, detail.shots.length - 1)
  dirtyProject.value = false
  dirtyShots.value = false
}

async function load(quiet = false) {
  try {
    const detail = await videosApi.detail(projectId.value)
    applyDetail(detail)
    loadError.value = ''
  } catch (err) {
    if (!quiet) loadError.value = errorText(err, '载入失败')
  }
}

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    imageReady.value = status.image.ready
    videoReady.value = status.video.ready
    videoModel.value = status.video.model || ''
  } catch {
    // 取不到就不拦：真正的门槛在服务端入队那一步，这里只是提前说一句。
    imageReady.value = true
    videoReady.value = true
  }
}

// 脚本在跑、有镜头作业在途、或成片还在合成时轮询；都落定就停表，不留空转定时器。
const pollLoop = createPollLoop(
  async () => {
    const before = lastJobStatus()
    const publishBefore = publishJob.value?.status
    await load(true)
    announceJobs(before)
    announcePublish(publishBefore)
  },
  {
    // 后端媒体循环 5s 才向方舟查一次视频任务，图片也在同一个 tick 里落定：
    // 前端比这更密只是白打详情请求，看不到更早的状态。
    interval: 5000,
    shouldContinue: () => generating.value || rendering.value || publishActive.value,
  },
)

function syncPolling() {
  if (generating.value || rendering.value || publishActive.value) pollLoop.start()
  else pollLoop.stop()
}

// 秒级钟只服务于「脚本生成中」那一段：跑完就停，切走标签页也停，不留空转定时器。
let clock: ReturnType<typeof setInterval> | null = null

function syncClock() {
  if (generating.value && !clock) {
    nowMs.value = Date.now()
    clock = setInterval(() => {
      nowMs.value = Date.now()
    }, 1000)
  } else if (!generating.value && clock) {
    stopClock()
  }
}

function stopClock() {
  if (clock) clearInterval(clock)
  clock = null
}

watch(generating, syncClock, { immediate: true })

// 「刚出图 / 刚出片」和「刚失败」都值得说一句：几十秒到几分钟的过程，用户不会一直盯着。
// 键带上作业类别：一镜两条作业，只按镜号记会让片段的变化被首帧的盖掉。
function jobKey(shotId: number, kind: MediaJobKind): string {
  return `${shotId}:${kind}`
}

function lastJobStatus(): Map<string, MediaJobStatus | undefined> {
  const map = new Map<string, MediaJobStatus | undefined>()
  for (const shot of shots.value) {
    if (!shot.id) continue
    map.set(jobKey(shot.id, 'image'), shot.image_job?.status)
    map.set(jobKey(shot.id, 'video'), shot.video_job?.status)
  }
  return map
}

function announceJobs(before: Map<string, MediaJobStatus | undefined>) {
  if (!before.size) return
  const done = { image: 0, video: 0 }
  let failed = 0
  for (const shot of shots.value) {
    for (const kind of ['image', 'video'] as const) {
      const job = kind === 'image' ? shot.image_job : shot.video_job
      const previous = before.get(jobKey(shot.id, kind))
      if (!previous || !ACTIVE_JOB_STATUS.includes(previous) || !job) continue
      if (job.status === 'succeeded') done[kind] += 1
      else if (job.status === 'failed' || job.status === 'expired') failed += 1
    }
  }
  if (done.image) message.success(`${done.image} 镜首帧已出图`)
  if (done.video) message.success(`${done.video} 镜片段已出片`)
  if (failed) message.error(`${failed} 条渲染失败，预览区有原因`)
}

/** 成片合成从在途翻到终态时说一句：本机拼接也要几十秒，用户不会一直盯着按钮。 */
function announcePublish(before: PublishJobStatus | undefined) {
  if (!before || !PUBLISH_ACTIVE.includes(before)) return
  const after = publishJob.value?.status
  if (after === 'succeeded') message.success('成片合成完成，「全片 · 发布」里可以下载了')
  else if (after === 'failed') message.error('成片合成失败，原因在发布区的作业卡片里')
}

// ---- 分镜编辑 ----------------------------------------------------------------

function markShotDirty() {
  dirtyShots.value = true
}

function addShot() {
  if (shots.value.length >= MAX_SHOTS) {
    message.warning(`一个项目最多 ${MAX_SHOTS} 个镜头`)
    return
  }
  shots.value.push(blankShot())
  selected.value = shots.value.length - 1
  markShotDirty()
}

function moveShot(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= shots.value.length) return
  const rows = shots.value.slice()
  const [row] = rows.splice(index, 1)
  rows.splice(target, 0, row)
  shots.value = rows
  selected.value = target
  markShotDirty()
}

function removeShot(index: number) {
  shots.value.splice(index, 1)
  selected.value = Math.max(0, Math.min(selected.value, shots.value.length - 1))
  markShotDirty()
}

function setSegment(index: number, value: string) {
  brief.structure[index] = value
  dirtyProject.value = true
}

function addSegment() {
  if (brief.structure.length >= MAX_SEGMENTS) return
  brief.structure.push('')
  dirtyProject.value = true
}

function removeSegment(index: number) {
  brief.structure.splice(index, 1)
  dirtyProject.value = true
}

// ---- 保存 / 脚本 / 渲染 / 字幕 ------------------------------------------------

function shotsInput(): VideoShotInput[] {
  // 数组顺序就是镜头顺序：sort_order 与 start_s 由服务端按这个顺序重算，前端不自己抄一份。
  return shots.value.map((shot) => ({
    id: shot.id || null,
    duration_s: shot.duration_s,
    scene: shot.scene,
    camera: shot.camera,
    action: shot.action,
    dialogue: shot.dialogue,
    on_screen_text: shot.on_screen_text,
    image_prompt: shot.image_prompt,
    video_prompt: shot.video_prompt,
    gen_mode: shot.gen_mode,
    // 服务端也会截到 4 条，这里先截一次：不给人一个「保存后少了两条」的意外。
    material_ids: shot.material_ids.slice(0, MAX_REFS_PER_SHOT),
  }))
}

function projectPayload() {
  return {
    title: title.value.trim() || undefined,
    platform: platform.value,
    idea: idea.value.trim() || undefined,
    requirement: requirement.value.trim() || undefined,
    brief: { ...brief, duration_s: Number(brief.duration_s) || 15 },
    script: script.value,
    style: { ...style },
    params: { ...params },
    publish: {
      title_candidates: publish.title_candidates.slice(0, 6),
      tags: publish.tags.slice(0, 8),
      cover_prompt: publish.cover_prompt,
    },
  }
}

async function onSave() {
  saving.value = true
  try {
    let detail = await videosApi.save(projectId.value, projectPayload())
    if (dirtyShots.value) detail = await videosApi.saveShots(projectId.value, shotsInput())
    // 用服务端规范化的结果对齐：新镜的 id、夹过的时长、截过的提示词都在它那份里。
    applyDetail(detail)
    message.success('已保存')
  } catch (err) {
    message.error(errorText(err, '保存失败'))
  } finally {
    saving.value = false
  }
}

function onRegenerate() {
  if (dirty.value) {
    Modal.confirm({
      title: '重新生成脚本会覆盖当前分镜',
      content: '创作卡、口播文案与全部镜头都会被这一轮的结果整包替换，未保存的改动会丢掉。',
      okText: '重新生成',
      cancelText: '取消',
      onOk: () => doRegenerate(),
    })
    return
  }
  void doRegenerate()
}

async function doRegenerate() {
  try {
    await videosApi.regenerate(projectId.value, {
      requirement: requirement.value.trim() || undefined,
      duration_s: Number(brief.duration_s) || undefined,
    })
    state.value = 'analyzing'
    message.success('已开始生成脚本，当前分镜会被覆盖')
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '重新生成失败'))
  }
}

async function openRender() {
  picked.value = []
  renderModal.value = true
  try {
    capacity.value = await videosApi.capacity()
  } catch {
    // 并发余量只是确认屏上的参考信息，取不到不拦渲染。
    capacity.value = null
  }
}

function onPick(keys: (string | number)[]) {
  picked.value = keys.map((key) => Number(key))
}

async function onRenderConfirm() {
  try {
    const detail = await videosApi.renderFrames(
      projectId.value,
      // 不勾表示全部：服务端把空数组当成「项目里全部镜头」。
      picked.value.length ? picked.value : [],
    )
    applyDetail(detail)
    renderModal.value = false
    message.success('已开始渲染首帧，出片需要几十秒到几分钟')
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '渲染失败'))
  }
}

async function onRenderOne() {
  if (!current.value?.id) return
  try {
    const detail = await videosApi.renderFrames(projectId.value, [current.value.id])
    applyDetail(detail)
    message.success('已开始渲染这一镜')
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '渲染失败'))
  }
}

/** 并发余量与「参考上一版」能不能用都从这一条接口来；取不到不拦渲染。 */
async function loadCapacity() {
  try {
    capacity.value = await videosApi.capacity()
  } catch {
    capacity.value = null
  }
}

async function openClipRender() {
  picked.value = []
  clipModal.value = true
  await loadCapacity()
}

async function onClipConfirm() {
  try {
    const detail = await videosApi.renderClips(
      projectId.value,
      // 不勾表示全部：服务端把空数组当成「项目里全部镜头」。
      picked.value.length ? picked.value : [],
      referencePrevious.value,
    )
    applyDetail(detail)
    clipModal.value = false
    message.success('已开始渲染片段，出片需要几十秒到几分钟')
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '渲染失败'))
  }
}

async function onRenderClipOne() {
  if (!current.value?.id) return
  try {
    const detail = await videosApi.renderClips(
      projectId.value,
      [current.value.id],
      referencePrevious.value,
    )
    applyDetail(detail)
    message.success('已开始渲染这一镜的片段')
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '渲染失败'))
  }
}

async function onRenderCover() {
  try {
    const detail = await videosApi.renderCover(projectId.value)
    applyDetail(detail)
    message.success('已开始生成封面')
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '封面生成失败'))
  }
}

async function onCancelJob(jobId?: number) {
  if (!jobId) return
  try {
    const detail = await videosApi.cancelJob(projectId.value, jobId)
    applyDetail(detail)
    message.success('已取消')
  } catch (err) {
    message.error(errorText(err, '取消失败'))
  }
}

async function onSaveToMaterial() {
  const shotId = current.value?.id
  if (!shotId) return
  storing.value = true
  try {
    const detail = await videosApi.saveFrameToMaterial(projectId.value, shotId)
    applyDetail(detail)
    // 复制出去的这条要立刻能在「参考素材」里挑到，所以顺手对一遍清单。
    await loadMaterials()
    message.success('已存进素材库，其它选题也能复用')
  } catch (err) {
    message.error(errorText(err, '存为素材失败'))
  } finally {
    storing.value = false
  }
}

function onExportSubtitle({ key }: { key: string | number }) {
  if (!subtitleCues(shots.value).length) {
    message.warning('还没有镜头写了屏幕文字，字幕导出用的是每一镜的「屏幕文字」')
    return
  }
  const name = (title.value || 'subtitle').replace(/[\\/:*?"<>|]/g, '_')
  if (key === 'ass') saveTextFile(`${name}.ass`, buildAss(shots.value, title.value), 'text/plain')
  else saveTextFile(`${name}.srt`, buildSrt(shots.value), 'text/plain')
  message.success('字幕文件已下载（时间轴按当前镜头顺序）')
}

// ---- 发布（成片合成 + 手动发布包） --------------------------------------------

async function onCompose() {
  if (!canCompose.value) return
  composing.value = true
  try {
    // 入队即返回：后台任务在本机跑 ffmpeg，进度靠详情轮询里的 publish_job 跟随。
    const detail = await videosApi.compose(projectId.value)
    applyDetail(detail)
    message.success('已开始合成成片，本机拼接通常几十秒内完成')
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '合成发起失败'))
  } finally {
    composing.value = false
  }
}

async function onCopyPublishText() {
  const ok = await copyText(publishCopyText.value)
  if (ok) message.success('标题与话题已复制，粘贴到发布框即可')
  else message.error('复制失败：浏览器没给剪贴板权限，手动从上方文案拷一下')
}

function safeFileName(fallback: string): string {
  return title.value.trim().replace(/[\\/:*?"<>|]/g, '_') || fallback
}

/** 站内 /uploads 是同源地址，download 属性给得出友好文件名，不必先拉成 blob。 */
function downloadByUrl(url: string, filename: string) {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.rel = 'noopener'
  anchor.click()
}

function urlExt(url: string, fallback: string): string {
  return url.split('?')[0].split('.').pop() || fallback
}

function onDownloadFinal() {
  const job = publishJob.value
  if (!job?.url) return
  downloadByUrl(job.url, `${safeFileName('final-video')}.${urlExt(job.url, 'mp4')}`)
}

function onDownloadCover() {
  if (!coverUrl.value) return
  downloadByUrl(coverUrl.value, `${safeFileName('cover')}-封面.${urlExt(coverUrl.value, 'jpg')}`)
}

function publishStatusText(status: PublishJobStatus): string {
  return (
    { queued: '排队中', running: '合成中', succeeded: '已合成', failed: '合成失败' }[status] ||
    status
  )
}

function publishStatusColor(status: PublishJobStatus): string {
  return (
    { queued: 'default', running: 'processing', succeeded: 'success', failed: 'error' }[status] ||
    'default'
  )
}

// ---- 素材引用 ----------------------------------------------------------------

// 素材本身归账号级的库，镜头行上只有 id：这里取一份清单，只为把 id 翻回缩略图和名字。
async function loadMaterials() {
  try {
    // 一次取满一屏，与选择器同一档：超出这一屏的老素材在挑选框里靠搜索照样能找到。
    const result = await materialsApi.list({ limit: 200 })
    materials.value = result.items
  } catch {
    // 保留上一份清单：取不到只是这一格显示成点不开，不该把已有的引用说成「已删除」。
  }
}

function onPickMaterials(ids: string[]) {
  if (!current.value) return
  current.value.material_ids = ids
  markShotDirty()
  // 挑选框里可以直接往库里传新素材，所以确认之后再对一遍清单。
  void loadMaterials()
}

function dropMaterial(id: string) {
  if (!current.value) return
  current.value.material_ids = current.value.material_ids.filter((item) => item !== id)
  markShotDirty()
}

function goToMaterials() {
  router.push('/office/materials')
}

// ---- 展示辅助 ----------------------------------------------------------------

function platformText(value: VideoPlatform | string): string {
  return { douyin: '抖音', kuaishou: '快手', other: '通用' }[value] || value
}

function statusText(value: string) {
  return (
    { uploaded: '待生成', analyzing: '生成中', ready: '已生成', error: '生成失败' }[value] || value
  )
}

function statusColor(value: string) {
  return (
    { uploaded: 'default', analyzing: 'processing', ready: 'success', error: 'error' }[value] ||
    'default'
  )
}

function genModeHint(mode: VideoGenMode) {
  return GEN_MODE_HINTS[mode] || ''
}

function jobStatusText(status: MediaJobStatus | string, kind: MediaJobKind = 'image'): string {
  const verb = kind === 'video' ? '片' : '图'
  return (
    {
      queued: '排队中',
      running: `出${verb}中`,
      downloading: '转存中',
      succeeded: `已出${verb}`,
      failed: '失败',
      cancelled: '已取消',
      expired: '超时未完成',
    }[status] || status
  )
}

function isActiveJob(job?: MediaJob | null): boolean {
  return Boolean(job && ACTIVE_JOB_STATUS.includes(job.status))
}

/** 一步的用时：落定了报服务端量到的，还在跑就按它自己的开始时间走秒。 */
function scriptElapsedText(step: VideoScriptStep): string {
  if (step.elapsed_ms != null) return fmtElapsed(step.elapsed_ms)
  if (step.status === 'running' && step.started_at) {
    return `已跑 ${fmtElapsed(liveMs(step.started_at))}`
  }
  return '—'
}

/** 服务端时钟与浏览器不齐时不让它显示成负数。 */
function liveMs(startedAt: string): number {
  return Math.max(0, nowMs.value - new Date(startedAt).getTime())
}

function jobColor(job?: MediaJob | null): string {
  const status = job?.status
  if (!status) return 'default'
  return (
    {
      queued: 'default',
      running: 'processing',
      downloading: 'processing',
      succeeded: 'success',
      failed: 'error',
      cancelled: 'warning',
      expired: 'warning',
    }[status] || 'default'
  )
}

function jobTextOf(job: MediaJob | null | undefined, kind: MediaJobKind): string {
  return job ? jobStatusText(job.status, kind) : '还没渲'
}

function jobText(shot: VideoShot): string {
  return jobTextOf(shot.image_job, 'image')
}

/** 分镜列表那行副标题：两类作业都报，没渲过片段就只报首帧。 */
function shotJobsText(shot: VideoShot): string {
  const frame = `图 ${jobText(shot)}`
  return shot.video_job ? `${frame} · 片 ${jobStatusText(shot.video_job.status, 'video')}` : frame
}

function frameUrlOf(shot?: VideoShot): string {
  return shot?.image_job?.status === 'succeeded' ? shot.image_job.url : ''
}

function clipUrlOf(shot?: VideoShot): string {
  return shot?.video_job?.status === 'succeeded' ? shot.video_job.url : ''
}

function genModeLabel(mode: VideoGenMode): string {
  return GEN_MODE_OPTIONS.find((item) => item.value === mode)?.label || mode
}

/** 参考图档只认图片类素材：视频素材要公网地址，进不了 base64 那一档。 */
function imageRefCount(shot: VideoShot): number {
  return shot.material_ids.filter((id) => materialById.value.get(id)?.kind === 'image').length
}

/**
 * 这一镜现在能不能发片段：与服务端 ``render_clips`` 的拒绝档一一对应。
 * 确认屏上先说清楚，就少一次「点了开始渲染才被整批拒掉」。
 */
function clipProblem(shot?: VideoShot): string {
  if (!shot) return '还没有镜头'
  if (shot.gen_mode === 'first_last_frame') return '首尾帧这一档还没开'
  if (!shot.video_prompt.trim()) return '缺运动提示词'
  if (shot.gen_mode === 'first_frame' && !frameUrlOf(shot)) return '首帧还没出图'
  if (shot.gen_mode === 'reference' && !imageRefCount(shot)) return '没挑图片素材'
  if (referencePrevious.value && !clipUrlOf(shot)) return '没有上一版可参考'
  return ''
}

const renderColumns = [
  { title: '镜号', dataIndex: 'sort_order', key: 'sort', width: 60 },
  { title: '画面', key: 'scene', ellipsis: true },
  { title: '时长', dataIndex: 'duration_s', key: 'duration', width: 70 },
  { title: '首帧提示词', key: 'prompt', width: 110 },
  { title: '上次渲染', key: 'job', width: 100 },
]

const clipColumns = [
  { title: '镜号', dataIndex: 'sort_order', key: 'sort', width: 60 },
  { title: '画面', key: 'scene', ellipsis: true },
  { title: '时长', dataIndex: 'duration_s', key: 'duration', width: 70 },
  { title: '生成方式', key: 'mode', width: 130 },
  { title: '这一档', key: 'ready', width: 120 },
  { title: '上次片段', key: 'job', width: 100 },
]

function goToSettings() {
  router.push('/system/settings')
}

/** 两步的 prompt、产物与轨迹都在运行现场那一页，这里只报状态与耗时。 */
function goToScriptRun() {
  if (scriptRun.value) router.push(`/workflows/runs/${scriptRun.value.id}`)
}

// ---- 生命周期 ----------------------------------------------------------------

onMounted(async () => {
  await Promise.all([load(), loadReadiness(), loadMaterials(), loadCapacity()])
  syncPolling()
})

onUnmounted(() => {
  pollLoop.stop()
  stopClock()
})

// keep-alive：切去别的标签页时停表，回来时按状态恢复。
onDeactivated(() => {
  pollLoop.stop()
  stopClock()
})
onActivated(() => {
  void load(true)
  // 期间可能去过素材库页：那边的增删只有重新取一次清单才看得见。
  void loadMaterials()
  // 也可能去设置页配好了公网基址：「参考上一版」那个勾选框据此解禁。
  void loadCapacity()
  syncPolling()
  syncClock()
})

// 同一枚标签页换个项目 id：keep-alive 复用同一个实例，参数变了必须自己重载，
// 否则屏幕上还是上一个项目的分镜。
watch(
  () => route.params.id,
  (value) => {
    const id = Number(value)
    if (!id || id === projectId.value) return
    projectId.value = id
    shots.value = []
    selected.value = 0
    editorTab.value = 'shot'
    dirtyProject.value = false
    dirtyShots.value = false
    loadError.value = ''
    void loadMaterials()
    void load().then(syncPolling)
  },
)

// 关标签页前提醒未保存的改动。
function guard(event: BeforeUnloadEvent) {
  if (dirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
window.addEventListener('beforeunload', guard)
onUnmounted(() => window.removeEventListener('beforeunload', guard))
</script>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  /* 外壳已经带了顶栏与标签栏，这里跟着父级撑满而不是 100vh。 */
  height: 100%;
  min-height: 0;
  background: var(--paper);
}

.wb-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--hairline);
  flex-wrap: wrap;
}

.wb-head-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  /* basis不能是0：那样这一组永远不换行，窄屏时会被压到比内容还窄，
     标题与meta溢出画到按钮底下。给一个内容下限，放不下就整组换行。 */
  flex: 1 1 340px;
}

.wb-title-input {
  font-size: var(--fs-lg);
  font-weight: 600;
  /* antd 的 Input 默认 width:100%，在弹性行里就按整行算基准：不压住它，标题会把
     状态标签与「平台 · 镜数 · 秒数」挤到换行，右侧按钮每多一枚就更错一点。
     这一行唯一该伸缩的是它，所以给它一个上限，让位留给后面那几个固定的东西。 */
  flex: 1 1 auto;
  width: auto;
  min-width: 160px;
  max-width: 480px;
}

/* 状态标签、meta、未保存提示都是内容定长的：让它们按自身宽度站住，不参与收缩。 */
.wb-head-main :is(.wb-head-meta, .dirty-hint) {
  flex: 0 0 auto;
  white-space: nowrap;
}

.dirty-hint {
  color: var(--warn, #d48806);
  font-size: var(--fs-xs);
}

.wb-alert {
  margin: 8px 16px 0;
}

.wb-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 250px minmax(320px, 1fr) minmax(340px, 1.1fr);
  gap: 12px;
  padding: 12px 16px 16px;
}

.wb-shots,
.wb-editor,
.wb-preview {
  min-height: 0;
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md, 10px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.pane-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  font-size: var(--fs-xs);
  color: var(--text-3);
  border-bottom: 1px solid var(--hairline);
  flex-shrink: 0;
}

.shot-list {
  list-style: none;
  margin: 0;
  padding: 4px;
  overflow: auto;
  flex: 1;
  min-height: 0;
}

.shot-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 8px;
  cursor: pointer;
}

.shot-item.active {
  background: rgba(10, 126, 134, 0.1);
}

.shot-no {
  flex-shrink: 0;
  width: 20px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.shot-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.shot-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.shot-sub {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.shot-ops {
  display: flex;
  flex-shrink: 0;
  opacity: 0.4;
}

.shot-item:hover .shot-ops,
.shot-item.active .shot-ops {
  opacity: 1;
}

.wb-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.wb-tabs :deep(.ant-tabs-content),
.wb-tabs :deep(.ant-tabs-tabpane) {
  height: 100%;
  min-height: 0;
}

.pane-form {
  height: 100%;
  overflow: auto;
  padding: 12px 16px 24px;
}

.form-section + .form-section {
  margin-top: 18px;
  padding-top: 12px;
  border-top: 1px solid var(--hairline);
}

.form-section-title {
  font-size: var(--fs-xs);
  color: var(--text-3);
  margin-bottom: 8px;
}

.pane-form :deep(.ant-form-item) {
  margin-bottom: 12px;
}

.structure-row {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}

.style-hint {
  margin: -6px 0 12px;
}

.cover-meta {
  margin-left: 10px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.cover-mini {
  display: block;
  margin-top: 6px;
  width: 54px;
  height: 96px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--hairline);
}

.preview-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.phone {
  position: relative;
  aspect-ratio: 9 / 16;
  max-height: 56vh;
  margin: 0 auto;
  width: 100%;
  max-width: 320px;
  /* 预览列里还有作业现场与时间轴：内容超高时让列滚动，而不是把竖屏框压扁。 */
  flex-shrink: 0;
  border-radius: 12px;
  overflow: hidden;
  background: #0d1116;
  border: 1px solid var(--hairline);
}

.phone-frame {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.phone-empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: rgba(255, 255, 255, 0.55);
  font-size: var(--fs-xs);
  text-align: center;
  padding: 0 16px;
}

.phone-subtitle {
  position: absolute;
  left: 24px;
  right: 24px;
  bottom: 18px;
  margin: 0;
  text-align: center;
  color: #fff;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.8);
  font-size: 15px;
  line-height: 1.4;
}

/* 片段在放时让开原生控制条，否则屏幕字正好压在播放/进度上。 */
.phone-subtitle--video {
  bottom: 56px;
}

.job-meta {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: var(--fs-xs);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md, 10px);
  padding: 10px 12px;
}

/* 发布区的作业卡片夹在按钮行下面，补一点呼吸。 */
.publish-job {
  margin-top: 10px;
}

.job-kind {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-weight: 600;
  color: var(--text-3);
}

.job-kind-link {
  height: auto;
  padding: 0;
  font-size: var(--fs-xs);
  font-weight: 400;
}

.job-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.job-step {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.job-step :deep(.ant-tag) {
  margin-inline-end: 0;
}

.job-error {
  margin-top: 4px;
}

.job-ops {
  margin-top: 6px;
}

.timeline {
  display: flex;
  gap: 2px;
  min-height: 26px;
}

.timeline-seg {
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  background: rgba(10, 126, 134, 0.14);
  color: var(--text-3);
  font-size: var(--fs-xs);
  cursor: pointer;
  min-width: 16px;
}

.timeline-seg.active {
  background: var(--brand, #0a7e86);
  color: #fff;
}

.timeline-hint {
  margin-top: -6px;
}

.material-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.material-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 172px;
  padding: 2px 2px 2px 2px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--surface-2);
}

.material-chip img,
.material-chip video {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  border-radius: 6px;
  object-fit: cover;
}

.chip-name {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: var(--fs-xs);
}

/* 库里删掉了这条也要留一格：压成虚线灰，是为了说清「这一镜的引用去了哪儿」。 */
.material-chip.is-missing {
  border-style: dashed;
  color: var(--text-3);
}

.chip-close {
  flex: 0 0 auto;
  height: 20px;
  padding: 0 4px;
}

.render-alert {
  margin-bottom: 10px;
}

@media (max-width: 1180px) {
  /* 单列时三块按内容撑高、整页滚动（外壳内容区本来就 overflow:auto）。
     原来三行平分一屏高：预览框被 flex 收缩压成一条线，分镜与表单也只剩
     两百像素的滚动窗，竖屏预览根本看不见。 */
  .workbench {
    height: auto;
  }

  .wb-body {
    grid-template-columns: 1fr;
    grid-auto-rows: min-content;
  }

  .shot-list {
    flex: none;
    max-height: 320px;
  }

  .wb-tabs {
    flex: none;
  }

  .wb-tabs :deep(.ant-tabs-content),
  .wb-tabs :deep(.ant-tabs-tabpane),
  .pane-form {
    height: auto;
  }

  .pane-form,
  .preview-body {
    overflow: visible;
  }

  .preview-body {
    flex: none;
  }
}
</style>
