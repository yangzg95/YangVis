<template>
  <div class="page-card">
    <div class="page-title">模型与集成</div>

    <a-tabs v-model:activeKey="activeTab">
      <!-- ================= 模型管理 ================= -->
      <a-tab-pane key="models" tab="模型管理">
        <!-- 模型配置有两个入口，容易找错：这里是站内自用模型，AI 网关是对外供给。
             两边各自指路，省得用户在两个页面之间来回翻。 -->
        <a-alert type="info" show-icon class="gate-alert">
          <template #message>这一页配的是站内自用模型</template>
          <template #description>
            控制台里的对话、简历、幻灯片与知识库检索都从这里取模型，每个账号各配各的。
            如果你要找的是「把本站当成 OpenAI 接口给别人调用」，那是管理员在「AI 网关」维护的上游通道、模型路由与统一密钥。
          </template>
          <template #action>
            <a-button v-if="auth.isAdmin" size="small" @click="goGateway">前往 AI 网关</a-button>
          </template>
        </a-alert>

        <a-alert
          v-if="!loading && !status.embedding.ready"
          type="warning"
          show-icon
          class="gate-alert"
          message="未配置可用的向量模型"
          description="对话与知识库检索依赖向量模型。请添加一个用途为「向量」的模型，并通过连通性测试。"
        >
          <template #action>
            <a-button size="small" type="primary" @click="openCreate('embedding')">
              立即添加
            </a-button>
          </template>
        </a-alert>

        <a-alert
          v-if="!loading && !status.chat.ready"
          type="warning"
          show-icon
          class="gate-alert"
          message="未配置可用的对话模型"
          description="对话功能需要一个已通过测试的对话模型才能回答问题。"
        >
          <template #action>
            <a-button size="small" type="primary" @click="openCreate('chat')">立即添加</a-button>
          </template>
        </a-alert>

        <div class="toolbar toolbar--between toolbar--snug">
          <a-radio-group v-model:value="filterPurpose" button-style="solid" @change="load">
            <a-radio-button value="">全部</a-radio-button>
            <a-radio-button value="chat">对话</a-radio-button>
            <a-radio-button value="embedding">向量</a-radio-button>
          </a-radio-group>
          <a-button type="primary" @click="openCreate()">
            <PlusOutlined /> 新增模型
          </a-button>
        </div>

        <a-table
          :data-source="models"
          :columns="columns"
          row-key="id"
          :loading="loading"
          size="middle"
          :pagination="{ pageSize: 8 }"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'purpose'">
              <a-tag :color="record.purpose === 'chat' ? 'blue' : 'purple'">
                {{ record.purpose === 'chat' ? '对话' : '向量' }}
              </a-tag>
              <a-tag v-if="record.is_default" color="green">默认</a-tag>
              <a-tag v-if="record.purpose === 'chat' && record.supports_vision" color="cyan">
                图片
              </a-tag>
            </template>

            <template v-else-if="column.key === 'api_key'">
              <a-tooltip v-if="record.api_key_error" :title="record.api_key_error">
                <span class="key-error">密钥无法解密</span>
              </a-tooltip>
              <span v-else class="muted">{{ record.api_key || '—' }}</span>
            </template>

            <template v-else-if="column.key === 'state'">
              <a-tooltip v-if="record.last_test_ok" :title="testedAtText(record)">
                <a-tag color="success">
                  可用<template v-if="record.vector_size"> · {{ record.vector_size }} 维</template>
                </a-tag>
              </a-tooltip>
              <a-tooltip v-else-if="record.last_test_error" :title="record.last_test_error">
                <a-tag color="error">测试失败</a-tag>
              </a-tooltip>
              <a-tag v-else>未测试</a-tag>
            </template>

            <template v-else-if="column.key === 'action'">
              <a-space>
                <a-button size="small" :loading="testingId === record.id" @click="onTest(record)">
                  测试
                </a-button>
                <a-tooltip :title="record.last_test_ok ? '' : '需先通过连通性测试'">
                  <a-button
                    size="small"
                    :disabled="record.is_default || !record.last_test_ok"
                    @click="onSetDefault(record)"
                  >
                    设为默认
                  </a-button>
                </a-tooltip>
                <a-button size="small" @click="openEdit(record)">编辑</a-button>
                <a-popconfirm title="确认删除该模型？" @confirm="onDelete(record.id)">
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
        </a-table>
      </a-tab-pane>

      <!-- ================= 百度网盘 ================= -->
      <a-tab-pane key="netdisk" tab="百度网盘">
        <template v-if="netdisk.configured">
          <a-space v-if="netdisk.bound" wrap>
            <a-tag color="blue" class="netdisk-tag">
              <CloudOutlined /> {{ netdisk.baidu_name || '已绑定网盘' }}
            </a-tag>
            <span class="hint">
              简历等模块的原文件会同步到你网盘的应用目录（/apps/应用名/）。
            </span>
            <a-button size="small" @click="openBindNetdisk">重新绑定</a-button>
            <a-popconfirm
              title="解除绑定不会删除网盘里已同步的文件，确认解绑？"
              ok-text="解绑"
              cancel-text="取消"
              @confirm="onUnbindNetdisk"
            >
              <a-button size="small" danger>解绑</a-button>
            </a-popconfirm>
          </a-space>
          <a-space v-else wrap>
            <span class="hint">绑定后，简历等模块的原文件会同步一份到你的网盘。</span>
            <a-button type="primary" size="small" @click="openBindNetdisk">
              <CloudOutlined /> 绑定百度网盘
            </a-button>
          </a-space>
        </template>
        <div v-else class="hint">
          未启用：请在后端 .env 配置 BAIDU_NETDISK_APP_KEY / SECRET_KEY / APP_NAME。
        </div>
      </a-tab-pane>

      <!-- ================= 长期记忆 ================= -->
      <a-tab-pane key="memory" tab="长期记忆">
        <a-alert
          type="info"
          show-icon
          class="gate-alert"
          message="长期记忆是 AI 在对话中自动沉淀的、关于你的持久事实（职业背景、偏好、长期目标等）。"
          description="只有开了「长期记忆」开关的智能体会使用并积累记忆；你可以在这里查看、修改或删除任何一条。"
        />

        <div class="toolbar toolbar--between toolbar--snug">
          <a-button type="primary" @click="openMemoryCreate">
            <PlusOutlined /> 手动添加
          </a-button>
          <a-popconfirm
            title="确认清空全部记忆？此操作不可恢复。"
            ok-text="清空"
            cancel-text="取消"
            @confirm="onMemoryClear"
          >
            <a-button danger :disabled="!memories.length">清空全部</a-button>
          </a-popconfirm>
        </div>

        <a-list :data-source="memories" :loading="memoryLoading" item-layout="horizontal">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta :description="`更新于 ${new Date(item.updated_at).toLocaleString()}`">
                <template #title>{{ item.content }}</template>
              </a-list-item-meta>
              <template #actions>
                <a-button size="small" @click="openMemoryEdit(item)">编辑</a-button>
                <a-popconfirm title="删除这条记忆？" @confirm="onMemoryDelete(item.id)">
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
              </template>
            </a-list-item>
          </template>
          <template #empty>
            <a-empty description="还没有记忆。与开了「长期记忆」的智能体对话后会自动积累。" />
          </template>
        </a-list>
      </a-tab-pane>

      <!-- ================= 消息通知 ================= -->
      <a-tab-pane key="notify" tab="消息通知">
