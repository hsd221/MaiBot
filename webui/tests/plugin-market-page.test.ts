import { describe, expect, it } from 'bun:test'

const pluginPage = await Bun.file(new URL('../src/routes/plugins.tsx', import.meta.url)).text()
const versionDialog = await Bun.file(
  new URL('../src/components/plugin-market-version-dialog.tsx', import.meta.url)
).text()
const pluginApi = await Bun.file(new URL('../src/lib/plugin-api.ts', import.meta.url)).text()

describe('plugin market page contract', () => {
  it('uses only the RiyaBot Registry projection for marketplace operations', () => {
    const migratedSources = `${pluginPage}\n${pluginApi}`

    for (const legacyReference of [
      'Mai-with-u',
      'plugin_details.json',
      'maibot-plugin-stats',
      'recordPluginDownload',
      'PluginStats',
    ]) {
      expect(migratedSources).not.toContain(legacyReference)
    }

    expect(pluginPage).not.toMatch(/\binstallPlugin\b/)
    expect(pluginPage).not.toMatch(/\bupdatePlugin\b/)
    expect(pluginPage).toContain('installMarketPlugin')
    expect(pluginPage).toContain('updateMarketPlugin')
    expect(pluginPage).toContain('getBoundMarketPlugin')
    expect(pluginPage).toContain('fetchPluginSources')
  })

  it('renders reviewed versions and installation-source metadata', () => {
    const renderedSources = `${pluginPage}\n${versionDialog}`

    expect(renderedSources).toContain('market_versions')
    expect(renderedSources).toContain('version.installable')
    expect(pluginPage).toContain('installation.install_method')
    expect(pluginPage).toContain('installation.registry_url')
    expect(pluginPage).toContain('installation.source_ref')
    expect(pluginPage).toContain('hasVerifiedMarketInstallation')
    expect(pluginPage).toContain('加载审核版本')
    expect(pluginPage).toContain('getInstalledMarketRisk')
    expect(pluginPage).toContain("marketRisk ? 'inline-flex' : 'hidden md:inline-flex'")
    expect(pluginPage).toContain('插件已封禁')
    expect(pluginPage).toContain('当前版本已撤回')
    expect(pluginPage).toContain('mergePluginSources(marketPlugins, installedPlugins)')
  })

  it('labels both responsive plugin search fields', () => {
    expect(pluginPage).toContain('id="plugin-search-desktop"')
    expect(pluginPage).toContain('name="plugin-search-desktop"')
    expect(pluginPage).toContain('id="plugin-search-mobile"')
    expect(pluginPage).toContain('name="plugin-search-mobile"')
    expect(pluginPage.match(/aria-label="搜索插件"/g)).toHaveLength(2)
  })

  it('connects the category filter and tabs to accessible names and content', () => {
    expect(pluginPage).toContain('aria-label="插件分类"')
    expect(pluginPage).toContain('aria-controls="plugin-market-results"')
    expect(pluginPage).toContain('id="plugin-market-results"')
    expect(pluginPage).toContain('role="tabpanel"')
  })

  it('keeps lifecycle failures separate from catalog loading failures', () => {
    expect(pluginPage).toContain("progress.operation === 'fetch'")
    expect(pluginPage).toContain("title: '插件列表刷新失败'")

    const closeDialog = pluginPage.indexOf(
      'setVersionPlugin(null)',
      pluginPage.indexOf('handleMarketVersion')
    )
    const refresh = pluginPage.indexOf('await refreshAfterPluginMutation()', closeDialog)
    expect(closeDialog).toBeGreaterThan(-1)
    expect(refresh).toBeGreaterThan(closeDialog)
  })

  it('ignores a bound Registry response after the details dialog is closed', () => {
    expect(pluginPage).toContain('const versionRequestIdRef = useRef(0)')
    expect(pluginPage).toContain('versionRequestIdRef.current += 1')
    expect(pluginPage).toContain('const versionRequestId = ++versionRequestIdRef.current')

    const load = pluginPage.indexOf('versionSource = await getBoundMarketPlugin(plugin.id)')
    const staleGuard = pluginPage.indexOf(
      'if (versionRequestId !== versionRequestIdRef.current) return',
      load
    )
    const openDialog = pluginPage.indexOf('setVersionPlugin(versionSource)', staleGuard)
    expect(load).toBeGreaterThan(-1)
    expect(staleGuard).toBeGreaterThan(load)
    expect(openDialog).toBeGreaterThan(staleGuard)
  })

  it('commits only the latest plugin snapshot and ignores stale retry results', () => {
    expect(pluginPage).toContain('const snapshotRequestIdRef = useRef(0)')

    const refresh = pluginPage.indexOf('const refreshPluginData = async () =>')
    const requestId = pluginPage.indexOf(
      'const snapshotRequestId = ++snapshotRequestIdRef.current',
      refresh
    )
    const load = pluginPage.indexOf('const snapshot = await loadPluginSnapshot()', requestId)
    const staleGuard = pluginPage.indexOf(
      'if (snapshotRequestId !== snapshotRequestIdRef.current) return null',
      load
    )
    const commit = pluginPage.indexOf('setPlugins(snapshot.plugins)', load)

    expect(refresh).toBeGreaterThan(-1)
    expect(requestId).toBeGreaterThan(refresh)
    expect(load).toBeGreaterThan(requestId)
    expect(staleGuard).toBeGreaterThan(load)
    expect(commit).toBeGreaterThan(staleGuard)

    const retry = pluginPage.slice(
      pluginPage.indexOf('const retryMarketData = async () =>'),
      pluginPage.indexOf('const refreshAfterPluginMutation = async () =>')
    )
    expect(retry).toContain('if (snapshot === null) return')
  })

  it('keeps local plugin management available when the Registry is unavailable', () => {
    expect(pluginPage).toContain('fetchPluginSources')
    expect(pluginPage).toContain('marketError')
    expect(pluginPage).toContain('Registry 暂时不可用')
  })

  it('validates local author links before rendering an external anchor', () => {
    expect(pluginPage).toContain('getSafeExternalUrl')
    expect(pluginPage).toContain('href={selectedAuthorUrl.href}')
    expect(pluginPage).toContain('aria-label="打开作者主页"')
    expect(pluginPage).not.toContain('href={selectedPlugin.manifest.author.url}')
  })
})
