/**
 * 在线简历的前端结构契约，与后端 app/services/resume_builder.py 的
 * default_doc / normalize_doc 严格对齐：保存时后端还会再归一化与消毒一遍，
 * 这里负责让新建的文档第一次提交就是合法形状，也让组件有类型可用。
 */

export type ResumeModuleKey = 'profile' | 'companies' | 'projects' | 'educations' | 'skills'
export type EntryModuleKey = 'companies' | 'projects' | 'educations'

/** 三类经历条目共有的时间段与描述。 */
export interface ResumeTimedEntry {
  start: string
  end: string
  /** 至今：为 true 时结束时间留空。 */
  current: boolean
  description_html: string
}

export interface CompanyEntry extends ResumeTimedEntry {
  cid: string
  company: string
  position: string
  department: string
}

export interface ProjectEntry extends ResumeTimedEntry {
  pid: string
  name: string
  role: string
  department: string
}

export interface EducationEntry extends ResumeTimedEntry {
  eid: string
  school: string
  major: string
  college: string
  degree: string
  study_mode: string
}

export interface ResumeProfile {
  name: string
  gender: string
  /** 求职意向 */
  job_target: string
  phone: string
  email: string
  /** 个人网站（可不带协议，预览时补 https:// 渲染成链接） */
  website: string
  city: string
  birthday: string
  /** 个人介绍：抬头下的一句话招牌，纯文本 */
  intro: string
  /** 个人优势（富文本，详细卖点） */
  summary_html: string
}

export interface ResumeDocData {
  /** 版式：classic 经典 / sidebar 双栏 / minimal 简约。 */
  template: string
  /** 主题配色 key，色板见 THEME_OPTIONS（与后端 THEMES 对齐）。 */
  theme: string
  /** 恰好是五个模块键的一个排列；数组顺序即编辑/导出顺序。 */
  module_order: ResumeModuleKey[]
  profile: ResumeProfile
  companies: CompanyEntry[]
  projects: ProjectEntry[]
  educations: EducationEntry[]
  skills_html: string
}

export const MODULE_KEYS: ResumeModuleKey[] = [
  'profile',
  'companies',
  'projects',
  'educations',
  'skills',
]

export const DEFAULT_MODULE_ORDER: ResumeModuleKey[] = [...MODULE_KEYS]

export const ENTRY_MODULE_KEYS: EntryModuleKey[] = ['companies', 'projects', 'educations']

export const MODULE_LABELS: Record<ResumeModuleKey, string> = {
  profile: '个人信息',
  companies: '工作经历',
  projects: '项目经历',
  educations: '教育经历',
  skills: '专业技能',
}

/** 条目模块 → 它的 id 字段名，与后端 _ENTRY_SPECS 一致。 */
export const ENTRY_ID_FIELD: Record<EntryModuleKey, 'cid' | 'pid' | 'eid'> = {
  companies: 'cid',
  projects: 'pid',
  educations: 'eid',
}

export const DEGREE_OPTIONS = ['高中', '大专', '本科', '硕士', '博士']
export const STUDY_MODE_OPTIONS = ['全日制', '非全日制']

export const TEMPLATE_OPTIONS: { value: string; label: string; hint: string }[] = [
  { value: 'classic', label: '经典', hint: '通栏抬头 · 单栏排布' },
  { value: 'sidebar', label: '双栏名片', hint: '左侧名片栏 · 右侧经历' },
  { value: 'minimal', label: '简约', hint: '克制用色 · 线条分区' },
]

/** 主题配色：accent 主色 / dark 深色底（双栏侧栏）/ soft 浅色底。与后端 THEMES 对齐。 */
export interface ThemeOption {
  value: string
  label: string
  accent: string
  dark: string
  soft: string
}

export const THEME_OPTIONS: ThemeOption[] = [
  { value: 'teal', label: '青碧', accent: '#0a7e86', dark: '#0b5c63', soft: '#e6f4f5' },
  { value: 'navy', label: '藏蓝', accent: '#1f4e79', dark: '#163a5c', soft: '#e9f0f8' },
  { value: 'crimson', label: '酒红', accent: '#9b3a4b', dark: '#732a38', soft: '#f8ebee' },
  { value: 'forest', label: '墨绿', accent: '#2f6b4f', dark: '#214937', soft: '#e9f3ee' },
  { value: 'violet', label: '黛紫', accent: '#6d4c8f', dark: '#51386b', soft: '#f1ecf7' },
  { value: 'amber', label: '琥珀', accent: '#b45309', dark: '#853f07', soft: '#fbf1e2' },
  { value: 'slate', label: '炭灰', accent: '#374151', dark: '#1f2733', soft: '#eef0f3' },
]

/** 生成本地条目 id。整文档保存、无条目级端点，id 只在文档内部寻址。 */
export function newEntryId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // 兜底形状同样满足后端的 ^[A-Za-z0-9_-]{1,64}$。
  return `id-${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`
}

function emptyTimedEntry(): ResumeTimedEntry {
  return { start: '', end: '', current: false, description_html: '' }
}

export function createEntry(kind: EntryModuleKey): CompanyEntry | ProjectEntry | EducationEntry {
  if (kind === 'companies') {
    return { cid: newEntryId(), company: '', position: '', department: '', ...emptyTimedEntry() }
  }
  if (kind === 'projects') {
    return { pid: newEntryId(), name: '', role: '', department: '', ...emptyTimedEntry() }
  }
  return {
    eid: newEntryId(),
    school: '',
    major: '',
    college: '',
    degree: '',
    study_mode: '',
    ...emptyTimedEntry(),
  }
}

export function createEmptyProfile(): ResumeProfile {
  return {
    name: '',
    gender: '',
    website: '',
    job_target: '',
    phone: '',
    email: '',
    city: '',
    birthday: '',
    intro: '',
    summary_html: '',
  }
}

export function createEmptyDoc(): ResumeDocData {
  return {
    template: 'classic',
    theme: 'teal',
    module_order: [...DEFAULT_MODULE_ORDER],
    profile: createEmptyProfile(),
    companies: [],
    projects: [],
    educations: [],
    skills_html: '',
  }
}

export function entryId(kind: EntryModuleKey, entry: { cid?: string; pid?: string; eid?: string }): string {
  return entry[ENTRY_ID_FIELD[kind]] ?? ''
}