<!--        <a-alert type="info" show-icon class="gate-alert">-->
<!--          <template #message>通知走群机器人 Webhook</template>-->
<!--          <template #description>-->
<!--            工作流跑失败、AI 提议的运维命令待确认或已过期时，按订阅关系推一条消息。发送是旁路：-->
<!--            失败只留下发送记录，不影响任务本身。正文只带标题、原因和命令首词，不含 SQL 与密钥全文。-->
<!--          </template>-->
<!--        </a-alert>-->

        <div class="toolbar toolbar--between toolbar--snug">
          <span class="hint">一个渠道＝一个群机器人或一个华为推送应用，订阅哪些事件按渠道各配。</span>
          <a-button type="primary" @click="openNotifyCreate">
            <PlusOutlined /> 新增渠道
          </a-button>
        </div>

        <a-table
          :data-source="notifyChannels"
          :columns="notifyColumns"
          row-key="id"
          :loading="notifyLoading"
          size="middle"
          :pagination="false"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'kind'">
              <a-tag>{{ kindLabel(record.kind) }}</a-tag>
            </template>

            <template v-else-if="column.key === 'webhook_url'">
              <template v-if="record.kind === 'huawei'">
                <span class="muted">{{ huaweiTargetText(record) }}</span>
                <a-tooltip
                  v-if="hwDetail(record.id)?.client_id_error"
                  :title="hwDetail(record.id)?.client_id_error"
                >
                  <div class="key-error">Client ID 无法解密</div>
                </a-tooltip>
                <a-tooltip
                  v-if="hwDetail(record.id)?.client_secret_error"
                  :title="hwDetail(record.id)?.client_secret_error"
                >
                  <div class="key-error">Client Secret 无法解密</div>
                </a-tooltip>
              </template>
              <template v-else>
                <a-tooltip v-if="record.webhook_url_error" :title="record.webhook_url_error">
                  <span class="key-error">地址无法解密</span>
                </a-tooltip>
                <span v-else class="muted">{{ record.webhook_url || '—' }}</span>
                <div v-if="record.secret_error" class="key-error">加签密钥无法解密</div>
              </template>
            </template>

            <template v-else-if="column.key === 'events'">
              <a-tag v-for="key in record.events" :key="key">{{ eventLabel(key) }}</a-tag>
              <a-tooltip v-if="!record.events.length" title="没有订阅任何事件，这个渠道一条消息也不会发">
                <span class="hint">未订阅</span>
              </a-tooltip>
            </template>

            <template v-else-if="column.key === 'enabled'">
              <a-switch
                :checked="record.enabled"
                size="small"
                @change="(value: boolean) => onNotifyToggle(record, value)"
              />
            </template>

            <template v-else-if="column.key === 'test'">
              <a-tooltip v-if="record.last_test_ok" :title="notifyTestedAtText(record)">
                <a-tag color="success">可用</a-tag>
              </a-tooltip>
              <a-tooltip v-else-if="record.last_test_error" :title="record.last_test_error">
                <a-tag color="error">测试失败</a-tag>
              </a-tooltip>
              <a-tag v-else>未测试</a-tag>
            </template>

            <template v-else-if="column.key === 'action'">
              <a-space>
                <a-button
                  size="small"
                  :loading="notifyTestingId === record.id"
                  @click="onNotifyTest(record)"
                >
                  测试
                </a-button>
                <a-button size="small" @click="openNotifyEdit(record)">编辑</a-button>
                <a-popconfirm title="确认删除该渠道？" @confirm="onNotifyDelete(record.id)">
                  <a-button size="small" danger>删除</a-button>
                </a-popconfirm>
              </a-space>
            </template>
          </template>
        </a-table>

        <div class="toolbar toolbar--between toolbar--snug notify-logs-head">
          <span class="hint">最近发送记录（保留 7 天）。</span>
          <a-button
            size="small"
            :loading="notifyLogLoading"
            :disabled="notifyLogLoading"
            @click="loadNotifyLogs"
          >
            刷新
          </a-button>
        </div>

        <a-table
          :data-source="notifyLogs"
          :columns="notifyLogColumns"
          row-key="id"
          :loading="notifyLogLoading"
          size="small"
          :pagination="{ pageSize: 10 }"
          :scroll="{ x: 'max-content' }"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'created_at'">
              <span class="muted">{{ fmtDateTime(record.created_at) }}</span>
            </template>
            <template v-else-if="column.key === 'event'">
              {{ eventLabel(record.event) }}
            </template>
            <template v-else-if="column.key === 'status'">
              <a-tooltip v-if="record.error" :title="record.error">
                <a-tag :color="record.status === 'sent' ? 'success' : 'error'">
                  {{ record.status === 'sent' ? '已发送' : '失败' }}
                </a-tag>
              </a-tooltip>
              <a-tag v-else :color="record.status === 'sent' ? 'success' : 'error'">
                {{ record.status === 'sent' ? '已发送' : '失败' }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'elapsed'">
              {{ record.elapsed_ms === null ? '—' : `${record.elapsed_ms} ms` }}
            </template>
          </template>
          <template #emptyText>
            <a-empty description="还没有发送记录" />
          </template>
        </a-table>
      </a-tab-pane>
    </a-tabs>

    <!-- ================= 记忆编辑弹窗 ================= -->
    <a-modal
      v-model:open="memoryModal.open"
      :title="memoryModal.editing ? '编辑记忆' : '添加记忆'"
      :confirm-loading="memoryModal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submitMemory"
    >
      <a-form layout="vertical">
        <a-form-item label="内容" required>
          <a-textarea
            v-model:value="memoryModal.content"
            :rows="3"
            :maxlength="512"
            placeholder="一句自包含的话，例如：用户是有 5 年经验的 Go 后端工程师"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- ================= 网盘绑定弹窗 ================= -->
    <a-modal
      v-model:open="bindModal.open"
      title="绑定百度网盘"
      :confirm-loading="bindModal.loading"
      ok-text="完成绑定"
      cancel-text="取消"
      @ok="submitBindNetdisk"
    >
      <ol class="bind-steps">
        <li>点击下方按钮，在新窗口打开的百度授权页中登录并同意授权；</li>
        <li>授权后页面会显示一串授权码，复制它；</li>
        <li>把授权码粘贴到下方输入框，点击「完成绑定」。</li>
      </ol>
      <a-button block :loading="bindModal.authLoading" @click="openAuthPage">
        打开百度授权页（新窗口）
      </a-button>
      <a-input
        v-model:value="bindModal.code"
        class="bind-code-input"
        :maxlength="128"
        placeholder="粘贴授权码"
        @press-enter="submitBindNetdisk"
      />
      <p class="hint bind-tip">
        绑定后，各模块的原文件会同步到你网盘的应用目录（/apps/应用名/）；之后解绑不会删除已同步的文件。
      </p>
    </a-modal>

    <a-modal
      v-model:open="notifyModal.open"
      :title="notifyModal.id ? '编辑渠道' : '新增渠道'"
      :confirm-loading="notifyModal.loading"
      ok-text="保存"
      cancel-text="取消"
      width="520px"
      @ok="submitNotifyChannel"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input
            v-model:value="notifyModal.form.name"
            :maxlength="64"
            placeholder="例如：运维值班群"
          />
        </a-form-item>
        <a-form-item label="渠道类型" required>
          <a-select
            v-model:value="notifyModal.form.kind"
            :options="KIND_OPTIONS"
            :disabled="Boolean(notifyModal.id)"
          />
          <div class="hint">
            各家的文本消息格式不同，选错会发不出去或发出一条空消息；「自定义」发给自建接收端，正文是 JSON；
            「华为推送」走 HMS Push Kit 直发手机，没有 Webhook 地址。类型保存后不可切换，要换请删除重建。
          </div>
        </a-form-item>
        <a-form-item
          v-if="!isHuaweiForm"
          label="Webhook 地址"
          :required="!notifyModal.id || notifyModal.urlBroken"
        >
          <a-input
            v-model:value="notifyModal.form.webhook_url"
            :placeholder="
              notifyModal.id
                ? notifyModal.urlBroken
                  ? '地址解不开，请重新粘贴完整地址'
                  : '留空表示不修改已保存的地址'
                : 'https://open.feishu.cn/open-apis/bot/v2/hook/...'
            "
            autocomplete="off"
          />
          <div class="hint">
            <template v-if="notifyModal.id">
              当前：{{ notifyModal.maskedUrl || '地址无法解密，请重新填写' }}。地址加密落库，列表只显示掩码。
            </template>
            <template v-else>
              粘贴群里机器人详情里的完整地址（加密落库，列表只显示掩码）。填内网自建接收端时，
              请确认这台机器允许被外发消息。
            </template>
          </div>
        </a-form-item>
        <a-form-item v-if="!isHuaweiForm && notifyNeedsSecret" label="加签密钥">
          <a-input-password
            v-model:value="notifyModal.form.secret"
            :placeholder="
              notifyModal.hadSecret ? '留空表示不修改已保存的密钥' : 'SEC 开头的加签密钥，未加签则留空'
            "
            autocomplete="new-password"
          />
          <div class="hint">
            <template v-if="notifyModal.id">
              留空表示不改动已保存的密钥；已存的密钥在页面上删不掉，不用了这个渠道直接删除重建。
            </template>
            <template v-else>钉钉「加签」机器人必填；飞书只有开了「签名校验」才填；只配了关键词的机器人留空。</template>
          </div>
        </a-form-item>

        <!-- 华为 HMS Push：OAuth 凭据 + 推送目标，与 webhook 字段完全不同 -->
        <template v-if="isHuaweiForm">
          <a-form-item label="App ID" required>
            <a-input
              v-model:value="notifyModal.form.app_id"
              :maxlength="32"
              placeholder="AGC 应用的 App ID，如 119672293"
              autocomplete="off"
            />
          </a-form-item>
          <a-form-item label="Client ID">
            <a-input
              v-model:value="notifyModal.form.client_id"
              :maxlength="256"
              :placeholder="notifyModal.id ? '留空表示不修改已保存的 Client ID' : 'AGC 后台应用的 OAuth Client ID'"
              autocomplete="off"
            />
            <div v-if="notifyModal.id" class="hint">
              <template v-if="notifyModal.hwClientIdError">
                <span class="key-error">已存的 Client ID 无法解密，请重新填写</span>
              </template>
              <template v-else>当前：{{ notifyModal.hwMaskedClientId || '—' }}。凭据加密落库，页面只显示掩码。</template>
            </div>
            <div v-else class="hint">与下方服务账号 JSON 二选一；两者都填时优先用服务账号。加密落库，页面只显示掩码。</div>
          </a-form-item>
          <a-form-item label="Client Secret">
            <a-input-password
              v-model:value="notifyModal.form.client_secret"
              :maxlength="256"
              :placeholder="notifyModal.id ? '留空表示不修改已保存的 Client Secret' : 'AGC 后台的 OAuth Client Secret'"
              autocomplete="new-password"
            />
            <div v-if="notifyModal.id" class="hint">
              <template v-if="notifyModal.hwClientSecretError">
                <span class="key-error">已存的 Client Secret 无法解密，请重新填写</span>
              </template>
              <template v-else>
                留空表示不改动已保存的密钥（当前{{ notifyModal.hwHasSecret ? '已配置' : '未配置' }}）；
                已存的密钥在页面上删不掉，不用了这个渠道直接删除重建。
              </template>
            </div>
            <div v-else class="hint">与 Client ID 成对填写；加密落库，接口只回「是否已配置」，不回显明文。</div>
          </a-form-item>
          <a-form-item label="服务账号私钥 JSON">
            <a-textarea
              v-model:value="notifyModal.form.service_account"
              :rows="5"
              :maxlength="8192"
              class="hw-sa-input"
              :placeholder="
                notifyModal.id
                  ? '留空表示不修改已保存的服务账号 JSON'
                  : '粘贴 AGC 下发的 *private.json 完整内容（含 key_id / sub_account / private_key）'
              "
            />
            <div v-if="notifyModal.id" class="hint">
              <template v-if="notifyModal.hwServiceAccountError">
                <span class="key-error">已存的服务账号私钥无法解密，请重新粘贴完整 JSON</span>
              </template>
              <template v-else>
                当前{{ notifyModal.hwHasServiceAccount ? '已配置服务账号私钥（JWT 授权）' : '未配置服务账号私钥' }}；
                留空不改动，粘贴新 JSON 即整体替换。
              </template>
            </div>
            <div v-else class="hint">
              与 Client ID/Secret 二选一：粘贴 AGC「服务账号」下载的 private.json 完整内容，
              后端用私钥本地签 RS256 JWT 换 token；加密落库，接口只回「是否已配置」。
            </div>
          </a-form-item>
          <a-form-item label="推送目标" required>
            <a-radio-group v-model:value="notifyModal.form.target_type" class="hw-target-radio">
              <a-radio value="topic">主题 Topic（App 订阅同名主题即可接收）</a-radio>
              <a-radio value="token">设备 Token（定向单设备）</a-radio>
            </a-radio-group>
            <a-input
              v-model:value="notifyModal.form.target"
              :maxlength="512"
              :placeholder="
                notifyModal.form.target_type === 'topic'
                  ? '主题名，如 yangvis'
                  : '设备 Push Token，只推给指定设备'
              "
              autocomplete="off"
            />
            <div class="hint">
              <template v-if="notifyModal.id && notifyModal.form.target_type === 'token'">
                当前：{{ hwDetail(notifyModal.id)?.target || '—' }}（掩码）。留空表示不修改。
              </template>
              <template v-else-if="notifyModal.id && notifyModal.form.target_type === 'topic'">
                主题名不敏感，可直接修改。
              </template>
              <template v-else>主题模式适合广播，所有订阅该主题的设备都会收到；设备 Token 模式只推一台。</template>
            </div>
          </a-form-item>
        </template>

        <a-form-item label="订阅事件">
          <a-checkbox-group
            v-model:value="notifyModal.form.events"
            class="event-group"
            :options="eventOptions"
          />
          <div class="hint">一个事件都不勾＝这个渠道永远不会发消息。</div>
        </a-form-item>
        <a-form-item label="启用">
          <a-switch v-model:checked="notifyModal.form.enabled" />
          <div class="hint">停用只是暂停发送，配置与发送记录都保留。</div>
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="modal.open"
      :title="modal.editing ? '编辑模型' : '新增模型'"
      @ok="submit"
      :confirm-loading="modal.loading"
      ok-text="保存"
      cancel-text="取消"
    >
      <a-form layout="vertical">
        <a-form-item label="用途" required>
          <a-radio-group v-model:value="modal.form.purpose" button-style="solid">
            <a-radio-button value="chat">对话</a-radio-button>
            <a-radio-button value="embedding">向量</a-radio-button>
          </a-radio-group>
          <div class="hint">
            对话模型用于生成回答；向量模型用于知识库检索。两者需分别配置。
          </div>
        </a-form-item>
        <a-form-item label="标题" required>
          <a-input v-model:value="modal.form.title" placeholder="例如：主力对话模型" />
        </a-form-item>
        <a-form-item label="模型名称" required>
          <a-input v-model:value="modal.form.model_name" placeholder="例如：gpt-4o-mini" />
          <div class="hint">
            填服务商要求的模型标识。部分服务商此处要求填部署 / 接入点 ID 而非模型名，填错会导致调用全部失败，保存后请用「测试」验证。
          </div>
        </a-form-item>
        <a-form-item label="模型地址" required>
          <a-input v-model:value="modal.form.base_url" placeholder="https://api.openai.com/v1" />
          <div class="hint">
            兼容 OpenAI 协议的服务地址，通常以 /v1 结尾（火山方舟：https://ark.cn-beijing.volces.com/api/v3）。
            注意：Coding Plan、Agent Plan 等套餐仅限厂商官方工具内使用，其 key 不能在此配置
            （API 调用有封号风险），请使用按量付费的 key。
          </div>
        </a-form-item>
        <a-form-item label="密钥">
          <a-input-password
            v-model:value="modal.form.api_key"
            :placeholder="modal.editing ? '留空则不修改已保存的密钥' : 'sk-...'"
            autocomplete="off"
          />
        </a-form-item>
        <a-form-item v-if="modal.form.purpose === 'chat'" label="思考过程">
          <a-radio-group v-model:value="modal.form.reasoning_mode" button-style="solid">
            <a-radio-button value="auto">自动</a-radio-button>
            <a-radio-button value="on">开启</a-radio-button>
            <a-radio-button value="off">关闭</a-radio-button>
          </a-radio-group>
          <div class="hint">
            自动＝服务商主动返回思维链才显示；开启＝另外按下方的参数向服务商索要思维链并显示；
            关闭＝任何思维链都不显示。只有支持深度思考的模型才有内容，
            普通模型（如各家 flash / turbo 档）本就不产出思维链。
          </div>
        </a-form-item>
        <a-form-item v-if="modal.form.purpose === 'chat'" label="额外请求参数（JSON）">
          <a-textarea
            v-model:value="modal.form.request_extra_text"
            :rows="2"
            :placeholder="EXTRA_PARAMS_PLACEHOLDER"
          />
          <div class="hint">
            仅在「开启」时原样并入请求体。各家「开启思考」的字段名不一样
            （enable_thinking / thinking / reasoning_effort），按服务商文档原样填写，
            保存后用「测试」验证；填错会让这份配置直接调用失败。
          </div>
        </a-form-item>
        <a-form-item v-if="modal.form.purpose === 'chat'" label="采样温度">
          <a-input-number
            v-model:value="modal.form.temperature"
            :min="0"
            :max="100"
            :step="1"
            placeholder="留空＝不设限（由智能体的发散度决定）"
            style="width: 100%"
          />
          <div class="hint">
            0-100 整数，调用时除以 100 传给模型。这里设了就是模型级硬约束，会盖过所有
            智能体的温度，只需在这一处设一次；留空表示模型不设限，由各智能体自己决定。
            像 Kimi 这类「只接受固定温度」的模型在这里填上那个值即可。
          </div>
        </a-form-item>
        <a-form-item v-if="modal.form.purpose === 'chat'" label="图片理解">
          <a-checkbox v-model:checked="modal.form.supports_vision">
            这个模型可以接收图片
          </a-checkbox>
          <div class="hint">
            只有服务商文档明确支持图片输入的视觉模型才勾选。勾选后，对话页才能给这个模型发图片；
            把图片发给纯文本模型会被上游整条拒绝。
          </div>
        </a-form-item>
        <a-form-item label="备注">
          <a-textarea v-model:value="modal.form.remark" :rows="2" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { CloudOutlined, PlusOutlined } from '@ant-design/icons-vue'
import {
  memoryApi,
  netdiskApi,
  notifyApi,
  settingsApi,
  type HuaweiChannelCreatePayload,
  type HuaweiChannelItem,
  type HuaweiChannelUpdatePayload,
  type HuaweiTargetType,
  type MemoryItem,
  type ModelConfig,
  type ModelPurpose,
  type NetdiskStatus,
  type NotifyChannelItem,
  type NotifyChannelPayload,
  type NotifyEventInfo,
  type NotifyKind,
  type NotifyLogItem,
  type ReasoningMode,
  type ReadinessStatus,
} from '@/api'
import { errorText } from '@/utils/errors'
import { fmtDateTime } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const activeTab = ref('models')

const goGateway = () => router.push('/system/ai-gateway')

const models = ref<ModelConfig[]>([])
const loading = ref(false)
const testingId = ref<number | null>(null)
const filterPurpose = ref<'' | ModelPurpose>('')

const status = ref<ReadinessStatus>({
  chat: { ready: false, model: null, config_id: null },
  embedding: { ready: false, model: null, config_id: null },
  kb: { status: 'uninitialized', doc_count: 0, chunk_count: 0, model_mismatch: false },
})

const columns = [
  { title: '用途', key: 'purpose', width: 130 },
  { title: '标题', dataIndex: 'title' },
  { title: '模型名称', dataIndex: 'model_name' },
  { title: '模型地址', dataIndex: 'base_url', ellipsis: true },
  { title: '密钥', key: 'api_key', width: 130 },
  { title: '状态', key: 'state', width: 130 },
  { title: '操作', key: 'action', width: 290 },
]

const emptyForm = () => ({
  id: 0,
  purpose: 'chat' as ModelPurpose,
  title: '',
  model_name: '',
  base_url: '',
  api_key: '',
  remark: '',
  reasoning_mode: 'auto' as ReasoningMode,
  request_extra_text: '',
  temperature: null as number | null,
  supports_vision: false,
})

// 例子放常量里而不是模板上，免得花括号被 Vue 当插值解析。
const EXTRA_PARAMS_PLACEHOLDER = '{"thinking": {"type": "enabled"}}'

const modal = reactive({
  open: false,
  editing: false,
  loading: false,
  form: emptyForm(),
})

function testedAtText(record: ModelConfig): string {
  return record.last_tested_at
    ? `最近测试：${new Date(record.last_tested_at).toLocaleString()}`
    : ''
}

async function load() {
  loading.value = true
  try {
    const [list, readiness] = await Promise.all([
      settingsApi.listModels(filterPurpose.value || undefined),
      settingsApi.status(),
    ])
    models.value = list.items
    status.value = readiness
  } finally {
    loading.value = false
  }
}

function openCreate(purpose: ModelPurpose = 'chat') {
  modal.editing = false
  modal.form = { ...emptyForm(), purpose }
  modal.open = true
}

function openEdit(m: ModelConfig) {
  modal.editing = true
  modal.form = {
    id: m.id,
    purpose: m.purpose,
    title: m.title,
    model_name: m.model_name,
    base_url: m.base_url,
    // 这里故意留空：把掩码原样送回去毫无意义，而留空正好告诉后端
    // 保留已保存的那把密钥。
    api_key: '',
    remark: m.remark || '',
    reasoning_mode: m.reasoning_mode || 'auto',
    request_extra_text: m.request_extra ? JSON.stringify(m.request_extra) : '',
    temperature: m.temperature ?? null,
    supports_vision: !!m.supports_vision,
  }
  modal.open = true
}

async function submit() {
  const { title, model_name, base_url } = modal.form
  if (!title.trim() || !model_name.trim() || !base_url.trim()) {
    message.warning('请填写标题、模型名称、模型地址')
    return
  }

  // 额外参数是原样透传给服务商的，只能是 JSON 对象；填坏了在这里就拦掉，
  // 而不是等调用时让上游报一个看不懂的 400。
  let requestExtra: Record<string, unknown> | null | undefined
  if (modal.form.purpose === 'chat') {
    const raw = modal.form.request_extra_text.trim()
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as unknown
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error('not an object')
        }
        requestExtra = parsed as Record<string, unknown>
      } catch {
        message.warning('额外请求参数要写成 JSON 对象，例如 {"thinking": {"type": "enabled"}}')
        return
      }
    } else {
      requestExtra = modal.editing ? null : undefined
    }
  }

  modal.loading = true
  try {
    const payload = {
      purpose: modal.form.purpose,
      title: title.trim(),
      model_name: model_name.trim(),
      base_url: base_url.trim(),
      remark: modal.form.remark.trim() || null,
      // 编辑时如果密钥留空就整个字段都不传，这样只改其他字段的编辑
      // 不会把已保存的密钥弄丢。
      ...(modal.form.api_key ? { api_key: modal.form.api_key } : {}),
      ...(modal.form.purpose === 'chat'
        ? {
            reasoning_mode: modal.form.reasoning_mode,
            request_extra: requestExtra,
            temperature: modal.form.temperature,
            supports_vision: modal.form.supports_vision,
          }
        : {}),
    }

    if (modal.editing) {
      await settingsApi.updateModel(modal.form.id, payload)
      message.success('已更新，请重新测试连通性')
    } else {
      await settingsApi.createModel(payload)
      message.success('已创建，请点击「测试」验证配置')
    }
    modal.open = false
    await load()
  } finally {
    modal.loading = false
  }
}

