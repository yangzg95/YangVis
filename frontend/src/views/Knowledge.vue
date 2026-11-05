<template>
  <div class="page-card">
    <a-alert
      v-if="!loading && !embeddingReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的向量模型"
      description="知识库需要向量模型将文档转成可检索的向量。请先添加一个用途为「向量」的模型并通过连通性测试。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="goToSettings">去配置</a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="indexState.model_mismatch"
      type="error"
      show-icon
      class="gate-alert"
      message="索引与当前向量模型不一致"
      :description="`知识库索引是用另一个向量模型建立的，检索结果不可靠。需要用当前模型重建全部索引（共 ${indexState.doc_count} 篇文档 / ${indexState.chunk_count} 个分片），期间会产生向量调用费用。`"
    >
      <template #action>
        <a-button size="small" danger :loading="rebuilding" @click="onRebuild">重建索引</a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="indexState.status === 'error'"
      type="error"
      show-icon
      class="gate-alert"
      message="索引状态异常"
      :description="indexState.error_msg || '上次索引操作失败，可尝试重建。'"
    >
      <template #action>
        <a-button size="small" :loading="rebuilding" @click="onRebuild">重建索引</a-button>
      </template>
    </a-alert>

    <div class="toolbar toolbar--between toolbar--snug">
      <a-space size="small" wrap class="kb-stats">
        <span>项目</span>
        <a-select
          :value="activeProjectId"
          style="width: 200px"
          placeholder="请选择项目"
          :options="projectOptions"
          :loading="loadingProjects"
          @change="selectProject"
        />
        <a-button @click="openProjects">管理项目</a-button>
        <a-divider type="vertical" />
        <a-tag :color="indexStatusColor">{{ indexStatusText }}</a-tag>
        <span class="muted">{{ indexState.doc_count }} 篇文档 · {{ indexState.chunk_count }} 个分片</span>
        <span v-if="indexState.vector_size" class="muted">· {{ indexState.vector_size }} 维</span>
      </a-space>
      <a-space>
        <a-tooltip :title="activeProjectId ? '' : '请先选择项目'">
          <a-button :disabled="!embeddingReady || !activeProjectId" @click="openSearch">
            检索预览
          </a-button>
        </a-tooltip>
        <a-popconfirm
          title="将删除并重建全部索引，期间检索不可用，且会产生向量调用费用。确认继续？"
          ok-text="确认重建"
          cancel-text="取消"
          @confirm="onRebuild"
        >
          <a-button :disabled="!embeddingReady || !indexState.doc_count" :loading="rebuilding">
            重建索引
          </a-button>
        </a-popconfirm>
      </a-space>
    </div>

    <a-row :gutter="16">
      <a-col :xs="24" :md="10" :lg="8">
        <a-card title="知识类型" size="small">
          <template #extra>
            <a-space size="small">
              <a-tooltip :title="repoButtonReason">
                <a-button
                  size="small"
                  :disabled="!embeddingReady || !activeProjectId"
                  @click="openCreateRepo"
                >
                  <BranchesOutlined /> 接入仓库
                </a-button>
              </a-tooltip>
              <a-tooltip :title="activeProjectId ? '' : '请先选择项目'">
                <a-button
                  type="primary"
                  size="small"
                  :disabled="!activeProjectId"
                  @click="openCreateType"
                >
                  <PlusOutlined /> 新建类型
                </a-button>
              </a-tooltip>
            </a-space>
          </template>
          <!-- 首屏骨架占位；已有内容时刷新直接留旧列表。 -->
          <a-skeleton v-if="loadingTypes && !types.length" active :paragraph="{ rows: 4 }" />
          <template v-else>
            <a-empty v-if="!activeProjectId" description="请先选择一个项目" />
            <a-empty v-else-if="!types.length" description="该项目下暂无知识类型" />
            <a-list
              v-else
              :data-source="typeRows"
              size="small"
              :pagination="typeRows.length > 8 ? { pageSize: 8, size: 'small' } : false"
            >
              <template #renderItem="{ item }">
                <a-list-item
                  :class="['type-item', 'list-row', { active: item.id === activeTypeId }]"
                  @click="selectType(item.id)"
                >
                  <a-list-item-meta>
                    <template #title>
                      <span>{{ item.name }}</span>
                      <a-tag v-if="item.repo" color="purple" style="margin-left: 8px">
                        仓库
                      </a-tag>
                      <a-tag color="blue" style="margin-left: 4px">
                        {{ item.document_count }}
                      </a-tag>
                    </template>
                    <template #description>
                      <span v-if="item.repo" class="muted repo-line">
                        <BranchesOutlined />
                        {{ repoBranchLabel(item.repo) }}
                        <a-tag
                          :color="repoStatusColor(item.repo.status)"
                          style="margin-left: 4px"
                        >
                          {{ repoStatusText(item.repo.status) }}
                        </a-tag>
                      </span>
                      <span v-else class="muted">{{ item.description || '—' }}</span>
                    </template>
                  </a-list-item-meta>
                  <!-- 仓库类型的编辑/移除收进右侧仓库信息条，避免两套删除口径。 -->
                  <a-space v-if="!item.repo">
                    <a-button size="small" @click.stop="openEditType(item)">编辑</a-button>
                    <a-popconfirm
                      title="确认删除该知识类型？其下文档与索引也会被删除。"
                      @confirm="onDeleteType(item.id)"
                    >
                      <a-button size="small" danger @click.stop>删除</a-button>
                    </a-popconfirm>
                  </a-space>
                </a-list-item>
              </template>
            </a-list>
          </template>
        </a-card>
      </a-col>

      <a-col :xs="24" :md="14" :lg="16">
        <a-card :title="activeType ? `文档 · ${activeType.name}` : '文档'" size="small">
          <template #extra>
            <a-space>
              <a-button size="small" :disabled="!activeType" @click="refreshDocuments">
                <ReloadOutlined /> 刷新
              </a-button>
              <template v-if="activeRepo">
                <!-- 仓库类型内容只能来自同步：不放任何上传入口。 -->
                <a-button
                  size="small"
                  type="primary"
                  :disabled="!embeddingReady"
                  :loading="repoSyncing"
                  @click="onSyncRepo(activeRepo)"
                >
                  <BranchesOutlined /> 立即同步
                </a-button>
              </template>
              <template v-else>
                <!-- 直接禁用，而不是让人点了再失败：一个收下文件、之后才
                     报错的上传按钮是最糟糕的做法。 -->
                <a-tooltip :title="uploadDisabledReason">
                  <a-upload
                    :show-upload-list="false"
                    :before-upload="handleBeforeUpload"
                    :accept="acceptAttr"
                    :disabled="!canUpload || uploading"
                  >
                    <a-button type="primary" :disabled="!canUpload || uploading">
                      <UploadOutlined /> 上传文档
                    </a-button>
                  </a-upload>
                </a-tooltip>
                <a-tooltip :title="uploadDisabledReason">
                  <!-- 单独一个 input：同一个文件选择器没办法既选文件又选目录，
                       所以上传文件夹这种情况只能自己配一个。 -->
                  <a-upload
                    :show-upload-list="false"
                    :before-upload="handleBeforeFolder"
                    directory
                    multiple
                    :disabled="!canUpload || uploading"
                  >
                    <a-button :disabled="!canUpload || uploading" :loading="uploading">
                      <FolderOpenOutlined /> 上传文件夹
                    </a-button>
                  </a-upload>
                </a-tooltip>
              </template>
            </a-space>
          </template>

          <!-- 仓库信息条：仓库类型的「操作中枢」，同步状态/计数/指纹都在这里。 -->
          <div v-if="activeRepo" class="repo-bar">
            <div class="repo-bar-row">
              <BranchesOutlined class="repo-branch-icon" />
              <a-tooltip :title="activeRepo.remote_url">
                <span class="repo-url">{{ activeRepo.remote_url }}</span>
              </a-tooltip>
              <a-tag :color="repoAuthMeta(activeRepo.auth_type).color">
                {{ repoAuthMeta(activeRepo.auth_type).label }}
              </a-tag>
              <a-tag color="purple">{{ repoBranchLabel(activeRepo) }}</a-tag>
              <a-tag :color="repoStatusColor(activeRepo.status)">
                {{ repoStatusText(activeRepo.status) }}
              </a-tag>
              <a-tooltip v-if="activeRepo.host_fingerprint" :title="activeRepo.host_fingerprint">
                <a-tag color="gold">指纹已记录</a-tag>
              </a-tooltip>
              <span class="repo-spacer" />
              <a-space size="small">
                <a-button size="small" @click="openEditRepo(activeRepo)">编辑</a-button>
                <a-popconfirm
                  title="移除仓库将删除其知识类型、全部仓库文件与向量，且不可恢复。确认继续？"
                  ok-text="移除"
                  cancel-text="取消"
                  @confirm="onRemoveRepo(activeRepo)"
                >
                  <a-button size="small" danger>移除仓库</a-button>
                </a-popconfirm>
              </a-space>
            </div>
            <div class="repo-bar-row muted">
              <template v-if="activeRepo.last_sync_result">
                <span>
                  上次同步
                  <b class="repo-add">+{{ activeRepo.last_sync_result.added }}</b>
                  / <b class="repo-change">~{{ activeRepo.last_sync_result.changed }}</b>
                  / <b class="repo-remove">-{{ activeRepo.last_sync_result.removed }}</b>
                  /
                  <a-popover
                    v-if="skipBreakdown(activeRepo.last_sync_result).length"
                    trigger="click"
                    placement="topLeft"
                    title="跳过明细（原因 · 次数 · 样本路径）"
                  >
                    <template #content>
                      <div class="skip-popover">
                        <div
                          v-for="row in skipBreakdown(activeRepo.last_sync_result)"
                          :key="row.reason"
                          class="skip-row"
                        >
                          <div class="skip-row-head">
                            <span>{{ row.label }}</span>
                            <b>{{ row.count }}</b>
                          </div>
                          <div
                            v-for="p in row.samples"
                            :key="p"
                            class="skip-sample"
                            :title="p"
                          >{{ p }}</div>
                        </div>
                      </div>
                    </template>
                    <a class="skip-link">跳过 {{ activeRepo.last_sync_result.skipped }}</a>
                  </a-popover>
                  <span v-else>跳过 {{ activeRepo.last_sync_result.skipped }}</span>
                </span>
                <span class="repo-dot">·</span>
                <span>{{ activeRepo.last_sync_result.indexed_chunks }} 个分片</span>
                <span class="repo-dot">·</span>
                <span>耗时 {{ fmtElapsed(activeRepo.last_sync_result.duration_ms) }}</span>
                <a-tooltip
                  v-if="activeRepo.last_sync_result.worktree_warned"
                  :title="`本地工作区体积 ${fmtSize(activeRepo.last_sync_result.worktree_bytes || 0)}，超过告警阈值；请检查是否误纳入大目录并在排除规则中屏蔽`"
                >
                  <a-tag color="orange" class="repo-warn-tag">工作区体积偏大</a-tag>
                </a-tooltip>
              </template>
              <span v-else>尚未完成过同步</span>
              <span class="repo-dot">·</span>
              <span>{{ fmtDateTime(activeRepo.last_sync_at, '未同步') }}</span>
              <span v-if="activeRepo.status === 'syncing'" class="repo-live">同步进行中…</span>
            </div>
            <a-alert
              v-if="activeRepo.error_msg"
              type="error"
              show-icon
              class="repo-error"
              :message="activeRepo.error_msg"
            >
              <template v-if="isHostKeyError(activeRepo.error_msg)" #action>
                <a-popconfirm
                  title="将重新连接远端并覆盖记录新的主机指纹。请确认该变更来自你的 Git 服务器（例如服务器迁移），而不是网络劫持。确认更新？"
                  ok-text="确认变更指纹"
                  cancel-text="取消"
                  @confirm="onRetrust(activeRepo)"
                >
                  <a-button size="small" danger :loading="retrustingId === activeRepo.id">
                    确认变更指纹
                  </a-button>
                </a-popconfirm>
              </template>
            </a-alert>
            <div class="hint repo-hint">
              文件由仓库同步物化，不能上传或单独删除；可对单篇失败文件做「重新索引」。
              排除规则在「编辑」中维护（如 {{ '**/test/**' }}）。
            </div>
          </div>

          <div v-else class="hint upload-hint">
            仅支持 {{ acceptLabel }}，单个文件不超过 10 MB。上传后在后台切片与向量化，状态会自动刷新。
            上传文件夹会递归读取子目录，其余格式自动跳过。
            <span v-if="batch.total" class="batch-progress">
              正在上传 {{ batch.done }} / {{ batch.total }}…
            </span>
          </div>

          <a-skeleton v-if="loadingDocs && !documents.length" active :paragraph="{ rows: 5 }" />
          <template v-else>
            <a-empty v-if="!activeType" description="请先选择一个知识类型" />
            <a-empty v-else-if="!documents.length" description="该类型下暂无文档" />
            <template v-else>
              <!-- 批量操作条只在有勾选时占位，平时不占纵向空间。
                   仓库类型不挂勾选列，自然也没有批量条（文件不能单删）。 -->
              <div v-if="!isRepoType && selectedDocIds.length" class="doc-batch-bar">
                <span>已选 {{ selectedDocIds.length }} 篇</span>
                <a-button
                  size="small"
                  :loading="batchOperating"
                  :disabled="!embeddingReady"
                  @click="onBatchReindex"
                >
                  重建索引
                </a-button>
                <a-button size="small" danger :loading="batchOperating" :disabled="batchOperating" @click="onBatchDelete">
                  删除
                </a-button>
                <a-button size="small" type="text" :disabled="batchOperating" @click="selectedDocIds = []">取消</a-button>
              </div>
              <a-table
                :data-source="documents"
                :columns="docColumns"
                :row-selection="isRepoType ? null : docRowSelection"
                row-key="id"
                size="small"
                :pagination="docPagination"
                :scroll="{ x: 'max-content' }"
                @change="onDocPageChange"
              >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'name'">
                  <a
                    v-if="record.source === 'repo'"
                    class="repo-file-path repo-file-link"
                    :title="record.name"
                    @click="openPreview(record.id)"
                  >{{ record.name }}</a>
                  <span v-else>{{ record.name }}</span>
                </template>

                <template v-else-if="column.key === 'size'">
                  {{ fmtSize(record.size) }}
                </template>

                <template v-else-if="column.key === 'status'">
                  <a-tag v-if="batchIds.includes(record.id)" color="processing">处理中</a-tag>
                  <a-tooltip v-else-if="record.error_msg" :title="record.error_msg">
                    <a-tag color="error">失败</a-tag>
                  </a-tooltip>
                  <a-tag v-else :color="statusMeta(record.status).color">
                    {{ statusMeta(record.status).label }}
                    <template v-if="record.status === 'ready' && record.chunk_count">
                      · {{ record.chunk_count }} 片
                    </template>
                  </a-tag>
                </template>

                <template v-else-if="column.key === 'action'">
                  <a-space>
                    <a-button
                      size="small"
                      :disabled="!embeddingReady || record.status === 'indexing' || batchOperating"
                      @click="onReindex(record.id)"
                    >
                      重新索引
                    </a-button>
                    <!-- 仓库文件不提供单篇删除：删了下次同步还会回来，回收只能走移除仓库。 -->
                    <a-popconfirm v-if="record.source !== 'repo'" title="确认删除该文档？" @confirm="onDeleteDoc(record.id)">
                      <a-button size="small" danger :disabled="batchOperating">删除</a-button>
                    </a-popconfirm>
                  </a-space>
                </template>
              </template>
              </a-table>
            </template>
          </template>
        </a-card>
      </a-col>
    </a-row>

    <a-modal
      v-model:open="typeModal.open"
      :title="typeModal.editing ? '编辑知识类型' : '新建知识类型'"
      @ok="submitType"
      :confirm-loading="typeModal.loading"
      ok-text="保存"
      cancel-text="取消"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="typeModal.form.name" placeholder="例如：产品文档" />
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea
            v-model:value="typeModal.form.description"
            :rows="3"
            placeholder="可选，描述该知识类型的用途"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="projectModal.open"
      title="项目管理"
      :footer="null"
      width="640px"
    >
      <div class="hint" style="margin-bottom: 12px">
        项目是检索的边界：对话时先选项目，只会读到该项目下的文档。
      </div>
      <a-form layout="inline" class="project-form">
        <a-form-item>
          <a-input v-model:value="projectModal.form.name" placeholder="项目名称" style="width: 180px" />
        </a-form-item>
        <a-form-item>
          <a-input
            v-model:value="projectModal.form.description"
            placeholder="描述（可选）"
            style="width: 220px"
          />
        </a-form-item>
        <a-form-item>
          <a-space>
            <a-button type="primary" :loading="projectModal.loading" @click="submitProject">
              {{ projectModal.editingId ? '保存' : '新建' }}
            </a-button>
            <a-button v-if="projectModal.editingId" @click="resetProjectForm">取消编辑</a-button>
          </a-space>
        </a-form-item>
      </a-form>

      <a-empty v-if="!projects.length" description="还没有项目" />
      <a-list v-else :data-source="projects" size="small">
        <template #renderItem="{ item }">
          <a-list-item>
            <a-list-item-meta>
              <template #title>
                <span>{{ item.name }}</span>
                <a-tag color="blue" style="margin-left: 8px">
                  {{ item.type_count }} 类型 · {{ item.document_count }} 文档
                </a-tag>
              </template>
              <template #description>
                <span class="muted">{{ item.description || '—' }}</span>
              </template>
            </a-list-item-meta>
            <a-space>
              <a-button size="small" @click="editProject(item)">编辑</a-button>
              <a-popconfirm
                title="删除项目会连同其下的知识类型、文档与索引一起删除，确认继续？"
                ok-text="确认删除"
                cancel-text="取消"
                @confirm="onDeleteProject(item.id)"
              >
                <a-button size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </a-list-item>
        </template>
      </a-list>
    </a-modal>

    <a-modal
      v-model:open="searchModal.open"
      title="检索预览"
      :footer="null"
      width="720px"
    >
      <div class="hint" style="margin-bottom: 12px">
        在「{{ activeProject?.name || '—' }}」范围内检索。这里返回的分片，就是对话时模型会读到的上下文。
      </div>
      <a-input-search
        v-model:value="searchModal.query"
        placeholder="输入一个问题，看看会命中哪些内容"
        enter-button="检索"
        :loading="searchModal.loading"
        @search="runSearch"
      />
      <div v-if="searchModal.searched" class="search-results">
        <a-empty
          v-if="!searchModal.hits.length"
          description="没有命中任何内容。相关度低于阈值的分片会被丢弃，避免用无关内容拼出看似合理的错误回答。"
        />
        <a-list v-else :data-source="searchModal.hits" size="small">
          <template #renderItem="{ item, index }">
            <a-list-item>
              <a-list-item-meta>
                <template #title>
                  <a-tag>[{{ index + 1 }}]</a-tag>
                  <a-tag v-if="item.source === 'repo'" color="geekblue">代码</a-tag>
                  <a
                    v-if="item.source === 'repo'"
                    class="repo-file-path repo-file-link"
                    :title="`查看 ${item.filename} 原文（第 ${item.line_start ?? 1} 行）`"
                    @click="openPreview(item.doc_id, item.line_start ?? 1)"
                  >{{ hitLocation(item) }}</a>
                  <span v-else :class="{ 'repo-file-path': item.source === 'repo' }">
                    {{ hitLocation(item) }}
                  </span>
                  <span v-if="item.symbol" class="muted"> · {{ item.symbol }}</span>
                  <span class="muted"> · 第 {{ item.seq + 1 }} 片 · 相关度 {{ item.score.toFixed(3) }}</span>
                </template>
                <template #description>
                  <div class="hit-content" :class="{ 'hit-content--code': item.source === 'repo' }">
                    {{ item.content }}
                  </div>
                </template>
              </a-list-item-meta>
            </a-list-item>
          </template>
        </a-list>
      </div>
    </a-modal>

    <!-- 仓库源文件只读预览：带行号，不做任何编辑/下载入口。 -->
    <a-modal
      v-model:open="previewOpen"
      :title="preview?.name ?? '源文件预览'"
      :footer="null"
      width="920px"
      class="source-preview-modal"
      :destroy-on-close="true"
    >
      <a-spin :spinning="previewLoading">
        <div v-if="preview" class="src-meta">
          <a-tag color="geekblue">代码</a-tag>
          <a-tag v-if="preview.language">{{ preview.language }}</a-tag>
          <span class="muted">{{ preview.line_count }} 行 · {{ fmtSize(preview.size) }} · 只读</span>
        </div>
        <div v-if="preview" class="src-view">
          <div
            v-for="(line, i) in previewLines"
            :id="`src-line-${i + 1}`"
            :key="i"
            class="src-line"
            :class="{ 'src-line--hit': previewAnchor === i + 1 }"
          >
            <span class="src-gutter">{{ i + 1 }}</span>
            <code class="src-code">{{ line === '' ? ' ' : line }}</code>
          </div>
        </div>
      </a-spin>
    </a-modal>

    <!-- 接入 / 编辑仓库。remote_url 不可改（改地址等于换仓库，请移除后重新接入）。 -->
    <a-modal
      v-model:open="repoModal.open"
      :title="repoModal.editing ? `编辑仓库 · ${repoModal.form.name}` : '接入代码仓库'"
      :confirm-loading="repoModal.submitting"
      :ok-text="repoModal.editing ? '保存' : '接入并首次同步'"
      cancel-text="取消"
      width="660px"
      :ok-button-props="{ disabled: repoModal.testing || repoModal.generating }"
      @ok="submitRepo"
    >
      <a-form layout="vertical">
        <a-form-item label="展示名称" :required="!repoModal.editing">
          <a-input
            v-model:value="repoModal.form.name"
            :placeholder="repoModal.editing ? '' : '可选，默认取地址末段；将自动创建同名知识类型'"
            :maxlength="128"
          />
        </a-form-item>

        <a-form-item v-if="!repoModal.editing" label="Git 地址" required>
          <a-input
            v-model:value="repoModal.form.remote_url"
            placeholder="git@host:group/repo.git、ssh://git@host:2222/group/repo.git，或 http(s)://group/repo.git"
            @change="onRemoteUrlChange"
          />
        </a-form-item>
        <a-form-item v-else label="Git 地址">
          <a-input :value="repoModal.form.remote_url" disabled />
        </a-form-item>

        <a-form-item label="跟踪分支">
          <a-input
            v-model:value="repoModal.form.branch"
            placeholder="留空：自动探测并跟随远端默认分支"
            :maxlength="255"
            allow-clear
          />
        </a-form-item>

        <a-form-item label="认证方式">
          <a-radio-group
            v-model:value="repoModal.form.auth_type"
            button-style="solid"
            @change="onAuthTypeChange"
          >
            <a-radio-button v-if="repoFormProtocol === 'ssh'" value="ssh_key">
              SSH Deploy Key
            </a-radio-button>
            <template v-else>
              <a-radio-button value="none">匿名 http(s)</a-radio-button>
              <a-radio-button value="http_token">HTTPS 令牌</a-radio-button>
            </template>
          </a-radio-group>
          <div class="repo-form-hint">认证方式由地址协议决定；编辑时地址不可改。</div>
        </a-form-item>

        <template v-if="repoModal.form.auth_type === 'http_token'">
          <a-form-item
            label="个人访问令牌"
            :required="!repoModal.editing"
            :extra="repoModal.editing
              ? '已保存的令牌留空表示不更换；切到「匿名 http(s)」保存即清除令牌。'
              : '各平台的 Personal Access Token；加密存储，提交后任何接口都无法回读。'"
          >
            <a-input-password
              v-model:value="repoModal.form.http_token"
              spellcheck="false"
              autocomplete="new-password"
              placeholder="粘贴个人访问令牌（PAT）"
              @change="invalidateProbe"
            />
          </a-form-item>
          <a-form-item
            label="令牌用户名（可选）"
            extra="多数平台任意非空即可，默认 x-access-token；Gitee 等个别平台要求填真实用户名。"
          >
            <a-input
              v-model:value="repoModal.form.http_username"
              :maxlength="128"
              placeholder="x-access-token"
              @change="invalidateProbe"
            />
          </a-form-item>
        </template>

        <template v-if="repoModal.form.auth_type === 'ssh_key'">
          <a-form-item
            v-if="!repoModal.editing"
            label="Deploy 私钥"
            :required="!repoModal.editing"
            extra='OpenSSH 格式的 ed25519 无口令私钥（-----BEGIN OPENSSH PRIVATE KEY-----）。私钥加密存储，之后任何接口都无法回读。'
          >
            <div class="repo-key-actions">
              <a-button size="small" :loading="repoModal.generating" @click="generateKeypair">
                <KeyOutlined /> 生成新密钥对
              </a-button>
              <a-checkbox v-model:checked="repoModal.showPrivate">显示私钥</a-checkbox>
            </div>
            <a-textarea
              v-model:value="repoModal.form.private_key"
              class="repo-key-input"
              :class="{ 'repo-key-hidden': !repoModal.showPrivate }"
              :rows="5"
              spellcheck="false"
              placeholder="粘贴现有私钥，或点上方按钮生成一对新的"
              @change="invalidateProbe"
            />
          </a-form-item>
          <a-form-item
            v-else
            label="更换 Deploy 私钥"
            extra="留空表示保持原私钥不变；粘贴新私钥（ed25519，无口令）则整体替换。"
          >
            <a-textarea
              v-model:value="repoModal.form.private_key"
              class="repo-key-input"
              :class="{ 'repo-key-hidden': !repoModal.showPrivate }"
              :rows="3"
              spellcheck="false"
              placeholder="留空不更换"
            />
            <div class="repo-key-actions">
              <a-checkbox v-model:checked="repoModal.showPrivate">显示私钥</a-checkbox>
            </div>
          </a-form-item>

          <!-- 公钥不是秘密：生成时即时展示，已接入仓库也可随时回来复制。 -->
          <a-form-item v-if="editingPublicKey" :label="repoModal.editing ? '当前公钥' : '公钥（登记到 Git 服务的 Deploy Keys）'">
            <a-textarea :value="editingPublicKey" :rows="3" readonly class="repo-public-key" />
            <a-button size="small" style="margin-top: 4px" @click="copyText(editingPublicKey)">
              <CopyOutlined /> 复制公钥
            </a-button>
          </a-form-item>

          <!-- 接入时显式钉死指纹：把 TOFU 的首次信任换成先验证后记录。 -->
          <a-collapse v-if="!repoModal.editing" ghost class="repo-advanced">
            <a-collapse-panel key="pin-fingerprint" header="高级：钉死主机指纹（可选）">
              <a-input
                v-model:value="repoModal.form.expected_fingerprint"
                placeholder="SHA256:...（留空则首次连接自动记录 TOFU）"
                spellcheck="false"
                :maxlength="255"
                allow-clear
                @change="invalidateProbe"
              />
              <div class="repo-form-hint">
                从运维渠道取得服务器主机指纹（ssh-keygen -lf 公钥文件）。填写后「测试连接」会严格比对，
                指纹不一致直接拒绝，防止首次连接即被劫持。
              </div>
            </a-collapse-panel>
          </a-collapse>
        </template>

        <a-form-item
          v-if="!repoModal.editing"
          label="连接探测"
          extra="建议先测试：后端会对远端做一次只读探测，并首次记录主机指纹（TOFU）；跳过也可以，首次同步时现场记录。"
        >
          <a-space>
            <a-button :loading="repoModal.testing" @click="testConnection">测试连接</a-button>
            <span v-if="repoModal.probe" class="repo-probe-ok">
              已连接 {{ repoModal.probe.host }} ·
              {{ repoModal.probe.default_branch || '默认分支' }}@{{ shortSha(repoModal.probe.head_sha) }}
              <a-tag v-if="repoModal.probe.host_fingerprint" color="gold" style="margin-left: 6px">
                指纹 {{ repoModal.probe.host_fingerprint.slice(0, 23) }}…
              </a-tag>
              <a-tag
                v-if="repoModal.form.expected_fingerprint.trim() && repoModal.probe.host_fingerprint"
                color="success"
                style="margin-left: 6px"
              >
                指纹匹配
              </a-tag>
            </span>
          </a-space>
          <div v-if="repoModal.probeError" class="repo-form-error">{{ repoModal.probeError }}</div>
        </a-form-item>

        <a-form-item
          label="排除规则"
          extra="每行一条 glob，与内置排除（node_modules、dist、build、锁文件、*.min.js 等）取并集。"
        >
          <a-textarea
            v-model:value="repoModal.form.exclude_globs"
            :rows="3"
            spellcheck="false"
            :placeholder="GLOBS_PLACEHOLDER"
          />
        </a-form-item>
      </a-form>

      <a-alert
        v-if="repoModal.submitError"
        type="error"
        show-icon
        :message="repoModal.submitError"
        style="margin-bottom: 10px"
      />
      <a-alert
        v-if="!repoModal.editing"
        type="info"
        show-icon
        message="提交后立即在后台开始首次同步：受跟踪的源码文件会被切片并调用向量模型，产生费用。"
      />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { message, Modal, type UploadProps } from 'ant-design-vue'
