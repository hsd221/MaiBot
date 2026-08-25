import { describe, expect, it } from 'bun:test'
import type { PluginInfo } from '../src/types/plugin'
import {
  compareSemanticVersions,
  getDefaultMarketVersion,
  getInstalledMarketRisk,
  hasSelectableMarketVersion,
  hasMarketUpdate,
  isMarketInstallation,
  mergePluginSources,
  matchesPluginCategory,
  matchesPluginCompatibilityPreference,
  resolveInstalledPluginSource,
} from '../src/lib/plugin-market-ui'

const runtimeVersion = {
  version: '0.14.0',
  version_major: 0,
  version_minor: 14,
  version_patch: 0,
}

function marketPlugin(overrides: Partial<PluginInfo> = {}): PluginInfo {
  return {
    id: 'github.alice.weather',
    manifest: {
      manifest_version: 2,
      name: '天气插件',
      version: '1.3.0',
      description: '查询天气',
      author: { name: 'Alice' },
      license: 'MIT',
      host_application: { min_version: '0.14.0' },
      repository_url: 'https://github.com/alice/weather',
      keywords: [],
      categories: [],
      default_locale: 'zh-CN',
    },
    downloads: 0,
    rating: 0,
    review_count: 0,
    installed: true,
    installed_version: '1.2.3',
    published_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-09T00:00:00Z',
    market_registry_url: 'https://plugins.riyabot.example/registry.json',
    installation: {
      install_method: 'market',
      installed_version: '1.2.3',
      registry_url: 'https://plugins.riyabot.example/registry.json',
      repository_url: 'https://github.com/alice/weather',
      source_ref: 'v1.2.3',
      source_commit: 'a'.repeat(40),
      updated_at: '2026-08-09T00:00:00Z',
    },
    market_versions: [
      {
        version: '1.4.0-beta.1',
        ref: 'v1.4.0-beta.1',
        commit: 'd'.repeat(40),
        status: 'approved',
        released_at: '2026-08-10T00:00:00Z',
        stable: false,
        installable: true,
        host_application: { min_version: '0.14.0' },
      },
      {
        version: '1.3.0',
        ref: 'v1.3.0',
        commit: 'b'.repeat(40),
        status: 'approved',
        released_at: '2026-08-09T00:00:00Z',
        stable: true,
        installable: true,
        host_application: { min_version: '0.15.0' },
      },
      {
        version: '1.2.3',
        ref: 'v1.2.3',
        commit: 'a'.repeat(40),
        status: 'approved',
        released_at: '2026-08-01T00:00:00Z',
        stable: true,
        installable: true,
        host_application: { min_version: '0.14.0' },
      },
      {
        version: '1.1.0',
        ref: 'v1.1.0',
        commit: 'c'.repeat(40),
        status: 'yanked',
        released_at: '2026-07-01T00:00:00Z',
        stable: true,
        installable: false,
        host_application: { min_version: '0.13.0' },
      },
    ],
    ...overrides,
  }
}

