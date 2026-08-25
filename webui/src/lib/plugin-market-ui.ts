import type {
  HostApplication,
  PluginAuthor,
  PluginInfo,
  PluginInstallationInfo,
  PluginMarketVersion,
} from '../types/plugin'

const SEMVER_PATTERN =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/

const CATEGORY_ALIASES = new Map<string, string>([
  ['entertainment', 'Entertainment & Interaction'],
  ['entertainment & interaction', 'Entertainment & Interaction'],
  ['tools', 'Utility Tools'],
  ['utility', 'Utility Tools'],
  ['utility tools', 'Utility Tools'],
])

const FILTERABLE_CATEGORIES = new Set([
  'Group Management',
  'Entertainment & Interaction',
  'Utility Tools',
  'Content Generation',
  'Multimedia',
  'External Integration',
  'Data Analysis & Insights',
  'Other',
])

interface ParsedSemanticVersion {
  core: [bigint, bigint, bigint]
  prerelease: Array<bigint | string> | null
}

export interface MaimaiVersion {
  version: string
  version_major: number
  version_minor: number
  version_patch: number
}

export interface InstalledPluginSnapshot {
  id: string
  manifest: {
    manifest_version: number
    name: string
    version: string
    description: string
    author: PluginAuthor
    license: string
    host_application: HostApplication
    homepage_url?: string
    repository_url?: string
    keywords?: string[]
    categories?: string[]
    default_locale?: unknown
    locales_path?: unknown
    capabilities?: unknown
    urls?: unknown
    i18n?: unknown
  }
  path: string
  installation?: PluginInstallationInfo | null
}

function parseSemanticVersion(version: string): ParsedSemanticVersion | null {
  const match = SEMVER_PATTERN.exec(version)
  if (!match) return null

  return {
    core: [BigInt(match[1]), BigInt(match[2]), BigInt(match[3])],
    prerelease: match[4]
      ? match[4]
          .split('.')
          .map((identifier) => (/^\d+$/.test(identifier) ? BigInt(identifier) : identifier))
      : null,
  }
}

export function compareSemanticVersions(left: string, right: string): number {
  const leftVersion = parseSemanticVersion(left)
  const rightVersion = parseSemanticVersion(right)
  if (!leftVersion || !rightVersion) return 0

  for (let index = 0; index < leftVersion.core.length; index += 1) {
    if (leftVersion.core[index] > rightVersion.core[index]) return 1
    if (leftVersion.core[index] < rightVersion.core[index]) return -1
  }

  if (leftVersion.prerelease === null) return rightVersion.prerelease === null ? 0 : 1
  if (rightVersion.prerelease === null) return -1

  const length = Math.max(leftVersion.prerelease.length, rightVersion.prerelease.length)
  for (let index = 0; index < length; index += 1) {
    const leftIdentifier = leftVersion.prerelease[index]
    const rightIdentifier = rightVersion.prerelease[index]
    if (leftIdentifier === undefined) return -1
    if (rightIdentifier === undefined) return 1
    if (leftIdentifier === rightIdentifier) continue

    if (typeof leftIdentifier === 'bigint' && typeof rightIdentifier === 'string') return -1
    if (typeof leftIdentifier === 'string' && typeof rightIdentifier === 'bigint') return 1
    return leftIdentifier > rightIdentifier ? 1 : -1
  }

  return 0
}

function compareStrings(left: string, right: string): number {
  if (left > right) return 1
  if (left < right) return -1
  return 0
}

function compareMarketVersions(left: PluginMarketVersion, right: PluginMarketVersion): number {
  const precedence = compareSemanticVersions(left.version, right.version)
  if (precedence !== 0) return precedence

  const leftReleasedAt = Date.parse(left.released_at)
  const rightReleasedAt = Date.parse(right.released_at)
  if (Number.isFinite(leftReleasedAt) && Number.isFinite(rightReleasedAt)) {
    if (leftReleasedAt > rightReleasedAt) return 1
    if (leftReleasedAt < rightReleasedAt) return -1
  } else {
    const releasedAt = compareStrings(left.released_at, right.released_at)
    if (releasedAt !== 0) return releasedAt
  }

  const version = compareStrings(left.version, right.version)
  if (version !== 0) return version
  return compareStrings(left.ref, right.ref)
}