import {
  BranchesOutlined,
  CopyOutlined,
  FolderOpenOutlined,
  KeyOutlined,
  PlusOutlined,
  ReloadOutlined,
  UploadOutlined,
} from '@ant-design/icons-vue'
import { useRouter } from 'vue-router'
import { errorText } from '@/utils/errors'
import { fmtDateTime, fmtElapsed, fmtSize } from '@/utils/format'
import { createPollLoop } from '@/utils/polling'
import {
  knowledgeApi,
  repoApi,
  settingsApi,
  SUPPORTED_UPLOAD_EXTENSIONS,
  type DocumentContent,
  type DocumentStatus,
  type IndexState,
  type KbProject,
  type KnowledgeDocument,
  type KnowledgeType,
  type RepoItem,
  type RepoSyncResult,
  type RepoTestConnectionResult,
  type RepoUpdatePayload,
  type SearchHit,
} from '@/api'

const router = useRouter()

const STATUS_META: Record<DocumentStatus, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'default' },
  indexing: { label: '索引中', color: 'processing' },
  ready: { label: '就绪', color: 'success' },
  error: { label: '失败', color: 'error' },
}

const INDEX_STATUS_META: Record<string, { label: string; color: string }> = {
  uninitialized: { label: '未建立索引', color: 'default' },
  indexing: { label: '索引中', color: 'processing' },
  ready: { label: '索引就绪', color: 'success' },
  rebuilding: { label: '重建中', color: 'processing' },
  error: { label: '索引异常', color: 'error' },
}

