/** 智能办公 · BOSS 直聘岗位抓取与缓存列表。 */
import { request } from './http'

export interface JobQuery {
  keyword?: string
  city?: string
  posted_days?: 1 | 3 | 7 | 30
  salary_min?: number
  salary_max?: number
}

export interface JobItem {
  id: number
  source: string
  title: string
  company: string
  city: string
  district: string | null
  salary_text: string
  salary_min: number | null
  salary_max: number | null
  experience: string | null
  education: string | null
  skills: string[]
  recruiter: string | null
  published_at: string | null
  apply_url: string
  first_seen_at: string
  last_seen_at: string
}

export interface JobFetchResult {
  fetched: number
  matched: number
  created: number
  updated: number
  fetched_at: string
}

export const JOB_CITIES = [
  '全国', '北京', '上海', '天津', '重庆', '广州', '深圳', '杭州', '南京', '苏州',
  '成都', '武汉', '西安', '长沙', '郑州', '合肥', '福州', '厦门', '济南', '青岛',
  '东莞', '佛山', '宁波', '无锡', '大连', '沈阳',
] as const

export const jobsApi = {
  list: (query: JobQuery, page: number, pageSize: number) =>
    request<{ items: JobItem[]; total: number }>({
      url: '/office/jobs',
      params: { ...query, page, page_size: pageSize },
    }),
  fetch: (query: JobQuery) =>
    request<JobFetchResult>({ url: '/office/jobs/fetch', method: 'POST', data: query }),
}
