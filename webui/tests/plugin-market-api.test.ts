import { afterEach, describe, expect, it, mock } from 'bun:test'

mock.module('@/lib/fetch-with-auth', () => ({
  fetchWithAuth: (input: RequestInfo | URL, init?: RequestInit) =>
    globalThis.fetch(input, { ...init, credentials: 'include' }),
  getAuthHeaders: () => ({ 'Content-Type': 'application/json' }),
}))

const {
  fetchPluginSources,
  fetchPluginList,
  getBoundMarketPlugin,
  getInstalledPlugins,
  getInstalledPluginsStrict,
  installMarketPlugin,
  updateMarketPlugin,
} = await import('../src/lib/plugin-api')

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
})

function marketResponse() {
  return {
    registry: {
      name: 'RiyaBot Official Plugin Market',
      url: 'https://plugins.riyabot.example/registry.json',
      repository_url: 'https://github.com/hsd221/RiyaBot-Plugins-Registry',
      fetched_at: '2026-08-09T12:00:00Z',
    },
    plugins: [
      {
        id: 'github.alice.weather',
        name: '天气插件',
        description: '查询天气信息',
        author: { name: 'Alice', url: 'https://github.com/alice' },
        license: 'MIT',
        repository_url: 'https://github.com/alice/riyabot-weather',
        homepage_url: 'https://github.com/alice/riyabot-weather#readme',
        status: 'approved',
        review_level: 'community',
        updated_at: '2026-08-09T00:00:00Z',
        capabilities: ['network'],
        categories: ['Utility'],
        keywords: ['weather'],
        host_application: { min_version: '0.14.0', max_version: null },
        latest_version: '1.2.3',
        versions: [
          {
            version: '1.2.3',
            ref: 'v1.2.3',
            commit: 'a'.repeat(40),
            status: 'approved',
            released_at: '2026-08-08T00:00:00Z',
            stable: true,
            installable: true,
            host_application: { min_version: '0.14.0', max_version: null },
          },
        ],
        installation: {
          install_method: 'market',
          installed_version: '1.2.3',
          registry_url: 'https://plugins.riyabot.example/registry.json',
          repository_url: 'https://github.com/alice/riyabot-weather',
          source_ref: 'v1.2.3',
          source_commit: 'a'.repeat(40),
          updated_at: '2026-08-09T10:00:00Z',
        },
      },
    ],
    pagination: { page: 1, page_size: 200, total: 1, total_pages: 1 },
  }
}