function statusMeta(status: DocumentStatus) {
  return STATUS_META[status] ?? { label: status, color: 'default' }
}

const acceptAttr = SUPPORTED_UPLOAD_EXTENSIONS.join(',')
const acceptLabel = SUPPORTED_UPLOAD_EXTENSIONS.join(' / ')
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024

const projects = ref<KbProject[]>([])
const activeProjectId = ref<number | null>(null)
const types = ref<KnowledgeType[]>([])
const documents = ref<KnowledgeDocument[]>([])
const activeTypeId = ref<number | null>(null)
// 文档表格走服务端分页：页码/总数都以后端为准，不再全量拉回来前端切。
const docPage = ref(1)
const docPageSize = 8
const docTotal = ref(0)
const loading = ref(true)
const loadingProjects = ref(false)
const loadingTypes = ref(false)
const loadingDocs = ref(false)
const uploading = ref(false)
const rebuilding = ref(false)
const embeddingReady = ref(false)

const indexState = ref<IndexState>({
  status: 'uninitialized',
  collection_name: null,
  vector_size: 0,
  doc_count: 0,
  chunk_count: 0,
  error_msg: null,
  model_mismatch: false,
  updated_at: null,
})

const docColumns = [
  { title: '名称', key: 'name', dataIndex: 'name', ellipsis: true },
  { title: '大小', key: 'size', dataIndex: 'size', width: 100 },
  { title: '状态', key: 'status', dataIndex: 'status', width: 130 },
  { title: '上传时间', dataIndex: 'created_at', width: 180 },
  { title: '操作', key: 'action', width: 180 },
]

