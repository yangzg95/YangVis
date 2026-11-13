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
      v-else-if="!imageReady"
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
              <span class="shot-sub">{{ shot.duration_s }} 秒 · {{ jobText(shot) }}</span>
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
                title="删除这一镜？它的首帧文件会一并从服务器上删除。"
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
                <a-form-item label="视频提示词（下一轮才用）">
                  <a-textarea
                    v-model:value="current.video_prompt"
                    :rows="2"
                    :maxlength="PROMPT_MAX"
                    placeholder="这一段视频里怎么动起来——一期只渲首帧，先攒着"
                    @change="markShotDirty"
                  />
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
                        视频片段那一轮才用它。默认 9 : 16：「自适应」跟着首帧的比例走，
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
                      <div class="hint">一期只渲首帧，这一档决定图片尺寸。</div>
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
                      <div class="hint">视频片段那一轮才用它。</div>
                    </a-form-item>
                  </a-col>
                  <a-col :span="12">
                    <a-form-item label="帧率">
                      <a-input-number
                        v-model:value="params.fps"
                        :min="1"
                        :max="60"
                        style="width: 100%"
                        @change="dirtyProject = true"
                      />
                    </a-form-item>
                  </a-col>
                </a-row>
                <a-form-item label="水印">
                  <a-switch v-model:checked="params.watermark" @change="dirtyProject = true" />
                  <div class="hint">默认关。</div>
                </a-form-item>
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
            <img
              v-if="frameUrl"
              class="phone-frame"
              :src="frameUrl"
              alt="本镜首帧"
              loading="lazy"
            />
            <div v-else class="phone-empty">
              <LoadingOutlined v-if="currentActive" spin />
              <span>{{ previewHint }}</span>
            </div>
            <p v-if="current?.on_screen_text" class="phone-subtitle">
              {{ current.on_screen_text }}
            </p>
          </div>

          <div v-if="current" class="job-meta">
            <div class="job-row">
              <span class="muted">状态</span>
              <a-tag :color="jobColor(current)">{{ jobText(current) }}</a-tag>
            </div>
            <template v-if="current.image_job">
              <div class="job-row">
                <span class="muted">模型</span><span>{{ current.image_job.model_name || '—' }}</span>
              </div>
              <div class="job-row">
                <span class="muted">用时</span>
                <span>{{
                  current.image_job.elapsed_ms ? fmtElapsed(current.image_job.elapsed_ms) : '—'
                }}</span>
              </div>
              <div class="job-row">
                <span class="muted">大小</span>
                <span>{{
                  current.image_job.file_size ? fmtSize(current.image_job.file_size) : '—'
                }}</span>
              </div>
              <a-alert
                v-if="current.image_job.error_msg"
                type="error"
                show-icon
                class="job-error"
                :message="current.image_job.error_msg"
              />
            </template>
            <a-space :size="8" wrap class="job-ops">
              <a-button
                v-if="currentActive"
                size="small"
                danger
                @click="onCancelJob(current.image_job?.id)"
              >
                取消这一镜
              </a-button>
              <a-button
                v-else
                size="small"
                :disabled="!canRenderOne"
                @click="onRenderOne"
              >
                <VideoCameraOutlined /> {{ frameUrl ? '重做这一镜' : '渲这一镜' }}
              </a-button>
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
            起点由镜头顺序累加：{{ totalDuration }} 秒。一镜一段，不做拼接。
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
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CloseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  LoadingOutlined,
  MinusOutlined,
  PictureOutlined,
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
  type MediaJobStatus,
  type VideoBrief,
  type VideoGenMode,
  type VideoParams,
  type VideoPlatform,
  type VideoProjectDetail,
  type VideoProjectStatus,
  type VideoPublish,
  type VideoShot,
  type VideoShotInput,
  type VideoStyle,
} from '@/api'
import MaterialPicker from '@/components/videos/MaterialPicker.vue'
import { saveTextFile } from '@/utils/download'
import { errorText } from '@/utils/errors'
import { fmtElapsed, fmtSize } from '@/utils/format'
import { createPollLoop } from '@/utils/polling'
import { buildAss, buildSrt, subtitleCues } from '@/utils/videoSubtitles'

// 短视频工作台：左边分镜（顺序即成片顺序）、中间两套提示词与全片参数、右边竖屏预览。
//
// 两阶段的进度在这里各看各的：脚本阶段是项目状态（从认领本项目的运行翻译过来），
// 渲染阶段是这一镜最近那条 media_job 的状态。所以详情接口轮询一次就把两件事都说清了。
//
// 一期只渲首帧：video_prompt 照旧攒在镜头行上，界面上不摆一个永远为空的「视频」格子。
// 字幕也不烧进画面——预览按屏幕文字叠加，导出的是旁边的 SRT / ASS 文件。

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

