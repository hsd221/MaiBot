import { fetchWithAuth, getAuthHeaders } from '@/lib/fetch-with-auth'
import { isPluginCompatible } from './plugin-market-ui'
import type { MaimaiVersion } from './plugin-market-ui'
import type {
  HostApplication,
  PluginInfo,
  PluginInstallationInfo,
  PluginMarketStatus,
  PluginMarketVersion,
} from '@/types/plugin'

export { isPluginCompatible }
export type { MaimaiVersion }

/**
 * Git 安装状态
 */
export interface GitStatus {
  installed: boolean
  version?: string
  path?: string
  error?: string
}

/**
 * 已安装插件信息
 */
export interface InstalledPlugin {
  id: string
  manifest: {
    manifest_version: number
    name: string
    version: string
    description: string
    author: {
      name: string
      url?: string
    }
    license: string
    host_application: {
      min_version: string
      max_version?: string
    }
    homepage_url?: string
    repository_url?: string
    keywords?: string[]
    categories?: string[]
    [key: string]: unknown // 允许其他字段
  }
  path: string
  installation?: PluginInstallationInfo | null
}

/**
 * 插件加载进度
 */
export interface PluginLoadProgress {
  operation: 'idle' | 'fetch' | 'install' | 'uninstall' | 'update'
  stage: 'idle' | 'loading' | 'success' | 'error'
  progress: number // 0-100
  message: string
  error?: string
  plugin_id?: string
  total_plugins: number
  loaded_plugins: number
}

export interface PluginProgressConnection {
  socket: WebSocket
  disconnect: () => void
}

interface PluginMarketCompatibilityResponse {
  min_version: string
  max_version: string | null
}

interface PluginMarketVersionResponse {
  version: string
  ref: string
  commit: string
  status: 'approved' | 'yanked' | 'blocked'
  released_at: string
  stable: boolean
  installable: boolean
  host_application: PluginMarketCompatibilityResponse
}

type PluginMarketInstallationResponse = PluginInstallationInfo

interface PluginMarketItemResponse {
  id: string
  name: string
  description: string
  author: { name: string; url: string | null }
  license: string
  repository_url: string
  homepage_url: string | null
  status: PluginMarketStatus
  review_level: 'community' | 'official'
  updated_at: string
  capabilities: string[]
  categories: string[]
  keywords: string[]
  host_application: PluginMarketCompatibilityResponse
  latest_version: string | null
  versions: PluginMarketVersionResponse[]
  installation: PluginMarketInstallationResponse | null
}

interface PluginMarketRegistryResponse {
  name: string
  url: string
  repository_url: string
  fetched_at: string
}

interface PluginMarketResponse {
  registry: PluginMarketRegistryResponse
  plugins: PluginMarketItemResponse[]
  pagination: {
    page: number
    page_size: number
    total: number
    total_pages: number
  }
}

interface PluginMarketDetailResponse {
  registry: PluginMarketRegistryResponse
  plugin: PluginMarketItemResponse
}

const MARKET_PAGE_SIZE = 200
const MAX_MARKET_PAGES = 50

function normalizeHostCompatibility(value: PluginMarketCompatibilityResponse): HostApplication {
  return {
    min_version: value.min_version,
    ...(value.max_version ? { max_version: value.max_version } : {}),
  }
}

function mapMarketVersion(version: PluginMarketVersionResponse): PluginMarketVersion {
  return {
    ...version,
    host_application: normalizeHostCompatibility(version.host_application),
  }
}

function mapMarketPlugin(item: PluginMarketItemResponse, registryUrl: string): PluginInfo {
  const displayedVersion = item.latest_version ?? item.versions[0]?.version ?? 'unknown'
  const publishedAt = item.versions[item.versions.length - 1]?.released_at ?? item.updated_at
  return {
    id: item.id,
    manifest: {
      manifest_version: 2,
      name: item.name,
      version: displayedVersion,
      description: item.description,
      author: { name: item.author.name, ...(item.author.url ? { url: item.author.url } : {}) },
      license: item.license,
      host_application: normalizeHostCompatibility(item.host_application),
      ...(item.homepage_url ? { homepage_url: item.homepage_url } : {}),
      repository_url: item.repository_url,
      keywords: item.keywords,
      categories: item.categories,
      default_locale: 'zh-CN',
    },
    downloads: 0,
    rating: 0,
    review_count: 0,
    installed: item.installation !== null,
    ...(item.installation ? { installed_version: item.installation.installed_version } : {}),
    published_at: publishedAt,
    updated_at: item.updated_at,
    review_level: item.review_level,
    market_status: item.status,
    market_registry_url: registryUrl,
    market_versions: item.versions.map(mapMarketVersion),
    installation: item.installation,
    capabilities: item.capabilities,
  }
}