const activeProject = computed(
  () => projects.value.find((p) => p.id === activeProjectId.value) || null,
)

const projectOptions = computed(() =>
  projects.value.map((p) => ({ value: p.id, label: p.name })),
)

const activeType = computed(
  () => types.value.find((t) => t.id === activeTypeId.value) || null,
)

// ---- 代码仓库 --------------------------------------------------------------
// 仓库与知识类型 1:1；列表跟着项目走，type_id 是两边的对位键。
const repos = ref<RepoItem[]>([])

const repoByType = computed(() => new Map(repos.value.map((r) => [r.type_id, r])))

/** 给类型列表用：类型 + 它背后的仓库（没有就是普通上传类型）。 */
const typeRows = computed(() =>
  types.value.map((t) => ({ ...t, repo: repoByType.value.get(t.id) ?? null })),
)

const activeRepo = computed(() =>
  activeTypeId.value ? repoByType.value.get(activeTypeId.value) ?? null : null,
)
const isRepoType = computed(() => activeRepo.value !== null)
const repoSyncing = computed(() => {
  const s = activeRepo.value?.status
  return s === 'syncing' || s === 'pending'
})
const retrustingId = ref<number | null>(null)

const REPO_STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: '待同步', color: 'default' },
  syncing: { label: '同步中', color: 'processing' },
  ready: { label: '就绪', color: 'success' },
  error: { label: '失败', color: 'error' },
}
function repoStatusText(status: string) {
  return REPO_STATUS_META[status]?.label ?? status
}
function repoStatusColor(status: string) {
  return REPO_STATUS_META[status]?.color ?? 'default'
}
function shortSha(sha?: string | null) {
  return sha ? sha.slice(0, 8) : ''
}
function repoBranchLabel(repo: RepoItem) {
  const branch = repo.branch || '默认分支'
  return repo.head_commit ? `${branch}@${shortSha(repo.head_commit)}` : branch
}
/** ssh 严格比对失败时 stderr 的固定措辞；据此给出「确认变更指纹」入口。 */
function isHostKeyError(msg?: string | null) {
  return !!msg && /Host key verification failed|REMOTE HOST IDENTIFICATION|主机指纹/i.test(msg)
}

const repoButtonReason = computed(() => {
  if (!embeddingReady.value) return '请先在「设置」中配置并测试通过一个向量模型'
  if (!activeProjectId.value) return '请先选择项目'
  return ''
})

let reposLoadSeq = 0
async function loadRepos() {
  const projectId = activeProjectId.value
  if (!projectId) {
    repos.value = []
    return
  }
  const seq = ++reposLoadSeq
  try {
    const data = await repoApi.list(projectId)
    // 切项目的竞态守卫，口径与 loadTypes 相同。
    if (seq !== reposLoadSeq || projectId !== activeProjectId.value) return
    repos.value = data.items
  } catch {
    /* 拦截器已经提示过了 */
  }
}

const canUpload = computed(() => embeddingReady.value && activeType.value !== null)

const uploadDisabledReason = computed(() => {
  if (!embeddingReady.value) return '请先在「设置」中配置并测试通过一个向量模型'
  if (!activeProjectId.value) return '请先选择一个项目'
  if (!activeType.value) return '请先选择一个知识类型'
  return ''
})

const indexStatusText = computed(
  () => INDEX_STATUS_META[indexState.value.status]?.label ?? indexState.value.status,
)
const indexStatusColor = computed(
  () => INDEX_STATUS_META[indexState.value.status]?.color ?? 'default',
)