const GEN_MODE_OPTIONS: { value: VideoGenMode; label: string }[] = [
  { value: 'first_frame', label: '首帧生视频（默认）' },
  { value: 't2v', label: '文生视频' },
  { value: 'first_last_frame', label: '首尾帧生视频' },
  { value: 'reference', label: '参考图生视频' },
]
const GEN_MODE_HINTS: Record<VideoGenMode, string> = {
  first_frame: '先出这一镜的首帧，再由首帧生成视频片段。一期的首帧渲染就是它的第一步。',
  t2v: '直接从提示词生成视频，不需要首帧——画面一致性强依赖参考图时不建议选它。',
  first_last_frame: '给定首尾两帧，中间由模型补：动作跨度大时比首帧流稳。',
  reference: '用参考图当主体，模型按参考图里的形象生成：人物一致性靠这一档。',
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
  fps: 24,
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

const selected = ref(0)
const editorTab = ref<'shot' | 'film'>('shot')
const dirtyProject = ref(false)
const dirtyShots = ref(false)
const saving = ref(false)
const loadError = ref('')
const imageReady = ref(true)

const pickerOpen = ref(false)
const renderModal = ref(false)
const picked = ref<number[]>([])
const capacity = ref<MediaCapacityOut | null>(null)

const current = computed(() => shots.value[selected.value])
const dirty = computed(() => dirtyProject.value || dirtyShots.value)
const generating = computed(() => state.value === 'analyzing')
const totalDuration = computed(() =>
  shots.value.reduce((sum, shot) => sum + (shot.duration_s || 0), 0),
)
const currentActive = computed(
  () =>
    Boolean(current.value?.image_job?.status) &&
    ACTIVE_JOB_STATUS.includes(current.value!.image_job!.status),
)
const frameUrl = computed(() =>
  current.value?.image_job?.status === 'succeeded' ? current.value.image_job.url : '',
)
const rendering = computed(() =>
  shots.value.some((shot) => shot.image_job && ACTIVE_JOB_STATUS.includes(shot.image_job.status)),
)
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
    !currentActive.value,
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
  } catch {
    imageReady.value = true
  }
}

// 脚本在跑、或有镜头作业在途时轮询；两者都落定就停表，不留空转定时器。
const pollLoop = createPollLoop(
  async () => {
    const before = lastJobStatus()
    await load(true)
    announceJobs(before)
  },
  { shouldContinue: () => generating.value || rendering.value },
)

function syncPolling() {
  if (generating.value || rendering.value) pollLoop.start()
  else pollLoop.stop()
}

// 「刚出图」和「刚失败」都值得说一句：几十秒到几分钟的过程，用户不会一直盯着。
function lastJobStatus(): Map<number, MediaJobStatus | undefined> {
  const map = new Map<number, MediaJobStatus | undefined>()
  for (const shot of shots.value) {
    if (shot.id) map.set(shot.id, shot.image_job?.status)
  }
  return map
}

function announceJobs(before: Map<number, MediaJobStatus | undefined>) {
  if (!before.size) return
  let done = 0
  let failed = 0
  for (const shot of shots.value) {
    const previous = before.get(shot.id)
    const now = shot.image_job?.status
    if (!previous || !ACTIVE_JOB_STATUS.includes(previous) || !now) continue
    if (now === 'succeeded') done += 1
    else if (now === 'failed' || now === 'expired') failed += 1
  }
  if (done) message.success(`${done} 镜首帧已出图`)
  if (failed) message.error(`${failed} 镜渲染失败，预览区有原因`)
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
    params: { ...params, fps: Number(params.fps) || 24 },
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

function jobStatusText(status: MediaJobStatus | string): string {
  return (
    {
      queued: '排队中',
      running: '出图中',
      downloading: '转存中',
      succeeded: '已出图',
      failed: '失败',
      cancelled: '已取消',
      expired: '超时未完成',
    }[status] || status
  )
}

function jobColor(shot: VideoShot): string {
  const status = shot.image_job?.status
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

function jobText(shot: VideoShot): string {
  return shot.image_job ? jobStatusText(shot.image_job.status) : '还没渲'
}

const renderColumns = [
  { title: '镜号', dataIndex: 'sort_order', key: 'sort', width: 60 },
  { title: '画面', key: 'scene', ellipsis: true },
  { title: '时长', dataIndex: 'duration_s', key: 'duration', width: 70 },
  { title: '首帧提示词', key: 'prompt', width: 110 },
  { title: '上次渲染', key: 'job', width: 100 },
]

function goToSettings() {
  router.push('/system/settings')
}

// ---- 生命周期 ----------------------------------------------------------------

onMounted(async () => {
  await Promise.all([load(), loadReadiness(), loadMaterials()])
  syncPolling()
})

onUnmounted(() => pollLoop.stop())

// keep-alive：切去别的标签页时停表，回来时按状态恢复。
onDeactivated(() => pollLoop.stop())
onActivated(() => {
  void load(true)
  // 期间可能去过素材库页：那边的增删只有重新取一次清单才看得见。
  void loadMaterials()
  syncPolling()
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
  flex: 1;
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

.job-meta {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: var(--fs-xs);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md, 10px);
  padding: 10px 12px;
}

.job-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
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
  .wb-body {
    grid-template-columns: 1fr;
  }
}
</style>
