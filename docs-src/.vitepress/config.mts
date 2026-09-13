import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

export default withMermaid(
  defineConfig({
    lang: 'zh-CN',
    title: 'RiyaBot',
    description: '一个基于大语言模型、长期记忆、行为规划和插件系统的拟生命体聊天机器人',
    base: '/riyabot/',
    lastUpdated: true,
    cleanUrls: true,
    head: [
      ['link', { rel: 'icon', type: 'image/svg+xml', href: '/riyabot/logo.svg' }],
    ],

    themeConfig: {
      logo: '/logo.svg',

      nav: [
        { text: '指南', link: '/guide/installation' },
        { text: '插件开发', link: '/plugins/index' },
        { text: '贡献指南', link: '/CONTRIBUTE' },
        {
          text: '仓库',
          link: 'https://github.com/hsd221/riyabot',
        },
      ],

      sidebar: {
        '/guide/': [
          {
            text: '安装与使用',
            items: [
              { text: '源码安装', link: '/guide/installation' },
              { text: 'Docker 安装', link: '/guide/docker-installation' },
              { text: '配置说明', link: '/guide/configuration' },
              { text: '插件市场', link: '/guide/plugin-market' },
              { text: '部署指南', link: '/guide/deployment' },
              { text: '架构概览', link: '/guide/architecture' },
              { text: '审计修复兼容说明', link: '/guide/audit-remediation' },
            ],
          },
        ],
        '/plugins/': [
          {
            text: '入门',
            items: [
              { text: '插件文档总览', link: '/plugins/index' },
              { text: '快速开始', link: '/plugins/quick-start' },
            ],
          },
          {
            text: '组件详解',
            items: [
              { text: 'Tool 组件', link: '/plugins/tool-components' },
              { text: 'Command 组件', link: '/plugins/command-components' },
              { text: '配置文件系统', link: '/plugins/configuration-guide' },
              { text: 'Manifest 系统', link: '/plugins/manifest-guide' },
              { text: '依赖管理', link: '/plugins/dependency-management' },
            ],
          },
          {
            text: 'API 参考',
            items: [
              { text: '发送 API', link: '/plugins/api/send-api' },
              { text: '消息 API', link: '/plugins/api/message-api' },
              { text: '聊天流 API', link: '/plugins/api/chat-api' },
              { text: 'LLM API', link: '/plugins/api/llm-api' },
              { text: '回复生成器 API', link: '/plugins/api/generator-api' },
              { text: '表情包 API', link: '/plugins/api/emoji-api' },
              { text: '人物信息 API', link: '/plugins/api/person-api' },
              { text: '数据库 API', link: '/plugins/api/database-api' },
              { text: '配置 API', link: '/plugins/api/config-api' },
              { text: '插件管理 API', link: '/plugins/api/plugin-manage-api' },
              { text: '组件管理 API', link: '/plugins/api/component-manage-api' },
              { text: '日志 API', link: '/plugins/api/logging-api' },
              { text: '工具 API', link: '/plugins/api/tool-api' },
            ],
          },
        ],
      },

      socialLinks: [
        { icon: 'github', link: 'https://github.com/hsd221/riyabot' },
      ],

      docFooter: {
        prev: '上一页',
        next: '下一页',
      },

      editLink: {
        pattern: 'https://github.com/hsd221/riyabot/edit/dev/docs-src/:path',
        text: '在 GitHub 上编辑此页',
      },

      search: {
        provider: 'local',
      },

      externalLinkIcon: true,

      notFound: {
        title: '页面不存在',
        quote: '你访问的页面可能已经移动，或者链接仍指向旧地址。',
        linkLabel: '返回首页',
        linkText: '回到文档首页',
      },

      outline: {
        label: '本页目录',
      },

      lastUpdatedText: '最后更新',

      returnToTopLabel: '返回顶部',
      sidebarMenuLabel: '菜单',
      langMenuLabel: '切换语言',
      skipToContentLabel: '跳转到正文',
      darkModeSwitchLabel: '外观',
      lightModeSwitchTitle: '切换到浅色模式',
      darkModeSwitchTitle: '切换到深色模式',

      footer: {
        message: '基于 GPL-3.0 许可发布，fork 自 MaiBot/MaiCore',
        copyright: `Copyright © ${new Date().getFullYear()} @hsd221`,
      },
    },
  }),
)