async function onTest(record: ModelConfig) {
  testingId.value = record.id
  try {
    const result = await settingsApi.testModel(record.id)
    if (result.success) {
      message.success(result.message)
    } else {
      // 服务商返回的错误往往很长，塞在 toast 里根本看不清。
      Modal.error({ title: '连通性测试失败', content: result.message })
    }
  } catch {
    message.error('测试请求失败，请稍后重试')
  } finally {
    testingId.value = null
    await load()
  }
}

async function onSetDefault(record: ModelConfig) {
  try {
    await settingsApi.setDefault(record.id)
    message.success('已设为默认')
  } catch {
    return // 拦截器已弹 toast。
  }
  await load()
}

async function onDelete(id: number) {
  try {
    await settingsApi.deleteModel(id)
    message.success('已删除')
  } catch {
    return // 拦截器已弹 toast。
  }
  await load()
}

// ---- 百度网盘 -----------------------------------------------------------------
// 绑定是全局能力（简历等模块共用），放在「百度网盘」Tab 维护；configured=false
// （后端没配 AppKey）时只展示一条提示，不渲染操作入口。

const netdisk = reactive<NetdiskStatus>({
  configured: false,
  bound: false,
  baidu_name: null,
  expires_at: null,
})
let netdiskLoaded = false