export function isPluginCompatible(
  pluginMinVersion: string,
  pluginMaxVersion: string | undefined,
  maimaiVersion: MaimaiVersion
): boolean {
  const currentVersion = [
    maimaiVersion.version_major,
    maimaiVersion.version_minor,
    maimaiVersion.version_patch,
  ]
  const minimumVersion = pluginMinVersion.split('.').map((part) => Number.parseInt(part, 10) || 0)
  const maximumVersion = pluginMaxVersion
    ? pluginMaxVersion.split('.').map((part) => Number.parseInt(part, 10) || 0)
    : null

  for (let index = 0; index < 3; index += 1) {
    if (currentVersion[index] > (minimumVersion[index] || 0)) break
    if (currentVersion[index] < (minimumVersion[index] || 0)) return false
  }

  if (maximumVersion) {
    for (let index = 0; index < 3; index += 1) {
      if (currentVersion[index] < (maximumVersion[index] || 0)) break
      if (currentVersion[index] > (maximumVersion[index] || 0)) return false
    }
  }

  return true
}

export function isMarketInstallation(plugin: PluginInfo): boolean {
  return (
    hasVerifiedMarketInstallation(plugin) &&
    plugin.installation?.registry_url === plugin.market_registry_url
  )
}

export function hasVerifiedMarketInstallation(plugin: PluginInfo): boolean {
  return (
    plugin.installed &&
    plugin.installation?.install_method === 'market' &&
    plugin.installation.registry_url !== null
  )
}

export type InstalledMarketRisk = 'blocked' | 'yanked'

export function getInstalledMarketRisk(plugin: PluginInfo): InstalledMarketRisk | null {
  if (!isMarketInstallation(plugin)) return null
  if (plugin.market_status === 'blocked') return 'blocked'

  const installedVersion = plugin.installed_version ?? plugin.installation?.installed_version
  const installedRelease = plugin.market_versions?.find(
    (version) => version.version === installedVersion
  )
  if (installedRelease?.status === 'blocked') return 'blocked'
  if (plugin.market_status === 'yanked' || installedRelease?.status === 'yanked') return 'yanked'
  return null
}

export function normalizePluginCategory(category: string): string {
  const trimmed = category.trim()
  return CATEGORY_ALIASES.get(trimmed.toLowerCase()) ?? trimmed
}

export function matchesPluginCategory(
  categories: string[] | undefined,
  selectedCategory: string
): boolean {
  if (selectedCategory === 'all') return true
  const normalized = (categories ?? []).map(normalizePluginCategory)
  if (selectedCategory === 'Other') {
    return (
      normalized.length === 0 ||
      normalized.some((category) => category === 'Other' || !FILTERABLE_CATEGORIES.has(category))
    )
  }
  return normalized.includes(selectedCategory)
}

export function matchesPluginCompatibilityPreference(
  plugin: PluginInfo,
  showCompatibleOnly: boolean,
  compatible: boolean
): boolean {
  return !showCompatibleOnly || plugin.installed || compatible
}

export function resolveInstalledPluginSource(
  installed: boolean,
  marketSource: PluginInstallationInfo | null | undefined,
  localSource?: PluginInstallationInfo | null,
  marketRegistryUrl?: string
): PluginInstallationInfo | null {
  if (!installed) return null
  if (marketSource) return marketSource
  if (!localSource) return null
  if (localSource.install_method === 'market' && localSource.registry_url === marketRegistryUrl) {
    return null
  }
  return localSource
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value ? value : undefined
}