/** 只要还有文档在后台处理中就为 true。 */
const hasRunningJobs = computed(() =>
  documents.value.some((d) => d.status === 'pending' || d.status === 'indexing'),
)

/** 当前项目里有仓库正在同步（克隆阶段可能还没产生文档行，所以要单独算一路）。 */
const hasRunningRepos = computed(() =>
  repos.value.some((r) => r.status === 'pending' || r.status === 'syncing'),
)

/** 轮询总闸：文档任务或仓库同步，任一在跑就继续 2.5 s 轮询，全落定即停。 */
const hasBackgroundWork = computed(() => hasRunningJobs.value || hasRunningRepos.value)

// ---- 加载 ------------------------------------------------------------------

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    embeddingReady.value = status.embedding.ready
  } catch {
    embeddingReady.value = false
  }
}

async function loadIndexState() {
  try {
    indexState.value = await knowledgeApi.indexState()
  } catch {
    /* 拦截器已经提示过了 */
  }
}

async function loadProjects() {
  loadingProjects.value = true
  try {
    const data = await knowledgeApi.listProjects()
    projects.value = data.items
    if (!projects.value.some((p) => p.id === activeProjectId.value)) {
      activeProjectId.value = projects.value[0]?.id ?? null
    }
  } catch {
    /* 拦截器已经提示过了 */
  } finally {
    loadingProjects.value = false
  }
}

async function loadTypes() {
  const projectId = activeProjectId.value
  if (!projectId) {
    // 这个接口必须带项目；没有项目也就没什么可列的。
    types.value = []
    repos.value = []
    activeTypeId.value = null
    documents.value = []
    docPage.value = 1
    docTotal.value = 0
    return
  }
  loadingTypes.value = true
  try {
    // 仓库列表与类型同属一个项目，并行拉回少一轮等待。
    const [data] = await Promise.all([knowledgeApi.listTypes(projectId), loadRepos()])
    // 等响应期间换了项目就整包丢弃，别把旧项目的类型灌进来。
    if (projectId !== activeProjectId.value) return
    types.value = data.items
    if (types.value.length && !types.value.some((t) => t.id === activeTypeId.value)) {
      selectType(types.value[0].id)
    } else if (!types.value.length) {
      activeTypeId.value = null
      documents.value = []
      docPage.value = 1
      docTotal.value = 0
    }
  } catch {
    /* 拦截器已经提示过了 */
  } finally {
    // 同上：只对「还是当前项目」的那次加载收尾。
    if (projectId === activeProjectId.value) loadingTypes.value = false
  }
}

function selectProject(id: number) {
  activeProjectId.value = id
  // 类型是挂在单个项目下面的，所以旧项目里选中的东西一个都不能留。
  activeTypeId.value = null
  documents.value = []
  repos.value = []
  docPage.value = 1
  docTotal.value = 0
  void loadTypes()
}

// 切换类型的竞态守卫：快速连点时慢响应不得覆盖新选择；被顶掉的请求
// 也不能碰 loadingDocs（否则会把新请求亮着的加载态误关掉）。
let docsLoadSeq = 0

async function loadDocuments(typeId: number, { silent = false } = {}) {
  const seq = ++docsLoadSeq
  if (!silent) loadingDocs.value = true
  try {
    const data = await knowledgeApi.listDocuments(typeId, docPage.value, docPageSize)
    if (seq !== docsLoadSeq || typeId !== activeTypeId.value) return
    documents.value = data.items
    docTotal.value = data.total
    // 删除把当前页清空了就往回退一页再拉，别把人留在空白页上。
    if (!data.items.length && data.total > 0 && docPage.value > 1) {
      docPage.value -= 1
      await loadDocuments(typeId, { silent: true })
    }
  } catch {
    /* 拦截器已经提示过了；轮询（silent）失败下次轮询会再试。 */
  } finally {
    if (seq === docsLoadSeq) loadingDocs.value = false
  }
}

const docPagination = computed(() => ({
  current: docPage.value,
  pageSize: docPageSize,
  total: docTotal.value,
  size: 'small' as const,
  showTotal: (total: number) => `共 ${total} 篇`,
}))

function onDocPageChange(pagination: { current?: number }) {
  docPage.value = pagination.current ?? 1
  // 翻页也意味着上一页的勾选项看不见了，留着只会误伤。
  selectedDocIds.value = []
  if (activeTypeId.value) loadDocuments(activeTypeId.value)
}

function refreshDocuments() {
  if (activeTypeId.value) loadDocuments(activeTypeId.value)
}

function selectType(id: number) {
  activeTypeId.value = id
  // 选中态与页码都不跨类型带过去：那些 id 在另一个列表里不存在，
  // 旧列表翻到第 5 页也不代表新列表有第 5 页。
  selectedDocIds.value = []
  docPage.value = 1
  loadDocuments(id)
}

// ---- 文档批量操作 -------------------------------------------------------------
// 表格多选后批量删除/重建索引，各走后端一个批量端点（不刷 N 次单条请求）。

const selectedDocIds = ref<number[]>([])
const batchOperating = ref(false)
// 批量请求正在处理的行：表格里打「处理中」标签，并禁掉这些行的单条操作。
const batchIds = ref<number[]>([])

const docRowSelection = computed(() => ({
  selectedRowKeys: selectedDocIds.value,
  // 批量执行期间锁住勾选，避免中途改集合导致「点了 5 篇实际删了 3 篇」。
  getCheckboxProps: () => ({ disabled: batchOperating.value }),
  onChange: (keys: (string | number)[]) => {
    selectedDocIds.value = keys.map(Number)
  },
}))

async function onBatchReindex() {
  if (!selectedDocIds.value.length || batchOperating.value) return
  batchOperating.value = true
  batchIds.value = [...selectedDocIds.value]
  try {
    const res = await knowledgeApi.batchReindexDocuments(selectedDocIds.value)
    message.success(`已重新排队索引 ${res.done} 篇文档`)
    selectedDocIds.value = []
    refreshDocuments()
  } catch (err) {
    message.error(errorText(err, '批量重建索引失败'))
  } finally {
    batchOperating.value = false
    batchIds.value = []
  }
}

function onBatchDelete() {
  const ids = [...selectedDocIds.value]
  if (!ids.length) return
  Modal.confirm({
    title: `删除选中的 ${ids.length} 篇文档？`,
    content: '文档与其向量索引会一并删除，不可恢复。',
    okText: '删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: async () => {
      batchOperating.value = true
      batchIds.value = ids
      try {
        const res = await knowledgeApi.batchDeleteDocuments(ids)
        message.success(`已删除 ${res.done} 篇文档`)
        selectedDocIds.value = []
        if (activeTypeId.value) {
          await Promise.all([loadDocuments(activeTypeId.value), loadTypes(), loadIndexState()])
        }
      } finally {
        batchOperating.value = false
        batchIds.value = []
      }
    },
  })
}

// 索引是在后台任务里跑的，没有推送通道，所以只要还有任务在进行，表格就轮询，
// 全部落定之后立刻停下来——没有任务时连定时器都不挂，不空转。keep-alive 下
// 切去别的标签页（deactivated）也要停表，回来时先补一轮再决定要不要继续。
// 失败自动退避、页面隐藏暂停（详见 utils/polling.ts）。
const pollLoop = createPollLoop(pollOnce, { shouldContinue: () => hasBackgroundWork.value })

async function pollOnce() {
  // 仓库同步（尤其克隆阶段）先刷仓库状态；文档任务刷文档表。
  if (hasRunningRepos.value) await loadRepos()
  if (
    activeTypeId.value &&
    (hasRunningJobs.value || (isRepoType.value && repoSyncing.value))
  ) {
    await loadDocuments(activeTypeId.value, { silent: true })
  }
  if (!hasBackgroundWork.value) {
    await Promise.all([loadIndexState(), loadTypes()])
  }
}

function stopPolling() {
  pollLoop.stop()
}

/** 有后台任务才挂表；任一条件翻边时由 watch 调进来，落定即停。 */
function syncPolling() {
  if (hasBackgroundWork.value) pollLoop.start()
  else pollLoop.stop()
}

watch([hasRunningJobs, hasRunningRepos], syncPolling)

// ---- 项目 ------------------------------------------------------------------

const projectModal = reactive({
  open: false,
  loading: false,
  editingId: 0,
  form: { name: '', description: '' },
})

function openProjects() {
  resetProjectForm()
  projectModal.open = true
}

function resetProjectForm() {
  projectModal.editingId = 0
  projectModal.form = { name: '', description: '' }
}

function editProject(p: KbProject) {
  projectModal.editingId = p.id
  projectModal.form = { name: p.name, description: p.description || '' }
}

async function submitProject() {
  const name = projectModal.form.name.trim()
  if (!name) {
    message.warning('请输入项目名称')
    return
  }
  projectModal.loading = true
  try {
    const payload = { name, description: projectModal.form.description.trim() || undefined }
    if (projectModal.editingId) {
      await knowledgeApi.updateProject(projectModal.editingId, payload)
      message.success('已更新')
    } else {
      const created = await knowledgeApi.createProject(payload)
      message.success('已创建')
      // 刚建好的项目，八成就是接下来要操作的那个。
      activeProjectId.value = created.id
    }
    resetProjectForm()
    await loadProjects()
    await loadTypes()
  } finally {
    projectModal.loading = false
  }
}