const bindModal = reactive({
  open: false,
  loading: false,
  authLoading: false,
  code: '',
})

async function loadNetdiskStatus() {
  try {
    Object.assign(netdisk, await netdiskApi.status())
    netdiskLoaded = true
  } catch {
    // 状态拿不到就当作功能未启用，不挡设置页主流程。
    netdisk.configured = false
  }
}

// 网盘状态按需加载：第一次切到该 Tab 才请求。
watch(activeTab, (key) => {
  if (key === 'netdisk' && !netdiskLoaded) {
    loadNetdiskStatus()
  }
  if (key === 'memory' && !memoryLoaded) {
    loadMemories()
  }
  if (key === 'notify' && !notifyLoaded) {
    loadNotify()
  }
})

// ---- 长期记忆 ---------------------------------------------------------------
// 记忆主要由后台任务自动提取，这里提供查看与手动管理入口。

const memories = ref<MemoryItem[]>([])
const memoryLoading = ref(false)
let memoryLoaded = false

const memoryModal = reactive({
  open: false,
  editing: false,
  loading: false,
  id: 0,
  content: '',
})

async function loadMemories() {
  memoryLoading.value = true
  try {
    const data = await memoryApi.list()
    memories.value = data.items
    memoryLoaded = true
  } finally {
    memoryLoading.value = false
  }
}