describe('plugin market API', () => {
  it('loads the backend Registry projection and preserves reviewed version metadata', async () => {
    const fetchMock = mock(async () => Response.json(marketResponse()))
    globalThis.fetch = fetchMock as typeof fetch

    const plugins = await fetchPluginList()

    expect(fetchMock.mock.calls[0][0]).toBe('/api/webui/plugins/market?page=1&page_size=200')
    expect(plugins).toHaveLength(1)
    expect(plugins[0].id).toBe('github.alice.weather')
    expect(plugins[0].installed).toBe(true)
    expect(plugins[0].installed_version).toBe('1.2.3')
    expect(plugins[0].review_level).toBe('community')
    expect(plugins[0].market_status).toBe('approved')
    expect(plugins[0].market_registry_url).toBe('https://plugins.riyabot.example/registry.json')
    expect(plugins[0].market_versions?.[0].ref).toBe('v1.2.3')
    expect(plugins[0].installation?.install_method).toBe('market')
  })

  it('loads additional market pages without a concurrent request burst', async () => {
    let activeRequests = 0
    let maximumConcurrency = 0
    const fetchMock = mock(async (input: RequestInfo | URL) => {
      activeRequests += 1
      maximumConcurrency = Math.max(maximumConcurrency, activeRequests)
      await new Promise((resolve) => setTimeout(resolve, 2))

      const page = Number(new URL(String(input), 'https://riyabot.local').searchParams.get('page'))
      const response = marketResponse()
      response.plugins[0].id = `github.alice.weather-${page}`
      response.pagination = { page, page_size: 200, total: 3, total_pages: 3 }
      activeRequests -= 1
      return Response.json(response)
    })
    globalThis.fetch = fetchMock as typeof fetch

    const plugins = await fetchPluginList()

    expect(plugins.map((plugin) => plugin.id)).toEqual([
      'github.alice.weather-1',
      'github.alice.weather-2',
      'github.alice.weather-3',
    ])
    expect(maximumConcurrency).toBe(1)
  })

  it('rejects pages assembled from different Registry snapshots', async () => {
    const fetchMock = mock(async (input: RequestInfo | URL) => {
      const page = Number(new URL(String(input), 'https://riyabot.local').searchParams.get('page'))
      const response = marketResponse()
      response.pagination = { page, page_size: 200, total: 2, total_pages: 2 }
      if (page === 2) response.registry.fetched_at = '2026-08-09T12:00:31Z'
      return Response.json(response)
    })
    globalThis.fetch = fetchMock as typeof fetch

    await expect(fetchPluginList()).rejects.toThrow('插件市场分页快照不一致')
  })

  it('propagates installed-plugin failures to strict market callers', async () => {
    const fetchMock = mock(async () =>
      Response.json({ detail: 'temporary failure' }, { status: 503 })
    )
    globalThis.fetch = fetchMock as typeof fetch

    await expect(getInstalledPluginsStrict()).rejects.toThrow('获取已安装插件列表失败 (503)')
    expect(await getInstalledPlugins()).toEqual([])
  })

  it('keeps installed plugins available when the Registry request fails', async () => {
    const installedPlugin = {
      id: 'local.weather',
      manifest: {
        manifest_version: 1,
        name: '本地天气',
        version: '0.1.0',
        description: '本地安装的插件',
        author: { name: 'Alice' },
        license: 'MIT',
        host_application: { min_version: '0.14.0' },
      },
      path: 'plugins/local_weather',
      installation: null,
    }
    const fetchMock = mock(async (input: RequestInfo | URL) => {
      if (String(input).startsWith('/api/webui/plugins/market?')) {
        return Response.json({ detail: '插件市场暂时不可用' }, { status: 502 })
      }
      return Response.json({ success: true, plugins: [installedPlugin] })
    })
    globalThis.fetch = fetchMock as typeof fetch

    const sources = await fetchPluginSources()

    expect(sources.marketPlugins).toEqual([])
    expect(sources.installedPlugins).toEqual([installedPlugin])
    expect(sources.marketError).toBe('插件市场暂时不可用')
  })

  it('submits only the reviewed market identity and version for install and update', async () => {
    const fetchMock = mock(async () =>
      Response.json({
        success: true,
        message: 'ok',
        old_version: '1.2.3',
        new_version: '1.3.0',
      })
    )
    globalThis.fetch = fetchMock as typeof fetch

    await installMarketPlugin('github.alice.weather', '1.2.3')
    await updateMarketPlugin('github.alice.weather', '1.3.0')

    expect(fetchMock.mock.calls[0][0]).toBe('/api/webui/plugins/market/install')
    expect(fetchMock.mock.calls[0][1]?.body).toBe(
      JSON.stringify({ plugin_id: 'github.alice.weather', version: '1.2.3' })
    )
    expect(fetchMock.mock.calls[1][0]).toBe('/api/webui/plugins/market/update')
    expect(fetchMock.mock.calls[1][1]?.body).toBe(
      JSON.stringify({ plugin_id: 'github.alice.weather', version: '1.3.0' })
    )
  })

  it('loads reviewed versions from the installed plugin bound Registry without a client URL', async () => {
    const response = marketResponse()
    response.registry.url = 'https://old-registry.example/registry.json'
    response.plugins[0].installation!.registry_url = response.registry.url
    const fetchMock = mock(async () =>
      Response.json({ registry: response.registry, plugin: response.plugins[0] })
    )
    globalThis.fetch = fetchMock as typeof fetch

    const plugin = await getBoundMarketPlugin('github.alice.weather')

    expect(fetchMock.mock.calls[0][0]).toBe('/api/webui/plugins/market/github.alice.weather')
    expect(fetchMock.mock.calls[0][1]).toEqual({ credentials: 'include' })
    expect(plugin.market_registry_url).toBe('https://old-registry.example/registry.json')
    expect(plugin.installation?.registry_url).toBe('https://old-registry.example/registry.json')
    expect(plugin.market_versions?.[0].version).toBe('1.2.3')
  })
})
