/**
 * 账号域：登录、验证码、改密与「查看自己」，以及仅管理员的用户增删改查。
 * 原文件的两节合进一个文件：同一套登录态与用户表的两副面孔，各自都只有几个导出。
 */
import { request } from './http'

// ---- 认证 ------------------------------------------------------------------

export interface UserInfo {
  user_id: number | null
  username: string | null
  nickname: string | null
  email: string | null
  phone: string | null
  project_id: number | null
  user_terminal: string | null
  roles: string[]
  permissions: string[]
  ops_write: boolean
}

export interface LoginResult {
  access_token: string
  user_info: UserInfo | null
}

export interface CaptchaResult {
  captcha_id: string
  /** data URI，可直接喂给 <img>。 */
  image: string
}

export const authApi = {
  captcha: () =>
    request<CaptchaResult>({
      url: '/auth/captcha',
      // 登录页自己处理加载失败（点击图片重试），跳过全局提示。
      skipErrorToast: true,
    }),
  login: (payload: {
    username: string
    password: string
    captcha_id: string
    captcha_code: string
  }) =>
    request<LoginResult>({
      url: '/auth/login',
      method: 'POST',
      data: payload,
      // 登录页会把失败原因内联显示出来，所以这里跳过全局提示。
      skipErrorToast: true,
    }),
  me: () => request<UserInfo>({ url: '/auth/me', skipErrorToast: true }),
  logout: () => request<null>({ url: '/auth/logout', method: 'POST', skipErrorToast: true }),
  changePassword: (payload: { old_password: string; new_password: string }) =>
    request<null>({ url: '/auth/password', method: 'POST', data: payload }),
}

// ---- 用户（仅管理员） ------------------------------------------------------

export interface UserItem {
  id: number
  username: string
  nickname: string | null
  email: string | null
  status: boolean
  is_admin: boolean
  ops_write: boolean
  last_login_at: string | null
  created_at: string
  updated_at: string
}

export const usersApi = {
  list: () => request<{ items: UserItem[]; total: number }>({ url: '/users' }),
  create: (payload: {
    username: string
    password: string
    nickname?: string
    email?: string
    is_admin?: boolean
    ops_write?: boolean
  }) => request<UserItem>({ url: '/users', method: 'POST', data: payload }),
  update: (
    id: number,
    payload: {
      nickname?: string | null
      email?: string | null
      is_admin?: boolean
      ops_write?: boolean
    },
  ) => request<UserItem>({ url: `/users/${id}`, method: 'PUT', data: payload }),
  setStatus: (id: number, status: boolean) =>
    request<UserItem>({ url: `/users/${id}/status`, method: 'PUT', data: { status } }),
  resetPassword: (id: number, newPassword: string) =>
    request<null>({
      url: `/users/${id}/password`,
      method: 'POST',
      data: { new_password: newPassword },
    }),
}