function openMemoryCreate() {
  memoryModal.editing = false
  memoryModal.id = 0
  memoryModal.content = ''
  memoryModal.open = true
}

function openMemoryEdit(item: MemoryItem) {
  memoryModal.editing = true
  memoryModal.id = item.id
  memoryModal.content = item.content
  memoryModal.open = true
}

async function submitMemory() {
  if (!memoryModal.content.trim()) {
    message.warning('请填写记忆内容')
    return
  }
  memoryModal.loading = true
  try {
    if (memoryModal.editing) {
      await memoryApi.update(memoryModal.id, memoryModal.content.trim())
      message.success('已更新')
    } else {
      await memoryApi.create(memoryModal.content.trim())
      message.success('已添加')
    }
    memoryModal.open = false
    await loadMemories()
  } finally {
    memoryModal.loading = false
  }
}

async function onMemoryDelete(id: number) {
  try {
    await memoryApi.remove(id)
    message.success('已删除')
  } catch {
    return // 拦截器已弹 toast。
  }
  await loadMemories()
}

async function onMemoryClear() {
  try {
    await memoryApi.clear()
    message.success('已清空全部记忆')
  } catch {
    return // 拦截器已弹 toast。
  }
  await loadMemories()
}

// ---- 消息通知 ---------------------------------------------------------------
// 事件词表来自后端而不是这里抄一份：订阅一个后端不认识的事件，结果是
// 「配置看起来没问题但永远收不到消息」，排查完全没有方向。