describe('plugin market UI decisions', () => {
  it('uses the newest installable version compatible with the running host', () => {
    expect(getDefaultMarketVersion(marketPlugin(), runtimeVersion)?.version).toBe('1.2.3')
  })

  it('keeps a plugin discoverable when only a reviewed prerelease is selectable', () => {
    const prereleaseOnly = marketPlugin({
      installed: false,
      installation: null,
      market_versions: [marketPlugin().market_versions![0]],
    })

    expect(getDefaultMarketVersion(prereleaseOnly, runtimeVersion)).toBeNull()
    expect(hasSelectableMarketVersion(prereleaseOnly, runtimeVersion)).toBe(true)
  })

  it('reports updates only for installations bound to the market', () => {
    const plugin = marketPlugin({
      market_versions: [
        {
          ...marketPlugin().market_versions![1],
          host_application: { min_version: '0.14.0' },
        },
      ],
    })

    expect(hasMarketUpdate(plugin, runtimeVersion)).toBe(true)
    expect(
      hasMarketUpdate(
        {
          ...plugin,
          installation: { ...plugin.installation!, install_method: 'git' },
        },
        runtimeVersion
      )
    ).toBe(false)
    expect(
      hasMarketUpdate(
        {
          ...plugin,
          market_versions: [marketPlugin().market_versions![0]],
        },
        runtimeVersion
      )
    ).toBe(false)
    expect(isMarketInstallation(plugin)).toBe(true)
    expect(
      isMarketInstallation({
        ...plugin,
        market_registry_url: 'https://other.example/registry.json',
      })
    ).toBe(false)
    expect(isMarketInstallation({ ...plugin, installation: null })).toBe(false)
  })

  it('orders stable releases after prereleases and ignores build metadata', () => {
    expect(compareSemanticVersions('1.3.0', '1.3.0-beta.2')).toBeGreaterThan(0)
    expect(compareSemanticVersions('1.3.0-beta.11', '1.3.0-beta.2')).toBeGreaterThan(0)
    expect(compareSemanticVersions('1.3.0+build.2', '1.3.0+build.1')).toBe(0)
    expect(compareSemanticVersions('1.3.0-invalid suffix', '1.2.0')).toBe(0)
  })

  it('selects equal-precedence builds by release time then version and ref', () => {
    const baseRelease = marketPlugin().market_versions![2]
    const olderBuild = {
      ...baseRelease,
      version: '1.2.3+build.2',
      ref: 'v1.2.3-build.2',
      released_at: '2026-08-08T00:00:00Z',
    }
    const newerBuild = {
      ...baseRelease,
      version: '1.2.3+build.1',
      ref: 'v1.2.3-build.1',
      released_at: '2026-08-09T00:00:00Z',
    }

    expect(
      getDefaultMarketVersion(
        marketPlugin({ market_versions: [olderBuild, newerBuild] }),
        runtimeVersion
      )?.version
    ).toBe('1.2.3+build.1')

    const sameReleaseBuild = { ...newerBuild, released_at: olderBuild.released_at }
    expect(
      getDefaultMarketVersion(
        marketPlugin({ market_versions: [sameReleaseBuild, olderBuild] }),
        runtimeVersion
      )?.version
    ).toBe('1.2.3+build.2')
  })

  it('maps documented category aliases to the available filters', () => {
    expect(matchesPluginCategory(['Utility'], 'Utility Tools')).toBe(true)
    expect(matchesPluginCategory(['Entertainment'], 'Entertainment & Interaction')).toBe(true)
    expect(matchesPluginCategory(['Other'], 'Other')).toBe(true)
    expect(matchesPluginCategory(['Custom Category'], 'Other')).toBe(true)
    expect(matchesPluginCategory([], 'Other')).toBe(true)
  })

  it('uses only filesystem-verified installation metadata and ignores stale market sources', () => {
    const marketSource = marketPlugin().installation!
    const gitSource = { ...marketSource, install_method: 'git' as const, registry_url: null }

    expect(resolveInstalledPluginSource(true, gitSource)).toEqual(gitSource)
    expect(resolveInstalledPluginSource(true, null)).toBeNull()
    expect(resolveInstalledPluginSource(false, marketSource)).toBeNull()
  })

  it('keeps an old Registry binding without attributing the current market review', () => {
    const currentRegistryPlugin = marketPlugin({
      installed: false,
      installation: null,
      review_level: 'official',
      market_status: 'approved',
    })
    const oldRegistrySource = {
      ...marketPlugin().installation!,
      registry_url: 'https://old-registry.example/registry.json',
    }
    const installedPlugin = {
      id: currentRegistryPlugin.id,
      manifest: {
        ...currentRegistryPlugin.manifest,
        name: '旧市场安装',
        version: '1.2.3',
      },
      path: 'plugins/github_alice_weather',
      installation: oldRegistrySource,
    }

    const [merged] = mergePluginSources([currentRegistryPlugin], [installedPlugin])

    expect(merged.manifest.name).toBe('旧市场安装')
    expect(merged.installation).toEqual(oldRegistrySource)
    expect(merged.review_level).toBeUndefined()
    expect(merged.market_status).toBeUndefined()
    expect(merged.market_versions).toBeUndefined()
  })

  it('does not restore a same-Registry source hidden by backend drift checks', () => {
    const currentRegistryPlugin = marketPlugin({ installed: false, installation: null })
    const installedPlugin = {
      id: currentRegistryPlugin.id,
      manifest: { ...currentRegistryPlugin.manifest, version: '9.9.9' },
      path: 'plugins/github_alice_weather',
      installation: marketPlugin().installation,
    }

    const [merged] = mergePluginSources([currentRegistryPlugin], [installedPlugin])

    expect(merged.installation).toBeNull()
    expect(merged.manifest.version).toBe('9.9.9')
  })

  it('reports plugin blocks and current-version withdrawals for market installations', () => {
    expect(getInstalledMarketRisk(marketPlugin({ market_status: 'blocked' }))).toBe('blocked')

    const withdrawnVersion = marketPlugin({
      market_versions: marketPlugin().market_versions!.map((version) =>
        version.version === '1.2.3' ? { ...version, status: 'yanked', installable: false } : version
      ),
    })
    expect(getInstalledMarketRisk(withdrawnVersion)).toBe('yanked')
    expect(
      getInstalledMarketRisk({
        ...withdrawnVersion,
        installation: { ...withdrawnVersion.installation!, install_method: 'git' },
      })
    ).toBeNull()
  })

  it('keeps installed plugins visible when the compatibility-only filter is enabled', () => {
    expect(matchesPluginCompatibilityPreference(marketPlugin(), true, false)).toBe(true)
    expect(
      matchesPluginCompatibilityPreference(marketPlugin({ installed: false }), true, false)
    ).toBe(false)
    expect(
      matchesPluginCompatibilityPreference(marketPlugin({ installed: false }), false, false)
    ).toBe(true)
  })
})