async function onDeleteProject(id: number) {
  await knowledgeApi.deleteProject(id)
  message.success('已删除')
  if (projectModal.editingId === id) resetProjectForm()
  if (activeProjectId.value === id) {
    activeProjectId.value = null
    activeTypeId.value = null
    documents.value = []
  }
  await loadProjects()
  await Promise.all([loadTypes(), loadIndexState()])
}

// ---- 知识类型 --------------------------------------------------------------

const typeModal = reactive({
  open: false,
  editing: false,
  loading: false,
  form: { id: 0, name: '', description: '' },
})

function openCreateType() {
  if (!activeProjectId.value) {
    message.warning('请先选择项目')
    return
  }
  typeModal.editing = false
  typeModal.form = { id: 0, name: '', description: '' }
  typeModal.open = true
}

function openEditType(t: KnowledgeType) {
  typeModal.editing = true
  typeModal.form = { id: t.id, name: t.name, description: t.description || '' }
  typeModal.open = true
}

async function submitType() {
  const name = typeModal.form.name.trim()
  if (!name) {
    message.warning('请输入名称')
    return
  }
  typeModal.loading = true
  try {
    const payload = {
      name,
      description: typeModal.form.description.trim() || undefined,
    }
    if (typeModal.editing) {
      await knowledgeApi.updateType(typeModal.form.id, payload)
      message.success('已更新')
    } else {
      if (!activeProjectId.value) {
        message.warning('请先选择项目')
        return
      }
      await knowledgeApi.createType({ ...payload, project_id: activeProjectId.value })
      message.success('已创建')
    }
    typeModal.open = false
    await Promise.all([loadTypes(), loadProjects()])
  } finally {
    typeModal.loading = false
  }
}

async function onDeleteType(id: number) {
  await knowledgeApi.deleteType(id)
  message.success('已删除')
  if (activeTypeId.value === id) {
    activeTypeId.value = null
    documents.value = []
  }
  await Promise.all([loadTypes(), loadProjects(), loadIndexState()])
}

// ---- 文档 ------------------------------------------------------------------

/** 说明这个文件为什么不能上传；没问题时返回 null。 */
function rejectReason(file: File): string | null {
  const dot = file.name.lastIndexOf('.')
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : ''
  if (!SUPPORTED_UPLOAD_EXTENSIONS.includes(ext)) {
    return `暂不支持 ${ext || '该'} 格式，目前仅支持 ${acceptLabel}`
  }
  if (!file.size) return '文件为空'
  if (file.size > MAX_UPLOAD_BYTES) return '文件过大，上限 10 MB'
  return null
}

/**
 * 文档最终存下来时用的名字。
 *
 * 上传文件夹时会保留相对路径，这样不同子目录下的两个 `README.md` 还能区分
 * 得开。截断是从前面截的，因为真正能标识它的是最里层的目录和文件名；字段
 * 长度上限是 255。
 */
function documentName(file: File): string {
  const path = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
  return path.length <= 255 ? path : path.slice(-255)
}

const handleBeforeUpload: UploadProps['beforeUpload'] = async (file) => {
  const target = file as unknown as File
  const reason = rejectReason(target)
  if (reason) {
    message.warning(reason)
    return false
  }
  await uploadBatch([target], { confirm: false })
  // 一律返回 false：请求是在这里自己发的，不走 a-upload。
  return false
}

const handleBeforeFolder: UploadProps['beforeUpload'] = async (file, fileList) => {
  // beforeUpload 会对选中的每个文件各触发一次，而每次拿到的都是同一份完整
  // 列表。只在最后一次调用时动手，就把它变成了一个批次，而不是 N 个互相
  // 抢着刷新表格的并发请求。
  if (file !== fileList[fileList.length - 1]) return false
  await uploadBatch(fileList as unknown as File[], { confirm: true })
  return false
}

const batch = reactive({ done: 0, total: 0 })

async function uploadBatch(files: File[], { confirm }: { confirm: boolean }) {
  const typeId = activeTypeId.value
  if (!typeId) {
    message.warning('请先选择知识类型')
    return
  }

  // 选文件夹是盲选的 —— 里面有什么就一股脑全带过来 —— 所以在这里先把不支持
  // 的文件筛掉，而不是让它们一个一个地失败。
  const accepted: File[] = []
  let skipped = 0
  for (const file of files) {
    if (rejectReason(file)) skipped += 1
    else accepted.push(file)
  }

  if (!accepted.length) {
    message.warning(`没有可上传的文件，已跳过 ${skipped} 个不支持或过大的文件`)
    return
  }

  if (confirm && !(await confirmBatch(accepted.length, skipped))) return

  uploading.value = true
  batch.total = accepted.length
  batch.done = 0
  const failures: string[] = []
  try {
    // 刻意串行：每次上传都会排一个索引任务，一口气丢几百个过去，
    // 向量服务商和 worker 池都得被压垮。这样跑完是慢一些，
    // 但它至少真的能跑完。
    for (const file of accepted) {
      try {
        await knowledgeApi.uploadDocument(typeId, file, documentName(file))
      } catch (err) {
        failures.push(`${documentName(file)}（${errorText(err, '上传失败')}）`)
      }
      batch.done += 1
    }
  } finally {
    uploading.value = false
    batch.total = 0
    batch.done = 0
  }

  reportBatch(accepted.length - failures.length, skipped, failures)
  await Promise.all([loadDocuments(typeId), loadTypes(), loadProjects()])
}