const KIND_LABELS: Record<NotifyKind, string> = {
  feishu: '飞书',
  wecom: '企业微信',
  dingtalk: '钉钉',
  generic: '自定义',
  huawei: '华为推送',
}
const KIND_OPTIONS = (Object.keys(KIND_LABELS) as NotifyKind[]).map((kind) => ({
  value: kind,
  label: KIND_LABELS[kind],
}))

function kindLabel(kind: NotifyKind): string {
  return KIND_LABELS[kind] ?? kind
}

const notifyChannels = ref<NotifyChannelItem[]>([])
const notifyEvents = ref<NotifyEventInfo[]>([])
const notifyLogs = ref<NotifyLogItem[]>([])
const notifyLoading = ref(false)
const notifyLogLoading = ref(false)
const notifyTestingId = ref<number | null>(null)
let notifyLoaded = false

const notifyColumns = [
  { title: '名称', dataIndex: 'name', width: 150 },
  { title: '类型', key: 'kind', width: 100 },
  // webhook 渠道显示机器人地址，华为渠道显示推送目标（主题/设备掩码）。
  { title: '地址 / 推送目标', key: 'webhook_url', width: 240, ellipsis: true },
  { title: '订阅事件', key: 'events' },
  { title: '启用', key: 'enabled', width: 70 },
  { title: '连通测试', key: 'test', width: 100 },
  { title: '操作', key: 'action', width: 200 },
]

const notifyLogColumns = [
  { title: '时间', key: 'created_at', width: 170 },
  { title: '渠道', dataIndex: 'channel_name', width: 140 },
  { title: '事件', key: 'event', width: 150 },
  { title: '内容', dataIndex: 'title' },
  { title: '结果', key: 'status', width: 90 },
  { title: '耗时', key: 'elapsed', width: 90 },
]

const eventLabels = computed(() =>
  Object.fromEntries(notifyEvents.value.map((event) => [event.key, event.label])),
)

/** 'test' 是连通测试写进发送记录的保留字，不在订阅词表里。 */
function eventLabel(key: string): string {
  if (key === 'test') return '连通测试'
  return eventLabels.value[key] ?? key
}

const eventOptions = computed(() =>
  notifyEvents.value.map((event) => ({ value: event.key, label: event.label })),
)

function notifyTestedAtText(record: NotifyChannelItem): string {
  return record.last_test_at
    ? `最近测试：${new Date(record.last_test_at).toLocaleString()}`
    : ''
}

const emptyNotifyForm = () => ({
  name: '',
  kind: 'feishu' as NotifyKind,
  webhook_url: '',
  secret: '',
  events: [] as string[],
  enabled: true,
  // 华为 HMS Push 渠道字段（kind=huawei 时启用，其余 kind 忽略）。
  app_id: '',
  client_id: '',
  client_secret: '',
  // 服务账号 private.json 完整文本；编辑态留空＝保持已存的 JSON。
  service_account: '',
  target_type: 'topic' as HuaweiTargetType,
  target: '',
})

const notifyModal = reactive({
  open: false,
  loading: false,
  // 0＝新建；编辑时缓存原掩码与「是否已存密钥」，用于输入框提示。
  id: 0,
  form: emptyNotifyForm(),
  maskedUrl: '',
  hadSecret: false,
  // 换过 ENCRYPTION_KEY 的渠道，掩码位置显示的是错误；这时地址必须重填，
  // 否则保存下来仍是一个解不开的渠道。
  urlBroken: false,
  // 华为编辑态：掩码/错误回显，以及 diff 基线（避免未改字段也触发凭据重置）。
  hwMaskedClientId: '',
  hwHasSecret: false,
  hwClientIdError: null as string | null,
  hwClientSecretError: null as string | null,
  // 服务账号私钥只回「有没有 / 解不解得开」，编辑框始终留空等用户粘贴替换。
  hwHasServiceAccount: false,
  hwServiceAccountError: null as string | null,
  hwInitial: null as null | {
    app_id: string
    target_type: HuaweiTargetType
    target: string
  },
})