async function fetchMarketPage(page: number): Promise<PluginMarketResponse> {
  const response = await fetchWithAuth(
    `/api/webui/plugins/market?page=${page}&page_size=${MARKET_PAGE_SIZE}`
  )
  if (!response.ok) {
    const error = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(error?.detail || `插件市场请求失败 (${response.status})`)
  }
  const result = (await response.json()) as PluginMarketResponse
  if (
    !Array.isArray(result.plugins) ||
    !result.pagination ||
    !result.registry ||
    typeof result.registry.url !== 'string' ||
    typeof result.registry.fetched_at !== 'string'
  ) {
    throw new Error('插件市场响应格式无效')
  }
  return result
}

/** Load the validated projection exposed by the configured backend Registry. */
export async function fetchPluginList(): Promise<PluginInfo[]> {
  const firstPage = await fetchMarketPage(1)
  const totalPages = firstPage.pagination.total_pages
  if (!Number.isInteger(totalPages) || totalPages < 0 || totalPages > MAX_MARKET_PAGES) {
    throw new Error('插件市场分页信息无效')
  }
  const pages = [firstPage]
  for (let page = 2; page <= totalPages; page += 1) {
    const nextPage = await fetchMarketPage(page)
    if (
      nextPage.registry.url !== firstPage.registry.url ||
      nextPage.registry.fetched_at !== firstPage.registry.fetched_at ||
      nextPage.pagination.page !== page ||
      nextPage.pagination.page_size !== firstPage.pagination.page_size ||
      nextPage.pagination.total !== firstPage.pagination.total ||
      nextPage.pagination.total_pages !== totalPages
    ) {
      throw new Error('插件市场分页快照不一致')
    }
    pages.push(nextPage)
  }
  return pages.flatMap((page) =>
    page.plugins.map((plugin) => mapMarketPlugin(plugin, page.registry.url))
  )
}

/** Load reviewed versions from the Registry bound to a verified local installation. */
export async function getBoundMarketPlugin(pluginId: string): Promise<PluginInfo> {
  const response = await fetchWithAuth(`/api/webui/plugins/market/${encodeURIComponent(pluginId)}`)
  if (!response.ok) {
    const error = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(error?.detail || `绑定的插件市场请求失败 (${response.status})`)
  }

  const result = (await response.json()) as PluginMarketDetailResponse
  if (
    !result.registry ||
    typeof result.registry.url !== 'string' ||
    !result.plugin ||
    typeof result.plugin.id !== 'string' ||
    !Array.isArray(result.plugin.versions)
  ) {
    throw new Error('绑定的插件市场响应格式无效')
  }
  return mapMarketPlugin(result.plugin, result.registry.url)
}

/**
 * 检查本机 Git 安装状态
 */
export async function checkGitStatus(): Promise<GitStatus> {
  try {
    const response = await fetchWithAuth('/api/webui/plugins/git-status')

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`)
    }

    return await response.json()
  } catch (error) {
    console.error('Failed to check Git status:', error)
    // 返回未安装状态
    return {
      installed: false,
      error: '无法检测 Git 安装状态',
    }
  }
}

/**
 * 获取麦麦版本信息
 */
export async function getMaimaiVersion(): Promise<MaimaiVersion> {
  try {
    const response = await fetchWithAuth('/api/webui/plugins/version')

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`)
    }

    return await response.json()
  } catch (error) {
    console.error('Failed to get Maimai version:', error)
    // 返回默认版本
    return {
      version: '0.0.0',
      version_major: 0,
      version_minor: 0,
      version_patch: 0,
    }
  }
}

/**
 * 连接插件加载进度 WebSocket
 */
export function connectPluginProgressWebSocket(
  onProgress: (progress: PluginLoadProgress) => void,
  onError?: (error: Event) => void
): PluginProgressConnection {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  const ws = new WebSocket(`${protocol}//${host}/api/webui/ws/plugin-progress`)
  let heartbeat: number | null = null
  let intentionallyClosed = false

  const stopHeartbeat = () => {
    if (heartbeat !== null) {
      window.clearInterval(heartbeat)
      heartbeat = null
    }
  }

  ws.onopen = () => {
    if (intentionallyClosed) {
      ws.close(1000, 'View closed')
      return
    }

    console.log('Plugin progress WebSocket connected')
    heartbeat = window.setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send('ping')
      } else {
        stopHeartbeat()
      }
    }, 30000)
  }

  ws.onmessage = (event) => {
    if (intentionallyClosed) return

    try {
      // 忽略心跳响应
      if (event.data === 'pong') {
        return
      }

      const data = JSON.parse(event.data) as PluginLoadProgress
      onProgress(data)
    } catch (error) {
      console.error('Failed to parse progress data:', error)
    }
  }

  ws.onerror = (error) => {
    if (intentionallyClosed) return

    console.error('Plugin progress WebSocket error:', error)
    onError?.(error)
  }

  ws.onclose = () => {
    stopHeartbeat()
    if (!intentionallyClosed) {
      console.log('Plugin progress WebSocket disconnected')
    }
  }

  return {
    socket: ws,
    disconnect: () => {
      if (intentionallyClosed) return

      intentionallyClosed = true
      stopHeartbeat()
      if (ws.readyState === WebSocket.OPEN) {
        ws.close(1000, 'View closed')
      }
    },
  }
}