/** 先说清楚接下来会发生什么，毕竟索引这么多文件是要花钱的。 */
function confirmBatch(count: number, skipped: number): Promise<boolean> {
  return new Promise((resolve) => {
    Modal.confirm({
      title: '确认上传',
      content:
        `将上传 ${count} 个文件` +
        (skipped ? `，跳过 ${skipped} 个不支持或过大的文件` : '') +
        '。每个文件都会切片并调用向量模型，会产生费用。',
      okText: '开始上传',
      cancelText: '取消',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

function reportBatch(ok: number, skipped: number, failures: string[]) {
  const skipText = skipped ? `，跳过 ${skipped} 个` : ''
  if (!failures.length) {
    message.success(`已上传 ${ok} 个文件${skipText}，正在后台切片与向量化`)
    return
  }
  // 只列前几条，否则一个有问题的文件夹会刷出一大片没法读的内容。
  const shown = failures.slice(0, 3).join('；')
  const more = failures.length > 3 ? ` 等 ${failures.length} 个` : ''
  message.error(`${ok} 个已上传${skipText}，失败：${shown}${more}`, 8)
}

async function onReindex(id: number) {
  try {
    await knowledgeApi.reindexDocument(id)
    message.success('已重新排队索引')
    refreshDocuments()
  } catch (err) {
    message.error(errorText(err, '重新索引失败'))
  }
}

async function onDeleteDoc(id: number) {
  await knowledgeApi.deleteDocument(id)
  message.success('已删除')
  if (activeTypeId.value) {
    await Promise.all([loadDocuments(activeTypeId.value), loadTypes(), loadIndexState()])
  }
}

// ---- 索引 ------------------------------------------------------------------

async function onRebuild() {
  rebuilding.value = true
  try {
    const result = await knowledgeApi.rebuildIndex()
    if (result.started) {
      message.success(result.message)
    } else {
      message.info(result.message)
    }
    await Promise.all([loadIndexState(), activeTypeId.value ? loadDocuments(activeTypeId.value) : null])
  } catch (err) {
    message.error(errorText(err, '重建失败'))
  } finally {
    rebuilding.value = false
  }
}

// ---- 检索预览 --------------------------------------------------------------

const searchModal = reactive({
  open: false,
  loading: false,
  searched: false,
  query: '',
  hits: [] as SearchHit[],
})

function openSearch() {
  searchModal.open = true
  searchModal.searched = false
  searchModal.hits = []
}

async function runSearch() {
  const query = searchModal.query.trim()
  if (!query || !activeProjectId.value) return
  searchModal.loading = true
  try {
    // 限定在当前选中的项目范围内，这样预览结果和对话时看到的是一致的。
    const result = await knowledgeApi.search({
      query,
      top_k: 8,
      project_id: activeProjectId.value,
    })
    searchModal.hits = result.hits
    searchModal.searched = true
  } catch (err) {
    message.error(errorText(err, '检索失败'))
  } finally {
    searchModal.loading = false
  }
}

// ---- 代码仓库：接入/编辑/同步/指纹 -------------------------------------------

const GLOBS_PLACEHOLDER = '**/test/**\n**/*.generated.*'

const repoModal = reactive({
  open: false,
  editing: false,
  repoId: 0,
  submitting: false,
  testing: false,
  generating: false,
  showPrivate: false,
  form: {
    name: '',
    remote_url: '',
    branch: '',
    auth_type: 'ssh_key' as 'ssh_key' | 'none' | 'http_token',
    private_key: '',
    http_token: '',
    http_username: '',
    exclude_globs: '',
    // SSH 接入时可选的钉死指纹；编辑弹窗不提供（变更走 retrust 流程）。
    expected_fingerprint: '',
  },
  // 弹窗里即时生成的公钥（私钥只存在 form.private_key，提交后再也读不回来）。
  generatedPublicKey: '',
  // 测试连接成功后暂存的探测结果，提交时原样回传做首次指纹记录。
  probe: null as RepoTestConnectionResult | null,
  probeError: '',
  submitError: '',
})

/** 编辑中仓库已登记的公钥；生成新密钥对后改显新公钥。 */
const editingRepo = ref<RepoItem | null>(null)
const editingPublicKey = computed(() => {
  if (repoModal.form.auth_type !== 'ssh_key') return ''
  return repoModal.generatedPublicKey || editingRepo.value?.public_key || ''
})

function resetRepoModal() {
  repoModal.editing = false
  repoModal.repoId = 0
  repoModal.submitting = false
  repoModal.testing = false
  repoModal.generating = false
  repoModal.showPrivate = false
  repoModal.form = {
    name: '',
    remote_url: '',
    branch: '',
    auth_type: 'ssh_key',
    private_key: '',
    http_token: '',
    http_username: '',
    exclude_globs: '',
    expected_fingerprint: '',
  }
  repoModal.generatedPublicKey = ''
  repoModal.probe = null
  repoModal.probeError = ''
  repoModal.submitError = ''
  editingRepo.value = null
}

function openCreateRepo() {
  if (!embeddingReady.value || !activeProjectId.value) return
  resetRepoModal()
  repoModal.open = true
}

function openEditRepo(repo: RepoItem) {
  resetRepoModal()
  repoModal.editing = true
  repoModal.repoId = repo.id
  editingRepo.value = repo
  repoModal.form = {
    name: repo.name,
    remote_url: repo.remote_url,
    branch: repo.branch || '',
    auth_type:
      (repo.auth_type as 'ssh_key' | 'none' | 'http_token') || 'ssh_key',
    private_key: '',
    // 令牌不回读：编辑框永远空白，留空=不变。
    http_token: '',
    http_username: '',
    exclude_globs: repo.exclude_globs || '',
    expected_fingerprint: '',
  }
  repoModal.open = true
}

/** 认证方式标签/颜色（信息条用）。 */
function repoAuthMeta(auth: string) {
  if (auth === 'ssh_key') return { label: 'Deploy Key', color: 'cyan' }
  if (auth === 'http_token') return { label: 'HTTPS 令牌', color: 'geekblue' }
  return { label: '匿名访问', color: 'default' }
}

/** 跳过原因码 → 中文标签，与后端 code_chunk/repo_sync 的 SKIP_REASON_LABELS 各维护一份。 */
const SKIP_REASON_LABELS: Record<string, string> = {
  excluded: '命中排除规则',
  unsupported_ext: '非受支持的源码扩展名',
  empty: '空文件',
  binary: '疑似二进制文件',
  too_large: '超过单文件大小上限',
  long_line: '超长行（疑似压缩/生成文件）',
  blank: '纯空白文件',
  path_too_long: '路径超过 512 字符',
  max_files: '超出文件数上限',
  index_failed: '索引失败',
  symlink: '符号链接（不索引）',
  submodule: 'Git 子模块（不递归索引）',
  unsafe_path: '非法路径',
}

/** 同步报告里的跳过明细，按次数降序；老版本落库的结果没有 skip_reasons，返回空数组。 */
function skipBreakdown(result?: RepoSyncResult | null) {
  const reasons = result?.skip_reasons
  if (!reasons) return []
  return Object.entries(reasons)
    .map(([reason, count]) => ({
      reason,
      label: SKIP_REASON_LABELS[reason] || reason,
      count,
      samples: result?.skip_samples?.[reason] || [],
    }))
    .sort((a, b) => b.count - a.count)
}

/** 弹窗里按当前地址推导协议：http(s) 才允许匿名/令牌，其余一律走 SSH。 */
const repoFormProtocol = computed<'ssh' | 'http'>(() =>
  /^https?:\/\//i.test(repoModal.form.remote_url.trim()) ? 'http' : 'ssh',
)

/** 地址或私钥一变，上次探测拿到的 known_hosts 就作废，不能再随提交回传。 */
function invalidateProbe() {
  repoModal.probe = null
  repoModal.probeError = ''
}

function onAuthTypeChange() {
  invalidateProbe()
  repoModal.generatedPublicKey = ''
  if (repoModal.form.auth_type !== 'ssh_key') {
    repoModal.form.private_key = ''
    repoModal.form.expected_fingerprint = ''
  }
  if (repoModal.form.auth_type !== 'http_token') {
    // 切走令牌认证时清掉明文，避免误带到后端。
    repoModal.form.http_token = ''
    repoModal.form.http_username = ''
  }
}

/** 地址协议变了，可选项随之切换；旧协议下的凭据必须清掉。 */
function onRemoteUrlChange() {
  invalidateProbe()
  const form = repoModal.form
  if (repoFormProtocol.value === 'http') {
    if (form.auth_type === 'ssh_key') {
      form.auth_type = 'none'
      form.private_key = ''
      form.expected_fingerprint = ''
      repoModal.generatedPublicKey = ''
    }
  } else if (form.auth_type !== 'ssh_key') {
    form.auth_type = 'ssh_key'
    form.http_token = ''
    form.http_username = ''
  }
}

async function generateKeypair() {
  repoModal.generating = true
  try {
    const kp = await repoApi.generateKeypair()
    repoModal.form.private_key = kp.private_key
    repoModal.generatedPublicKey = kp.public_key
    // 新私钥和旧探测结果肯定不配套。
    invalidateProbe()
  } catch (err) {
    message.error(errorText(err, '生成密钥对失败'))
  } finally {
    repoModal.generating = false
  }
}

async function testConnection() {
  const url = repoModal.form.remote_url.trim()
  if (!url) {
    message.warning('请先填写 Git 地址')
    return
  }
  repoModal.testing = true
  repoModal.probeError = ''
  repoModal.probe = null
  try {
    const form = repoModal.form
    const result = await repoApi.testConnection({
      remote_url: url,
      branch: form.branch.trim() || null,
      private_key:
        form.auth_type === 'ssh_key' ? form.private_key.trim() || null : null,
      http_token:
        form.auth_type === 'http_token' ? form.http_token.trim() || null : null,
      http_username:
        form.auth_type === 'http_token' ? form.http_username.trim() || null : null,
      // 仅 SSH 且填写了钉死指纹时参与严格校验；HTTP 协议带指纹会被后端直接拒绝。
      expected_fingerprint:
        form.auth_type === 'ssh_key'
          ? form.expected_fingerprint.trim() || null
          : null,
    })
    repoModal.probe = result
    if (!repoModal.form.branch.trim() && result.default_branch) {
      repoModal.form.branch = result.default_branch
    }
  } catch (err) {
    repoModal.probeError = errorText(err, '连接失败')
  } finally {
    repoModal.testing = false
  }
}

async function submitRepo() {
  const form = repoModal.form
  repoModal.submitError = ''

  if (!repoModal.editing) {
    const url = form.remote_url.trim()
    if (url.length < 4) {
      repoModal.submitError = '请填写 Git 地址'
      return
    }
    if (form.auth_type === 'ssh_key' && !form.private_key.trim()) {
      repoModal.submitError = 'SSH 仓库需要 deploy 私钥：粘贴现有私钥或生成新密钥对'
      return
    }
    if (form.auth_type === 'http_token' && !form.http_token.trim()) {
      repoModal.submitError = '请粘贴个人访问令牌（PAT）'
      return
    }
    if (!activeProjectId.value) {
      repoModal.submitError = '请先选择项目'
      return
    }
    repoModal.submitting = true
    try {
      const created = await repoApi.create({
        project_id: activeProjectId.value,
        remote_url: url,
        branch: form.branch.trim() || null,
        auth_type: form.auth_type,
        private_key: form.auth_type === 'ssh_key' ? form.private_key.trim() : null,
        http_token: form.auth_type === 'http_token' ? form.http_token.trim() : null,
        http_username:
          form.auth_type === 'http_token' ? form.http_username.trim() || null : null,
        known_hosts: repoModal.probe?.known_hosts || null,
        host_fingerprint: repoModal.probe?.host_fingerprint || null,
        name: form.name.trim() || null,
        exclude_globs: form.exclude_globs.trim() || null,
      })
      message.success('仓库已接入，首次同步已在后台开始')
      repoModal.open = false
      // loadTypes 自己会默认选第一个类型；这里随后显式切到新仓库的类型，
      // 让用户立刻看到仓库信息条与同步进度。
      await loadTypes()
      selectType(created.type_id)
      syncPolling()
    } catch (err) {
      // 地址不合法、私钥读不出、重名等业务拒绝：留在弹窗里就地改。
      repoModal.submitError = errorText(err, '接入失败')
    } finally {
      repoModal.submitting = false
    }
    return
  }

  // 编辑：只传可改字段。私钥/令牌留空=不变；分支空串=回到远端默认分支。
  const payload: RepoUpdatePayload = {
    name: form.name.trim(),
    branch: form.branch.trim(),
    exclude_globs: form.exclude_globs.trim(),
  }
  if (form.auth_type === 'ssh_key') {
    if (form.private_key.trim()) payload.private_key = form.private_key.trim()
  } else {
    // http(s) 行允许在匿名与令牌之间切换；切到匿名时后端清掉旧令牌。
    payload.auth_type = form.auth_type
    if (form.auth_type === 'http_token') {
      // 编辑时令牌/用户名留空=不变；只有显式填了才下发。
      if (form.http_token.trim()) payload.http_token = form.http_token.trim()
      if (form.http_username.trim()) payload.http_username = form.http_username.trim()
    }
  }
  repoModal.submitting = true
  try {
    await repoApi.update(repoModal.repoId, payload)
    message.success('已保存；下次同步生效')
    repoModal.open = false
    await loadRepos()
  } catch (err) {
    repoModal.submitError = errorText(err, '保存失败')
  } finally {
    repoModal.submitting = false
  }
}

async function onSyncRepo(repo: RepoItem) {
  try {
    const res = await repoApi.sync(repo.id)
    if (res.already_running) message.info(res.message)
    else message.success(res.message)
    await loadRepos()
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '同步失败'))
  }
}

async function onRetrust(repo: RepoItem) {
  retrustingId.value = repo.id
  try {
    await repoApi.retrust(repo.id)
    message.success('主机指纹已更新，已重新开始同步')
    // retrust 只换钉子；真正恢复还得再跑一次同步，顺手排上。
    await repoApi.sync(repo.id)
    await loadRepos()
    syncPolling()
  } catch (err) {
    message.error(errorText(err, '更新指纹失败'))
  } finally {
    retrustingId.value = null
  }
}

async function onRemoveRepo(repo: RepoItem) {
  try {
    await repoApi.remove(repo.id)
    message.success('仓库已移除')
    if (activeTypeId.value === repo.type_id) {
      activeTypeId.value = null
      documents.value = []
    }
    await Promise.all([loadTypes(), loadIndexState()])
  } catch (err) {
    message.error(errorText(err, '移除失败'))
  }
}

/** 检索预览命中代码分片时：path:起-止；散文分片退化为文件名。 */
function hitLocation(hit: SearchHit) {
  if (hit.line_start == null) return hit.filename
  return hit.line_end && hit.line_end !== hit.line_start
    ? `${hit.filename}:${hit.line_start}-${hit.line_end}`
    : `${hit.filename}:${hit.line_start}`
}

// ---- 仓库源文件只读预览（带行号） ----
const previewOpen = ref(false)
const previewLoading = ref(false)
const preview = ref<DocumentContent | null>(null)
// 检索命中跳转过来时高亮的行号
const previewAnchor = ref<number | null>(null)

/**
 * 与后端 Python splitlines 的行数口径保持一致：去掉唯一一个末尾换行
 * 产生的空串（"a\n" 是 1 行而不是 2 行），中间的空行保留。
 */
function toSourceLines(content: string): string[] {
  if (!content) return []
  const lines = content.split(/\r\n|\r|\n/)
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop()
  return lines
}
const previewLines = computed(() => (preview.value ? toSourceLines(preview.value.content) : []))

async function scrollPreviewTo(line: number) {
  await nextTick()
  document.getElementById(`src-line-${line}`)?.scrollIntoView({ block: 'center' })
}

async function openPreview(docId: number, anchorLine: number | null = null) {
  previewOpen.value = true
  previewLoading.value = true
  preview.value = null
  previewAnchor.value = anchorLine
  try {
    preview.value = await knowledgeApi.documentContent(docId)
    if (anchorLine != null) await scrollPreviewTo(anchorLine)
  } catch (err) {
    message.error(errorText(err, '读取源文件失败'))
    previewOpen.value = false
  } finally {
    previewLoading.value = false
  }
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success('已复制')
  } catch {
    // 非安全上下文（http 内网地址）下 clipboard API 可能不可用，退回隐藏文本域。
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      message.success('已复制')
    } catch {
      message.error('复制失败，请手动选择复制')
    } finally {
      document.body.removeChild(ta)
    }
  }
}

// ---- 辅助函数 --------------------------------------------------------------

function goToSettings() {
  router.push('/system/settings')
}

// 文件大小格式化统一用 utils/format 的 fmtSize（812 B / 4.2 K / 30.1 M）。

onMounted(async () => {
  loading.value = true
  try {
    await Promise.all([loadReadiness(), loadIndexState(), loadProjects()])
    // 加载类型必须先确定项目，所以这一步没法并进上面那批一起发。
    await loadTypes()
  } finally {
    loading.value = false
  }
  syncPolling()
})

// keep-alive：切走时停表；回来时本地状态可能是旧的，先补一轮再按结果挂表。
onDeactivated(stopPolling)
onActivated(() => {
  void pollOnce().finally(syncPolling)
})

onUnmounted(stopPolling)
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}
/* flex 基底用全局 .toolbar（style.css），这里只留本页差异。 */
.kb-stats {
  font-size: var(--fs-sm);
}
/* hover/active 底色用全局 .list-row；.muted/.hint 用全局共享类（style.css）。 */
.upload-hint {
  margin-bottom: 12px;
}
.batch-progress {
  color: var(--signal-text);
  margin-left: 4px;
}
/* 文档批量操作条：选中才出现，信号色浅底与全局选中态同族。 */
.doc-batch-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding: 6px 10px;
  border-radius: var(--tool-radius);
  background: var(--signal-bg);
  font-size: var(--fs-xs);
}
.project-form {
  margin-bottom: 12px;
}
.search-results {
  margin-top: 16px;
  max-height: 420px;
  overflow-y: auto;
}
.hit-content {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-2);
  font-size: var(--fs-xs);
  line-height: 1.7;
}
.hit-content--code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