const notifyNeedsSecret = computed(
  () => notifyModal.form.kind === 'dingtalk' || notifyModal.form.kind === 'feishu',
)
const isHuaweiForm = computed(() => notifyModal.form.kind === 'huawei')

// 列表接口只回 webhook 形状；华为行的推送目标与凭据状态要按 id 补拉详情。
const huaweiDetails = ref(new Map<number, HuaweiChannelItem>())

function hwDetail(id: number): HuaweiChannelItem | undefined {
  return huaweiDetails.value.get(id)
}

function huaweiTargetText(record: NotifyChannelItem): string {
  const detail = huaweiDetails.value.get(record.id)
  if (!detail) return '—'
  // token 模式后端已掩码，可直接展示；topic 名不敏感，明文显示。
  return detail.target_type === 'topic' ? `主题：${detail.target}` : `设备：${detail.target}`
}

// 切换推送目标类型后旧目标值不再适用（主题名 vs 设备 token），清空避免误提交。
watch(
  () => notifyModal.form.target_type,
  (value, oldValue) => {
    if (notifyModal.open && value !== oldValue) notifyModal.form.target = ''
  },
)

async function loadNotify() {
  notifyLoading.value = true
  try {
    const [events, channels] = await Promise.all([notifyApi.events(), notifyApi.listChannels()])
    notifyEvents.value = events.items
    notifyChannels.value = channels.items
    notifyLoaded = true
    // 华为行的目标/掩码不在列表接口里，按行补拉；单个失败不影响整张表。
    const hwRows = channels.items.filter((channel) => channel.kind === 'huawei')
    const details = await Promise.all(
      hwRows.map((channel) => notifyApi.getHuaweiChannel(channel.id, true).catch(() => null)),
    )
    huaweiDetails.value = new Map(
      details
        .filter((detail): detail is HuaweiChannelItem => detail !== null)
        .map((detail) => [detail.id, detail]),
    )
  } finally {
    notifyLoading.value = false
  }
  await loadNotifyLogs()
}

async function loadNotifyLogs() {
  notifyLogLoading.value = true
  try {
    notifyLogs.value = (await notifyApi.logs()).items
  } catch {
    // 记录面板读不到不影响渠道维护，拦截器已弹 toast。
  } finally {
    notifyLogLoading.value = false
  }
}

function resetHwEditState() {
  notifyModal.hwMaskedClientId = ''
  notifyModal.hwHasSecret = false
  notifyModal.hwClientIdError = null
  notifyModal.hwClientSecretError = null
  notifyModal.hwHasServiceAccount = false
  notifyModal.hwServiceAccountError = null
  notifyModal.hwInitial = null
}

function openNotifyCreate() {
  notifyModal.id = 0
  notifyModal.maskedUrl = ''
  notifyModal.hadSecret = false
  notifyModal.urlBroken = false
  resetHwEditState()
  notifyModal.form = {
    ...emptyNotifyForm(),
    events: notifyEvents.value.filter((event) => event.default_on).map((event) => event.key),
  }
  notifyModal.open = true
}

async function openNotifyEdit(channel: NotifyChannelItem) {
  if (channel.kind === 'huawei') {
    // 华为渠道的字段在独立端点：拉详情填表单，掩码只用于提示、不回填输入框。
    const detail = await notifyApi.getHuaweiChannel(channel.id)
    notifyModal.id = channel.id
    notifyModal.maskedUrl = ''
    notifyModal.hadSecret = false
    notifyModal.urlBroken = false
    notifyModal.hwMaskedClientId = detail.client_id_masked
    notifyModal.hwHasSecret = detail.has_client_secret
    notifyModal.hwClientIdError = detail.client_id_error ?? null
    notifyModal.hwClientSecretError = detail.client_secret_error ?? null
    notifyModal.hwHasServiceAccount = detail.has_service_account
    notifyModal.hwServiceAccountError = detail.service_account_error ?? null
    // topic 名不敏感、明文回填可直接编辑；设备 token 是掩码，输入框留空＝保持原样。
    const topicTarget = detail.target_type === 'topic' ? detail.target : ''
    notifyModal.hwInitial = {
      app_id: detail.app_id,
      target_type: detail.target_type,
      target: topicTarget,
    }
    notifyModal.form = {
      ...emptyNotifyForm(),
      kind: 'huawei',
      name: detail.name,
      events: [...detail.events],
      enabled: detail.enabled,
      app_id: detail.app_id,
      target_type: detail.target_type,
      target: topicTarget,
    }
    notifyModal.open = true
    return
  }

  notifyModal.id = channel.id
  notifyModal.maskedUrl = channel.webhook_url
  notifyModal.hadSecret = channel.has_secret
  notifyModal.urlBroken = Boolean(channel.webhook_url_error)
  resetHwEditState()
  notifyModal.form = {
    ...emptyNotifyForm(),
    name: channel.name,
    kind: channel.kind,
    // 与模型密钥同一处理：把掩码原样送回毫无意义，留空才是告诉后端保留已存的地址。
    events: [...channel.events],
    enabled: channel.enabled,
  }
  notifyModal.open = true
}

async function submitNotifyChannel() {
  const name = notifyModal.form.name.trim()
  if (!name) {
    message.warning('请填写渠道名称')
    return
  }
  if (isHuaweiForm.value) {
    await submitHuaweiChannel(name)
    return
  }

  const url = notifyModal.form.webhook_url.trim()
  const secret = notifyModal.form.secret.trim()
  if ((!notifyModal.id || notifyModal.urlBroken) && !url) {
    message.warning(
      notifyModal.urlBroken
        ? '这个渠道已存的地址解不开（ENCRYPTION_KEY 换过），请重新粘贴完整地址'
        : '请填写 Webhook 地址',
    )
    return
  }

  const payload: NotifyChannelPayload = {
    name,
    kind: notifyModal.form.kind,
    events: notifyModal.form.events,
    enabled: notifyModal.form.enabled,
  }
  if (url) payload.webhook_url = url
  if (secret) payload.secret = secret

  notifyModal.loading = true
  try {
    if (notifyModal.id) {
      await notifyApi.updateChannel(notifyModal.id, payload)
      message.success('已更新')
    } else {
      await notifyApi.createChannel(payload)
      message.success('已创建，建议点「测试」确认群里能收到')
    }
    notifyModal.open = false
    await loadNotify()
  } finally {
    notifyModal.loading = false
  }
}