/**
 * 获取已安装插件列表
 */
async function fetchInstalledPlugins(): Promise<InstalledPlugin[]> {
  const response = await fetchWithAuth('/api/webui/plugins/installed', {
    headers: getAuthHeaders(),
  })

  if (!response.ok) {
    throw new Error(`获取已安装插件列表失败 (${response.status})`)
  }

  const result = (await response.json()) as {
    success?: boolean
    message?: string
    plugins?: unknown
  }

  if (!result.success) {
    throw new Error(result.message || '获取已安装插件列表失败')
  }
  if (!Array.isArray(result.plugins)) {
    throw new Error('已安装插件响应格式无效')
  }

  return result.plugins as InstalledPlugin[]
}

/** Load installed plugins without hiding backend or response-format failures. */
export async function getInstalledPluginsStrict(): Promise<InstalledPlugin[]> {
  return await fetchInstalledPlugins()
}

export interface PluginSources {
  marketPlugins: PluginInfo[]
  installedPlugins: InstalledPlugin[]
  marketError: string | null
}

/** Keep local plugin management usable when the optional Registry is unavailable. */
export async function fetchPluginSources(): Promise<PluginSources> {
  const [market, installedPlugins] = await Promise.all([
    fetchPluginList()
      .then((marketPlugins) => ({ marketPlugins, marketError: null }))
      .catch((error: unknown) => ({
        marketPlugins: [],
        marketError: error instanceof Error ? error.message : '插件市场暂时不可用',
      })),
    getInstalledPluginsStrict(),
  ])

  return { ...market, installedPlugins }
}

/** Load installed plugins with the legacy empty-list fallback used by the configuration page. */
export async function getInstalledPlugins(): Promise<InstalledPlugin[]> {
  try {
    return await fetchInstalledPlugins()
  } catch (error) {
    console.error('Failed to get installed plugins:', error)
    return []
  }
}

/**
 * 检查插件是否已安装
 */
export function checkPluginInstalled(
  pluginId: string,
  installedPlugins: InstalledPlugin[]
): boolean {
  return installedPlugins.some((p) => p.id === pluginId)
}

/**
 * 获取已安装插件的版本
 */
export function getInstalledPluginVersion(
  pluginId: string,
  installedPlugins: InstalledPlugin[]
): string | undefined {
  const plugin = installedPlugins.find((p) => p.id === pluginId)
  if (!plugin) return undefined

  // 兼容两种格式：新格式有 manifest，旧格式直接有 version
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return plugin.manifest?.version || (plugin as any).version
}

/**
 * 安装插件
 */
export async function installPlugin(
  pluginId: string,
  repositoryUrl: string,
  branch: string = 'main'
): Promise<{ success: boolean; message: string }> {
  const response = await fetchWithAuth('/api/webui/plugins/install', {
    method: 'POST',

    body: JSON.stringify({
      plugin_id: pluginId,
      repository_url: repositoryUrl,
      branch: branch,
    }),
  })

  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || '安装失败')
  }

  return await response.json()
}

/** Install a version already approved by the backend-configured Registry. */
export async function installMarketPlugin(
  pluginId: string,
  version: string
): Promise<{ success: boolean; message: string }> {
  const response = await fetchWithAuth('/api/webui/plugins/market/install', {
    method: 'POST',
    body: JSON.stringify({ plugin_id: pluginId, version }),
  })

  if (!response.ok) {
    const error = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(error?.detail || '市场插件安装失败')
  }

  return (await response.json()) as { success: boolean; message: string }
}

/**
 * 卸载插件
 */
export async function uninstallPlugin(
  pluginId: string
): Promise<{ success: boolean; message: string }> {
  const response = await fetchWithAuth('/api/webui/plugins/uninstall', {
    method: 'POST',

    body: JSON.stringify({
      plugin_id: pluginId,
    }),
  })

  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || '卸载失败')
  }

  return await response.json()
}

/**
 * 更新插件
 */