/* ---- 代码仓库 ---------------------------------------------------------- */
.repo-line {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.repo-bar {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--hairline);
  border-radius: var(--tool-radius);
  background: var(--paper);
}
.repo-bar-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: var(--fs-sm);
}
.repo-bar-row + .repo-bar-row {
  margin-top: 6px;
}
.repo-branch-icon {
  color: var(--signal-text);
}
.repo-url {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--fs-xs);
}
.repo-spacer {
  flex: 1;
}
.repo-add {
  color: var(--success, #52a852);
}
.repo-change {
  color: var(--signal-text);
}
.repo-remove {
  color: var(--danger);
}
.repo-dot {
  margin: 0 2px;
}
.repo-live {
  margin-left: 4px;
  color: var(--signal-text);
}
.skip-link {
  cursor: pointer;
  color: var(--signal-text);
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}
.skip-popover {
  max-width: 420px;
  max-height: 320px;
  overflow-y: auto;
}
.skip-row + .skip-row {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--hairline);
}
.skip-row-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: var(--fs-sm);
}
.skip-sample {
  margin-top: 2px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--fs-xs);
  color: var(--signal-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.repo-warn-tag {
  margin-inline-start: 2px;
}
.repo-advanced {
  margin-top: 8px;
}
.repo-error {
  margin-top: 8px;
}
.repo-hint {
  margin: 8px 0 0;
}
.repo-file-path {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--fs-xs);
  word-break: break-all;
}
.repo-file-link {
  cursor: pointer;
}
.repo-file-link:hover {
  color: #1677ff;
  text-decoration: underline;
}
.src-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.src-view {
  max-height: 60vh;
  overflow: auto;
  background: rgba(0, 0, 0, 0.02);
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--fs-xs);
  line-height: 1.7;
}
.src-line {
  display: flex;
}
.src-line:hover {
  background: rgba(0, 0, 0, 0.04);
}
.src-line--hit,
.src-line--hit:hover {
  background: rgba(250, 219, 20, 0.28);
}
.src-gutter {
  flex: none;
  width: 56px;
  padding-right: 10px;
  text-align: right;
  color: rgba(128, 128, 128, 0.85);
  user-select: none;
  position: sticky;
  left: 0;
  background: inherit;
}
.src-code {
  flex: 1;
  padding-right: 12px;
  white-space: pre;
  color: inherit;
  background: transparent;
  font-family: inherit;
}
.repo-key-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 6px;
}
/* 私钥默认打码：webkit-text-security 把每个字符替成圆点，比自己换密码框
   更适合多行 PEM；勾选「显示私钥」后移除该类即恢复明文。 */
.repo-key-hidden :deep(textarea) {
  -webkit-text-security: disc;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.repo-key-input :deep(textarea),
.repo-public-key :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.repo-public-key {
  margin-bottom: 0;
}
.repo-probe-ok {
  font-size: var(--fs-xs);
  color: var(--text-2);
}
.repo-form-error {
  margin-top: 6px;
  font-size: var(--fs-xs);
  color: var(--danger);
}
.repo-form-hint {
  margin-top: 4px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}
</style>