async function submitHuaweiChannel(name: string) {
  const form = notifyModal.form
  const isEdit = Boolean(notifyModal.id)
  const appId = form.app_id.trim()
  const clientId = form.client_id.trim()
  const clientSecret = form.client_secret.trim()
  const serviceAccount = form.service_account.trim()
  const target = form.target.trim()

  if (!appId) {
    message.warning('请填写 App ID')
    return
  }
  // 新建时两种 OAuth 凭据二选一；编辑态留空＝保持已存值。
  if (!isEdit) {
    const hasPair = Boolean(clientId && clientSecret)
    if (!hasPair && !serviceAccount) {
      message.warning('Client ID/Secret 与服务账号私钥 JSON 至少填写一种')
      return
    }
    if ((clientId && !clientSecret) || (!clientId && clientSecret)) {
      message.warning('Client ID 与 Client Secret 必须成对填写，或改用服务账号私钥 JSON')
      return
    }
    if (!target) {
      message.warning(form.target_type === 'topic' ? '请填写订阅主题名' : '请填写设备 Push Token')
      return
    }
  } else if (
    notifyModal.hwInitial &&
    form.target_type !== notifyModal.hwInitial.target_type &&
    !target
  ) {
    // 切换目标类型会清空旧目标（主题名与设备 token 不通用），不重填就保存会错配。
    message.warning('切换推送目标类型后，请重新填写目标（主题名或设备 Token）')
    return
  }

  notifyModal.loading = true
  try {
    if (!isEdit) {
      const payload: HuaweiChannelCreatePayload = {
        name,
        app_id: appId,
        target_type: form.target_type,
        target,
        events: form.events,
        enabled: form.enabled,
      }
      // 为空的字段直接不传，后端按缺失处理，避免发出空串凭据。
      if (clientId) payload.client_id = clientId
      if (clientSecret) payload.client_secret = clientSecret
      if (serviceAccount) payload.service_account = serviceAccount
      await notifyApi.createHuaweiChannel(payload)
      message.success('已创建，建议点「测试」确认手机能收到')
    } else {
      // 只传变化的字段：app_id/target_type/target 未改就不传，否则后端会把
      // 它们计为凭据变更，无谓地作废旧的连通测试结论。
      const initial = notifyModal.hwInitial
      const payload: HuaweiChannelUpdatePayload = {
        name,
        events: form.events,
        enabled: form.enabled,
      }
      if (initial && appId !== initial.app_id) payload.app_id = appId
      if (initial && form.target_type !== initial.target_type) {
        payload.target_type = form.target_type
      }
      // topic 基线是明文（未改即相等，不传）；token 基线输入框留空，填了才是新值。
      const baselineTarget = initial?.target_type === 'topic' ? initial.target : ''
      if (target && target !== baselineTarget) payload.target = target
      if (clientId) payload.client_id = clientId
      if (clientSecret) payload.client_secret = clientSecret
      // 服务账号 JSON 没有掩码形态：粘贴了内容就是整体替换，留空则保持已存值。
      if (serviceAccount) payload.service_account = serviceAccount

      await notifyApi.updateHuaweiChannel(notifyModal.id, payload)
      message.success('已更新')
    }
    notifyModal.open = false
    await loadNotify()
  } finally {
    notifyModal.loading = false
  }
}

async function onNotifyToggle(channel: NotifyChannelItem, enabled: boolean) {
  try {
    await notifyApi.updateChannel(channel.id, { enabled })
    message.success(enabled ? '已启用' : '已停用')
  } catch {
    return // 拦截器已弹 toast。
  }
  await loadNotify()
}

async function onNotifyTest(channel: NotifyChannelItem) {
  notifyTestingId.value = channel.id
  try {
    const result = await notifyApi.testChannel(channel.id)
    if (result.success) {
      message.success(result.message)
    } else {
      // 机器人那边的报错往往带一整段返回体，toast 里看不全。
      Modal.error({ title: '测试发送失败', content: result.message })
    }
  } catch {
    message.error('测试请求失败，请稍后重试')
  } finally {
    notifyTestingId.value = null
    await loadNotify()
  }
}

async function onNotifyDelete(id: number) {
  try {
    await notifyApi.removeChannel(id)
    message.success('已删除')
  } catch {
    return // 拦截器已弹 toast。
  }
  await loadNotify()
}

function openBindNetdisk() {
  bindModal.code = ''
  bindModal.open = true
}

async function openAuthPage() {
  bindModal.authLoading = true
  try {
    const { url } = await netdiskApi.authUrl()
    window.open(url, '_blank', 'noopener')
  } catch (err) {
    message.error(errorText(err, '获取授权地址失败'))
  } finally {
    bindModal.authLoading = false
  }
}

async function submitBindNetdisk() {
  if (!bindModal.code.trim()) {
    message.warning('请先粘贴授权码')
    return
  }
  bindModal.loading = true
  try {
    const status = await netdiskApi.bind(bindModal.code.trim())
    Object.assign(netdisk, status)
    message.success('网盘绑定成功')
    bindModal.open = false
  } catch (err) {
    message.error(errorText(err, '绑定失败'))
  } finally {
    bindModal.loading = false
  }
}

async function onUnbindNetdisk() {
  try {
    await netdiskApi.unbind()
    netdisk.bound = false
    netdisk.baidu_name = null
    message.success('已解绑（网盘里的文件未受影响）')
  } catch (err) {
    message.error(errorText(err, '解绑失败'))
  }
}

onMounted(load)
</script>

<style scoped>
.gate-alert {
  margin-bottom: 12px;
}
/* flex 基底用全局 .toolbar（style.css），这里只留本页差异。 */
/* 打码后的 API key 用等宽显示；颜色/字号继承全局 .muted（style.css）。 */
.muted {
  font-family: var(--font-mono);
}
.key-error {
  color: var(--danger);
  font-size: var(--fs-xs);
}
/* 颜色/字号/行高用全局 .hint（style.css），这里只留本页差异。 */
.hint {
  margin-top: 4px;
}
.netdisk-tag {
  margin-inline-end: 0;
}
.bind-steps {
  margin: 0 0 12px;
  padding-left: 18px;
  line-height: 1.9;
  color: var(--text-2);
}
.bind-code-input {
  margin-top: 12px;
}
.bind-tip {
  margin-top: 10px;
}
/* 订阅事件竖排：四个事件名各带一句说明，横排在 520px 弹窗里会挤成两行。 */
.event-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
/* 华为推送目标两个选项文案都长，横排会换行错位，竖排与输入框对齐。 */
.hw-target-radio {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 8px;
}
/* 服务账号 JSON 是整段粘贴的私钥文件，等宽字体便于发现换行/截断问题。 */
.hw-sa-input textarea {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
}
.notify-logs-head {
  margin-top: 20px;
}
</style>