function localPluginInfo(
  installedPlugin: InstalledPluginSnapshot,
  installation: PluginInstallationInfo | null
): PluginInfo {
  const rawUrls =
    installedPlugin.manifest.urls && typeof installedPlugin.manifest.urls === 'object'
      ? (installedPlugin.manifest.urls as Record<string, unknown>)
      : null
  const rawI18n =
    installedPlugin.manifest.i18n && typeof installedPlugin.manifest.i18n === 'object'
      ? (installedPlugin.manifest.i18n as Record<string, unknown>)
      : null
  const capabilities = Array.isArray(installedPlugin.manifest.capabilities)
    ? installedPlugin.manifest.capabilities.filter(
        (capability): capability is string => typeof capability === 'string'
      )
    : undefined

  return {
    id: installedPlugin.id,
    manifest: {
      manifest_version: installedPlugin.manifest.manifest_version || 1,
      name: installedPlugin.manifest.name,
      version: installedPlugin.manifest.version,
      description: installedPlugin.manifest.description || '',
      author: installedPlugin.manifest.author,
      license: installedPlugin.manifest.license || 'Unknown',
      host_application: installedPlugin.manifest.host_application,
      homepage_url: installedPlugin.manifest.homepage_url ?? optionalString(rawUrls?.homepage),
      repository_url:
        installedPlugin.manifest.repository_url ?? optionalString(rawUrls?.repository),
      keywords: installedPlugin.manifest.keywords || [],
      categories: installedPlugin.manifest.categories || [],
      default_locale:
        optionalString(installedPlugin.manifest.default_locale) ??
        optionalString(rawI18n?.default_locale) ??
        'zh-CN',
      locales_path: optionalString(installedPlugin.manifest.locales_path),
    },
    downloads: 0,
    rating: 0,
    review_count: 0,
    installed: true,
    installed_version: installedPlugin.manifest.version,
    published_at: new Date(0).toISOString(),
    updated_at: new Date(0).toISOString(),
    installation,
    ...(capabilities ? { capabilities } : {}),
  }
}

export function mergePluginSources(
  marketPlugins: PluginInfo[],
  installedPlugins: InstalledPluginSnapshot[]
): PluginInfo[] {
  const installedById = new Map(installedPlugins.map((plugin) => [plugin.id, plugin]))
  const marketPluginIds = new Set(marketPlugins.map((plugin) => plugin.id))
  const mergedPlugins = marketPlugins.map((plugin) => {
    const installedPlugin = installedById.get(plugin.id)
    if (!installedPlugin) {
      return { ...plugin, installed: false, installed_version: undefined, installation: null }
    }

    const installation = resolveInstalledPluginSource(
      true,
      plugin.installation,
      installedPlugin.installation,
      plugin.market_registry_url
    )
    const isCurrentMarketInstallation = Boolean(
      installation?.install_method === 'market' &&
      installation.registry_url === plugin.market_registry_url
    )
    if (!isCurrentMarketInstallation) {
      return localPluginInfo(installedPlugin, installation)
    }

    return {
      ...plugin,
      installed: true,
      installed_version: installedPlugin.manifest.version,
      installation,
    }
  })

  for (const installedPlugin of installedPlugins) {
    if (marketPluginIds.has(installedPlugin.id)) continue
    mergedPlugins.push(localPluginInfo(installedPlugin, installedPlugin.installation ?? null))
  }

  return mergedPlugins
}

export function isMarketVersionCompatible(
  version: PluginMarketVersion,
  runtimeVersion: MaimaiVersion | null
): boolean {
  if (!runtimeVersion) return true
  return isPluginCompatible(
    version.host_application.min_version,
    version.host_application.max_version,
    runtimeVersion
  )
}

export function isMarketVersionSelectable(
  version: PluginMarketVersion,
  runtimeVersion: MaimaiVersion | null
): boolean {
  return version.installable && isMarketVersionCompatible(version, runtimeVersion)
}

export function getDefaultMarketVersion(
  plugin: PluginInfo,
  runtimeVersion: MaimaiVersion | null
): PluginMarketVersion | null {
  const selectableVersions = (plugin.market_versions ?? []).filter(
    (version) => version.stable && isMarketVersionSelectable(version, runtimeVersion)
  )
  if (selectableVersions.length === 0) return null

  return selectableVersions.reduce((latest, candidate) =>
    compareMarketVersions(candidate, latest) > 0 ? candidate : latest
  )
}

export function hasSelectableMarketVersion(
  plugin: PluginInfo,
  runtimeVersion: MaimaiVersion | null
): boolean {
  return (plugin.market_versions ?? []).some((version) =>
    isMarketVersionSelectable(version, runtimeVersion)
  )
}

export function hasMarketUpdate(plugin: PluginInfo, runtimeVersion: MaimaiVersion | null): boolean {
  if (!isMarketInstallation(plugin) || !plugin.installed_version) return false

  return (plugin.market_versions ?? []).some(
    (version) =>
      version.stable &&
      isMarketVersionSelectable(version, runtimeVersion) &&
      compareSemanticVersions(version.version, plugin.installed_version!) > 0
  )
}