export async function updatePlugin(
  pluginId: string,
  repositoryUrl: string,
  branch: string = 'main'
): Promise<{ success: boolean; message: string; old_version: string; new_version: string }> {
  const response = await fetchWithAuth('/api/webui/plugins/update', {
    method: 'POST',

    body: JSON.stringify({
      plugin_id: pluginId,
      repository_url: repositoryUrl,
      branch: branch,
    }),
  })

  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || '更新失败')
  }

  return await response.json()
}

/** Move a market installation to another reviewed version, including rollback. */
export async function updateMarketPlugin(
  pluginId: string,
  version: string
): Promise<{ success: boolean; message: string; old_version: string; new_version: string }> {
  const response = await fetchWithAuth('/api/webui/plugins/market/update', {
    method: 'POST',
    body: JSON.stringify({ plugin_id: pluginId, version }),
  })

  if (!response.ok) {
    const error = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(error?.detail || '市场插件更新失败')
  }

  return (await response.json()) as {
    success: boolean
    message: string
    old_version: string
    new_version: string
  }
}

// ============ 插件配置管理 ============

/**
 * 配置字段定义
 */
export interface ConfigFieldSchema {
  name: string
  type: string
  default: unknown
  description: string
  example?: string
  required: boolean
  choices?: unknown[]
  min?: number
  max?: number
  step?: number
  pattern?: string
  max_length?: number
  label: string
  placeholder?: string
  hint?: string
  icon?: string
  hidden: boolean
  disabled: boolean
  order: number
  input_type?: string
  ui_type: string
  rows?: number
  group?: string
  depends_on?: string
  depends_value?: unknown
}

/**
 * 配置节定义
 */
export interface ConfigSectionSchema {
  name: string
  title: string
  description?: string
  icon?: string
  collapsed: boolean
  order: number
  fields: Record<string, ConfigFieldSchema>
}

/**
 * 配置标签页定义
 */
export interface ConfigTabSchema {
  id: string
  title: string
  sections: string[]
  icon?: string
  order: number
  badge?: string
}

/**
 * 配置布局定义
 */
export interface ConfigLayoutSchema {
  type: 'auto' | 'tabs' | 'pages'
  tabs: ConfigTabSchema[]
}

/**
 * 插件配置 Schema
 */
export interface PluginConfigSchema {
  plugin_id: string
  plugin_info: {
    name: string
    version: string
    description: string
    author: string
  }
  sections: Record<string, ConfigSectionSchema>
  layout: ConfigLayoutSchema
  _note?: string
}

/**
 * 获取插件配置 Schema
 */
export async function getPluginConfigSchema(pluginId: string): Promise<PluginConfigSchema> {
  const response = await fetchWithAuth(`/api/webui/plugins/config/${pluginId}/schema`, {
    headers: getAuthHeaders(),
  })

  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || '获取配置 Schema 失败')
  }

  const result = await response.json()

  if (!result.success) {
    throw new Error(result.message || '获取配置 Schema 失败')
  }

  return result.schema
}

/**
 * 获取插件当前配置值
 */
export async function getPluginConfig(pluginId: string): Promise<Record<string, unknown>> {
  const response = await fetchWithAuth(`/api/webui/plugins/config/${pluginId}`, {
    headers: getAuthHeaders(),
  })

  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || '获取配置失败')
  }

  const result = await response.json()

  if (!result.success) {
    throw new Error(result.message || '获取配置失败')
  }

  return result.config
}

/**
 * 更新插件配置
 */
export async function updatePluginConfig(
  pluginId: string,
  config: Record<string, unknown>
): Promise<{ success: boolean; message: string; note?: string }> {
  const response = await fetchWithAuth(`/api/webui/plugins/config/${pluginId}`, {
    method: 'PUT',

    body: JSON.stringify({ config }),
  })

  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || '保存配置失败')
  }

  return await response.json()
}

/**
 * 重置插件配置为默认值
 */
export async function resetPluginConfig(
  pluginId: string
): Promise<{ success: boolean; message: string; backup?: string }> {
  const response = await fetchWithAuth(`/api/webui/plugins/config/${pluginId}/reset`, {
    method: 'POST',
    headers: getAuthHeaders(),
  })

  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || '重置配置失败')
  }

  return await response.json()
}

/**
 * 切换插件启用状态
 */
export async function togglePlugin(
  pluginId: string
): Promise<{ success: boolean; enabled: boolean; message: string; note?: string }> {
  const response = await fetchWithAuth(`/api/webui/plugins/config/${pluginId}/toggle`, {
    method: 'POST',
    headers: getAuthHeaders(),
  })

  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || '切换状态失败')
  }

  return await response.json()
}
