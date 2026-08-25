import {
  Menu,
  Moon,
  Sun,
  Monitor,
  Check,
  ChevronLeft,
  Home,
  Settings,
  LogOut,
  FileText,
  Server,
  Boxes,
  Smile,
  MessageSquare,
  UserCircle,
  FileSearch,
  Package,
  BookOpen,
  Search,
  Sliders,
  Hash,
  BrainCircuit,
  Activity,
  BarChart3,
  LoaderCircle,
  MoreHorizontal,
  X,
  ChevronRight,
  ScanSearch,
  UploadCloud,
} from 'lucide-react'
import { useState, useEffect, useRef } from 'react'
import { Link, useMatchRoute, useRouterState } from '@tanstack/react-router'
import { useTheme, toggleThemeWithTransition } from './use-theme'
import { useAuthGuard } from '@/hooks/use-auth'
import { logout } from '@/lib/fetch-with-auth'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { SearchDialog } from '@/components/search-dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { APP_NAME } from '@/lib/version'
import { openExternalLink } from '@/lib/external-link'
import type { ReactNode, ComponentType } from 'react'
import type { LucideProps } from 'lucide-react'

interface LayoutProps {
  children: ReactNode
}

interface MenuItem {
  icon: ComponentType<LucideProps>
  label: string
  path: string
  tourId?: string
}

interface MenuSection {
  title: string
  items: MenuItem[]
}

type ThemeMode = 'light' | 'dark' | 'system'

const DESKTOP_NAVIGATION_QUERY = '(min-width: 1024px)'
const PROJECT_DOCS_URL = 'https://hsd221.github.io/riyabot/'

const themeOptions: Array<{
  value: ThemeMode
  label: string
  description: string
  icon: ComponentType<LucideProps>
}> = [
  {
    value: 'system',
    label: '跟随系统',
    description: '根据设备外观自动切换',
    icon: Monitor,
  },
  {
    value: 'light',
    label: '浅色模式',
    description: '始终使用浅色外观',
    icon: Sun,
  },
  {
    value: 'dark',
    label: '深色模式',
    description: '始终使用深色外观',
    icon: Moon,
  },
]

const menuIconTileClasses: Record<string, string> = {
  '/': 'ios-symbol-blue',
  '/statistics': 'ios-symbol-blue',
  '/config/bot': 'ios-symbol-purple',
  '/config/modelProvider': 'ios-symbol-green',
  '/config/model': 'ios-symbol-teal',
  '/config/adapter': 'ios-symbol-purple',
  '/resource/emoji': 'ios-symbol-yellow',
  '/resource/expression': 'ios-symbol-orange',
  '/resource/behavior': 'ios-symbol-purple',
  '/resource/chat-history-import': 'ios-symbol-teal',
  '/resource/jargon': 'ios-symbol-pink',
  '/resource/person': 'ios-symbol-blue',
  '/resource/memory': 'ios-symbol-green',
  '/plugins': 'ios-symbol-purple',
  '/plugin-config': 'ios-symbol-teal',
  '/model-traces': 'ios-symbol-teal',
  '/logs': 'ios-symbol-gray',
  '/chat': 'ios-symbol-green',
  '/settings': 'ios-symbol-gray',
}

const PAGE_TITLE_ROUTES = [
  ['/', '首页'],
  ['/statistics', '统计数据'],
  ['/config/bot', '主程序配置'],
  ['/config/modelProvider', 'AI模型厂商配置'],
  ['/config/model', '模型管理与分配'],
  ['/config/adapter', '平台接入'],
  ['/resource/emoji', '表情包管理'],
  ['/resource/expression', '表达方式管理'],
  ['/resource/behavior', '行为学习管理'],
  ['/resource/chat-history-import', '聊天记录学习'],
  ['/resource/jargon', '黑话管理'],
  ['/resource/person', '人物信息管理'],
  ['/resource/memory', '记忆系统概览'],
  ['/plugins', '插件市场'],
  ['/plugin-config', '插件配置'],
  ['/model-traces', '模型请求追踪'],
  ['/logs', '日志查看器'],
  ['/chat', '本地聊天室'],
  ['/settings', '系统设置'],
] as const

export function Layout({ children }: LayoutProps) {
  const { checking } = useAuthGuard() // 检查认证状态

  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [mobileActionsOpen, setMobileActionsOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const [isDesktopNavigation, setIsDesktopNavigation] = useState(
    () => window.matchMedia(DESKTOP_NAVIGATION_QUERY).matches
  )
  const mobileMenuTriggerRef = useRef<HTMLButtonElement>(null)
  const mobileMenuCloseRef = useRef<HTMLButtonElement>(null)
  const mainContentRef = useRef<HTMLElement>(null)
  const { theme, resolvedTheme, setTheme } = useTheme()
  const matchRoute = useMatchRoute()
  const pathname = useRouterState({ select: (state) => state.location.pathname })

  useEffect(() => {
    const mediaQuery = window.matchMedia(DESKTOP_NAVIGATION_QUERY)
    const handleChange = (event: MediaQueryListEvent) => setIsDesktopNavigation(event.matches)

    setIsDesktopNavigation(mediaQuery.matches)
    mediaQuery.addEventListener('change', handleChange)
    return () => mediaQuery.removeEventListener('change', handleChange)
  }, [])

  useEffect(() => {
    if (isDesktopNavigation || !mobileMenuOpen) return

    const trigger = mobileMenuTriggerRef.current
    const focusFrame = window.requestAnimationFrame(() => mobileMenuCloseRef.current?.focus())

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setMobileMenuOpen(false)
        return
      }

      if (event.key !== 'Tab') return

      const menu = document.getElementById('primary-navigation')
      if (!menu) return

      const focusableElements = Array.from(
        menu.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )
      ).filter((element) => !element.hasAttribute('hidden'))
      const firstElement = focusableElements[0]
      const lastElement = focusableElements.at(-1)

      if (!firstElement || !lastElement) return

      const activeElement = document.activeElement
      if (event.shiftKey && (activeElement === firstElement || !menu.contains(activeElement))) {
        event.preventDefault()
        lastElement.focus()
      } else if (
        !event.shiftKey &&
        (activeElement === lastElement || !menu.contains(activeElement))
      ) {
        event.preventDefault()
        firstElement.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)

    return () => {
      window.cancelAnimationFrame(focusFrame)
      document.removeEventListener('keydown', handleKeyDown)
      window.requestAnimationFrame(() => {
        if (!window.matchMedia(DESKTOP_NAVIGATION_QUERY).matches && trigger?.isConnected) {
          trigger.focus()
        }
      })
    }
  }, [isDesktopNavigation, mobileMenuOpen])

  // 搜索快捷键监听（Cmd/Ctrl + K）
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        setSearchOpen(true)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [])

  useEffect(() => {
    const activeRoute =
      PAGE_TITLE_ROUTES.find(([path]) => path === pathname) ??
      [...PAGE_TITLE_ROUTES]
        .filter(([path]) => path !== '/')
        .sort(([firstPath], [secondPath]) => secondPath.length - firstPath.length)
        .find(([path]) => pathname.startsWith(`${path}/`))

    document.title = activeRoute ? `${activeRoute[1]} · ${APP_NAME}` : APP_NAME
    if (checking) return

    const focusFrame = window.requestAnimationFrame(() => mainContentRef.current?.focus())
    return () => window.cancelAnimationFrame(focusFrame)
  }, [checking, pathname])

  // 认证检查中，显示加载状态
  if (checking) {
    return (
      <div className="ios-page ios-app-height flex items-center justify-center overflow-hidden">
        <div className="ios-status-panel">
          <span className="ios-symbol ios-symbol-md ios-symbol-blue">
            <LoaderCircle className="ios-spin-slow h-5 w-5" strokeWidth={2.5} />
          </span>
          <div className="min-w-0">
            <p className="truncate text-[16px] font-semibold leading-6 text-foreground">
              {APP_NAME}
            </p>
            <p className="text-[14px] leading-5 text-muted-foreground">正在验证登录状态...</p>
          </div>
        </div>
      </div>
    )
  }

  // 菜单项配置 - 分块结构
  const menuSections: MenuSection[] = [
    {
      title: '概览',
      items: [
        { icon: Home, label: '首页', path: '/' },
        { icon: BarChart3, label: '统计数据', path: '/statistics' },
      ],
    },
    {
      title: '配置编辑',
      items: [
        { icon: FileText, label: '主程序配置', path: '/config/bot' },
        {
          icon: Server,
          label: 'AI模型厂商配置',
          path: '/config/modelProvider',
          tourId: 'sidebar-model-provider',
        },
        {
          icon: Boxes,
          label: '模型管理与分配',
          path: '/config/model',
          tourId: 'sidebar-model-management',
        },
        { icon: Sliders, label: '平台接入', path: '/config/adapter' },
      ],
    },
    {
      title: '资源管理',
      items: [
        { icon: Smile, label: '表情包管理', path: '/resource/emoji' },
        { icon: MessageSquare, label: '表达方式管理', path: '/resource/expression' },
        { icon: Activity, label: '行为学习管理', path: '/resource/behavior' },
        { icon: UploadCloud, label: '聊天记录学习', path: '/resource/chat-history-import' },
        { icon: Hash, label: '黑话管理', path: '/resource/jargon' },
        { icon: UserCircle, label: '人物信息管理', path: '/resource/person' },
        { icon: BrainCircuit, label: '记忆系统概览', path: '/resource/memory' },
      ],
    },
    {
      title: '扩展与监控',
      items: [
        { icon: Package, label: '插件市场', path: '/plugins' },
        { icon: Sliders, label: '插件配置', path: '/plugin-config' },
        { icon: ScanSearch, label: '模型请求追踪', path: '/model-traces' },
        { icon: FileSearch, label: '日志查看器', path: '/logs' },
        { icon: MessageSquare, label: '本地聊天室', path: '/chat' },
      ],
    },
    {
      title: '系统',
      items: [{ icon: Settings, label: '系统设置', path: '/settings' }],
    },
  ]

  const CurrentThemeIcon = theme === 'system' ? Monitor : resolvedTheme === 'dark' ? Moon : Sun
  const themeLabel =
    theme === 'system' ? '跟随系统' : resolvedTheme === 'dark' ? '深色模式' : '浅色模式'

  // 登出处理
  const handleLogout = async () => {
    await logout()
  }

  return (
    <TooltipProvider delayDuration={300}>
      <div className="ios-app-shell ios-app-height flex overflow-hidden">
        <a
          href="#main-content"
          className="fixed left-4 top-3 z-[100] -translate-y-[calc(100%+1rem)] rounded-full bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground shadow-lg transition-transform focus:translate-y-0 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
        >
          跳到主内容
        </a>
        {/* Sidebar */}
        <aside
          id="primary-navigation"
          data-mobile-state={mobileMenuOpen ? 'open' : 'closed'}
          role={isDesktopNavigation ? undefined : 'dialog'}
          aria-label={isDesktopNavigation ? undefined : '主导航菜单'}
          aria-modal={isDesktopNavigation ? undefined : true}
          aria-hidden={!isDesktopNavigation && !mobileMenuOpen}
          inert={!isDesktopNavigation && !mobileMenuOpen}
          className={cn(
            'motion-sidebar fixed inset-y-0 left-0 z-50 flex w-[min(86vw,22rem)] max-w-[22rem] flex-col overflow-hidden rounded-r-[28px] border-r border-white/70 bg-white/[0.9] shadow-[18px_0_48px_rgba(31,41,55,0.14)] backdrop-blur-2xl dark:border-white/10 dark:bg-zinc-900/[0.78] dark:shadow-[18px_0_54px_rgba(0,0,0,0.38)] lg:relative lg:z-0 lg:w-auto lg:max-w-none lg:rounded-none lg:border-black/[0.035] lg:bg-white/[0.58] lg:shadow-none lg:backdrop-blur-2xl dark:lg:border-white/10 dark:lg:bg-white/[0.055]',
            sidebarOpen ? 'lg:w-64' : 'lg:w-16',
            mobileMenuOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'
          )}
        >
          {/* Logo 区域 */}
          <div className="flex min-h-[5rem] items-center justify-between border-b border-black/[0.035] bg-white/[0.48] px-6 pb-3 pt-[max(1rem,env(safe-area-inset-top))] dark:border-white/10 dark:bg-white/[0.035] lg:h-16 lg:min-h-0 lg:bg-transparent lg:px-4 lg:py-0">
            <div
              className={cn(
                'relative flex min-w-0 flex-1 items-center justify-start overflow-hidden pr-4 transition-all duration-[var(--motion-duration-standard)] ease-[var(--motion-ease-standard)] lg:justify-center lg:pr-0',
                // 移动端始终完整显示,桌面端根据 sidebarOpen 切换
                'lg:flex-1',
                !sidebarOpen && 'lg:w-8 lg:flex-none'
              )}
            >
              {/* 移动端始终显示完整 Logo，桌面端根据 sidebarOpen 切换 */}
              <div className={cn('flex min-w-0 flex-1 items-center', !sidebarOpen && 'lg:hidden')}>
                <span
                  className="lg:text-primary-gradient min-w-0 truncate text-[20px] font-semibold leading-tight text-foreground sm:text-[21px] lg:text-xl"
                  title={APP_NAME}
                >
                  {APP_NAME}
                </span>
              </div>
              {/* 折叠时的 Logo - 仅桌面端显示 */}
              {!sidebarOpen && (
                <span className="text-primary-gradient hidden text-2xl font-semibold lg:block">
                  R
                </span>
              )}
            </div>
            <button
              ref={mobileMenuCloseRef}
              type="button"
              onClick={() => setMobileMenuOpen(false)}
              className="ios-touch flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-muted/70 text-muted-foreground shadow-[0_1px_0_rgba(255,255,255,0.56)_inset] hover:bg-muted dark:shadow-[0_1px_0_rgba(255,255,255,0.07)_inset] lg:hidden"
              aria-label="关闭菜单"
              title="关闭菜单"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          <ScrollArea className={cn('flex-1 overflow-x-hidden', !sidebarOpen && 'lg:w-16')}>
            <nav className={cn('px-5 py-5 lg:p-3', !sidebarOpen && 'lg:w-16 lg:p-2')}>
              <ul
                className={cn(
                  // 移动端始终使用正常间距,桌面端根据 sidebarOpen 切换
                  'space-y-4 lg:space-y-5',
                  !sidebarOpen && 'lg:w-full lg:space-y-3'
                )}
              >
                {menuSections.map((section, sectionIndex) => (
                  <li key={section.title}>
                    {/* 块标题 - 移动端始终可见，桌面端根据 sidebarOpen 切换 */}
                    <div
                      className={cn(
                        'mb-1.5 h-[1.25rem] px-2 lg:mb-2 lg:px-3',
                        // 移动端始终显示，桌面端根据状态切换
                        !sidebarOpen && 'lg:invisible lg:mb-1'
                      )}
                    >
                      <h3 className="text-muted-foreground/72 whitespace-nowrap text-[12px] font-medium leading-5 lg:text-xs lg:font-semibold lg:uppercase lg:text-muted-foreground/70">
                        {section.title}
                      </h3>
                    </div>

                    {/* 分割线 - 仅在桌面端折叠时显示 */}
                    {!sidebarOpen && sectionIndex > 0 && (
                      <div className="mb-2 hidden border-t border-border lg:block" />
                    )}

                    {/* 菜单项列表 */}
                    <ul className="ios-group overflow-hidden lg:space-y-1 lg:overflow-visible lg:rounded-none lg:border-0 lg:bg-transparent lg:shadow-none lg:backdrop-blur-none dark:lg:border-0 dark:lg:bg-transparent dark:lg:shadow-none">
                      {section.items.map((item) => {
                        const isActive = matchRoute({ to: item.path })
                        const Icon = item.icon
                        const iconTileClass = menuIconTileClasses[item.path] ?? 'ios-symbol-gray'

                        const menuItemContent = (
                          <>
                            {/* 左侧高亮条 */}
                            {isActive && (
                              <div className="motion-selection absolute left-2 top-1/2 h-6 w-1 -translate-y-1/2 rounded-full bg-primary/95 lg:left-2 lg:h-5" />
                            )}
                            <div
                              className={cn(
                                'flex items-center transition-all duration-[var(--motion-duration-standard)] ease-[var(--motion-ease-standard)]',
                                sidebarOpen ? 'gap-3' : 'gap-3 lg:gap-0'
                              )}
                            >
                              <span className={cn('ios-symbol ios-symbol-sm', iconTileClass)}>
                                <Icon
                                  className="h-[19px] w-[19px] flex-shrink-0"
                                  strokeWidth={2.75}
                                  fill="none"
                                />
                              </span>
                              <span
                                className={cn(
                                  'whitespace-nowrap text-[16px] font-medium leading-6 transition-all duration-[var(--motion-duration-standard)] ease-[var(--motion-ease-standard)] lg:text-sm lg:leading-normal',
                                  isActive && 'font-semibold',
                                  sidebarOpen
                                    ? 'max-w-[200px] opacity-100'
                                    : 'max-w-[200px] opacity-100 lg:max-w-0 lg:overflow-hidden lg:opacity-0'
                                )}
                              >
                                {item.label}
                              </span>
                            </div>
                          </>
                        )

                        return (
                          <li key={item.path} className="relative">
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Link
                                  to={item.path}
                                  data-tour={item.tourId}
                                  className={cn(
                                    'ios-touch-row ios-nav-row group relative flex min-h-[58px] items-center overflow-hidden border-b border-border/50 py-3 last:border-b-0 lg:min-h-12 lg:rounded-[14px] lg:border-b-0 lg:py-2 lg:active:scale-[0.98]',
                                    isActive
                                      ? 'bg-[rgb(120_120_128_/_0.12)] text-foreground shadow-[inset_0_0_0_1px_rgba(255,255,255,0.58)] dark:bg-white/[0.08] lg:bg-[rgb(120_120_128_/_0.11)] lg:shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025),0_6px_18px_rgba(31,41,55,0.055)]'
                                      : 'text-muted-foreground',
                                    sidebarOpen
                                      ? 'px-4 lg:px-3'
                                      : 'px-4 lg:mx-auto lg:w-12 lg:justify-center lg:px-0'
                                  )}
                                  onClick={() => setMobileMenuOpen(false)}
                                >
                                  {menuItemContent}
                                  <ChevronRight className="ml-auto h-4 w-4 text-muted-foreground/70 lg:hidden" />
                                </Link>
                              </TooltipTrigger>
                              {!sidebarOpen && (
                                <TooltipContent side="right" className="hidden lg:block">
                                  <p>{item.label}</p>
                                </TooltipContent>
                              )}
                            </Tooltip>
                          </li>
                        )
                      })}
                    </ul>
                  </li>
                ))}
              </ul>
            </nav>
          </ScrollArea>
        </aside>

        {/* Mobile overlay */}
        <button
          type="button"
          tabIndex={-1}
          aria-label="关闭导航菜单"
          aria-hidden={!mobileMenuOpen}
          className={cn(
            'motion-overlay fixed inset-0 z-40 h-[100dvh] w-screen bg-[rgb(15_23_42_/_0.14)] dark:bg-[rgb(0_0_0_/_0.2)] lg:hidden',
            mobileMenuOpen ? 'opacity-100' : 'pointer-events-none opacity-0'
          )}
          onClick={() => setMobileMenuOpen(false)}
        />

        {/* Main content */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {/* Topbar */}
          <header className="sticky top-0 z-10 flex h-14 items-center justify-between border-b border-black/[0.035] bg-white/[0.72] px-4 shadow-[0_1px_0_rgba(255,255,255,0.55)_inset] backdrop-blur-2xl dark:border-white/10 dark:bg-white/[0.055] dark:shadow-[0_1px_0_rgba(255,255,255,0.06)_inset] sm:h-16 sm:px-4">
            <div className="z-10 flex min-w-0 items-center gap-2 sm:gap-4">
              {/* 移动端菜单按钮 */}
              <button
                ref={mobileMenuTriggerRef}
                onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
                className="ios-touch flex h-11 w-11 items-center justify-center rounded-full bg-muted/70 text-foreground/85 hover:bg-muted lg:hidden"
                aria-label={mobileMenuOpen ? '关闭菜单' : '打开菜单'}
                aria-controls="primary-navigation"
                aria-expanded={mobileMenuOpen}
              >
                <Menu className="h-5 w-5" />
              </button>

              {/* 桌面端侧边栏收起/展开按钮 */}
              <button
                onClick={() => setSidebarOpen(!sidebarOpen)}
                className="ios-touch hidden h-11 w-11 items-center justify-center rounded-full hover:bg-accent lg:flex"
                title={sidebarOpen ? '收起侧边栏' : '展开侧边栏'}
              >
                <ChevronLeft
                  className={cn(
                    'h-5 w-5 transition-transform duration-[var(--motion-duration-control)] ease-[var(--motion-ease-standard)]',
                    !sidebarOpen && 'rotate-180'
                  )}
                />
              </button>
            </div>

            <div className="pointer-events-none flex min-w-0 flex-1 justify-center lg:hidden">
              <span className="truncate text-[17px] font-semibold leading-6 text-foreground">
                {APP_NAME}
              </span>
            </div>

            <div className="z-10 flex items-center gap-1.5 sm:gap-2">
              {/* 搜索框 */}
              <button
                onClick={() => setSearchOpen(true)}
                className="ios-touch relative hidden h-11 w-64 items-center rounded-full border border-black/[0.025] bg-muted/65 pl-10 pr-16 text-left shadow-[inset_0_1px_1px_rgba(0,0,0,0.025)] hover:bg-muted/80 md:flex"
              >
                <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">搜索...</span>
                <Kbd size="sm" className="absolute right-2 top-1/2 -translate-y-1/2">
                  <span className="text-xs">⌘</span>K
                </Kbd>
              </button>

              {/* 搜索对话框 */}
              <SearchDialog open={searchOpen} onOpenChange={setSearchOpen} />

              <button
                onClick={() => setSearchOpen(true)}
                className="ios-touch flex h-11 w-11 items-center justify-center rounded-full bg-muted/70 text-foreground/85 hover:bg-muted md:hidden"
                aria-label="搜索"
                title="搜索"
              >
                <Search className="h-5 w-5" />
              </button>

              {/* 项目文档链接 */}
              <Button
                variant="ghost"
                size="sm"
                onClick={() => openExternalLink(PROJECT_DOCS_URL)}
                className="hidden h-10 w-10 gap-2 px-0 sm:inline-flex sm:h-11 sm:w-auto sm:px-4"
                title="查看项目文档"
              >
                <BookOpen className="h-4 w-4" />
                <span className="hidden sm:inline">项目文档</span>
              </Button>

              {/* 主题切换按钮 */}
              <Popover>
                <PopoverTrigger asChild>
                  <button
                    className="ios-touch hidden h-10 w-10 items-center justify-center rounded-full hover:bg-accent sm:flex sm:h-11 sm:w-11"
                    title={`外观：${themeLabel}`}
                    aria-label={`外观：${themeLabel}`}
                  >
                    <CurrentThemeIcon className="h-5 w-5" />
                  </button>
                </PopoverTrigger>
                <PopoverContent align="end" className="w-60 p-1.5">
                  <div className="overflow-hidden rounded-[14px]">
                    {themeOptions.map((option) => {
                      const OptionIcon = option.icon
                      const selected = theme === option.value

                      return (
                        <button
                          key={option.value}
                          type="button"
                          className="ios-touch flex min-h-[54px] w-full items-center gap-3 border-b border-border/45 px-3 py-2.5 text-left last:border-b-0 hover:bg-accent/60 focus-visible:bg-accent/60"
                          onClick={(event) =>
                            toggleThemeWithTransition(option.value, setTheme, event)
                          }
                        >
                          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[9px] bg-secondary text-muted-foreground">
                            <OptionIcon className="h-[18px] w-[18px]" />
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block text-[15px] font-medium leading-5 text-foreground">
                              {option.label}
                            </span>
                            <span className="block truncate text-[12px] leading-4 text-muted-foreground">
                              {option.description}
                            </span>
                          </span>
                          {selected && (
                            <Check className="motion-selection h-4 w-4 shrink-0 text-primary" />
                          )}
                        </button>
                      )
                    })}
                  </div>
                </PopoverContent>
              </Popover>

              {/* 分隔线 */}
              <div className="hidden h-6 w-px bg-border/70 sm:block" />

              {/* 登出按钮 */}
              <Button
                variant="ghost"
                size="sm"
                onClick={handleLogout}
                className="hidden h-10 w-10 gap-2 px-0 sm:inline-flex sm:h-11 sm:w-auto sm:px-4"
                title="登出系统"
              >
                <LogOut className="h-4 w-4" />
                <span className="hidden sm:inline">登出</span>
              </Button>

              <button
                className="ios-touch flex h-11 w-11 items-center justify-center rounded-full bg-muted/70 text-foreground/85 hover:bg-muted sm:hidden"
                aria-label="更多操作"
                title="更多操作"
                onClick={() => setMobileActionsOpen(true)}
              >
                <MoreHorizontal className="h-5 w-5" />
              </button>

              <Dialog open={mobileActionsOpen} onOpenChange={setMobileActionsOpen}>
                <DialogContent className="ios-sheet bottom-0 left-0 top-auto max-h-[82vh] w-full max-w-none translate-x-0 translate-y-0 gap-0 rounded-b-none rounded-t-[28px] border-x-0 border-b-0 p-0 pb-[max(1rem,env(safe-area-inset-bottom))] sm:hidden [&>button:last-child]:hidden">
                  <DialogHeader className="sr-only">
                    <DialogTitle>更多操作</DialogTitle>
                  </DialogHeader>
                  <div className="space-y-3 px-5 pb-2 pt-4">
                    <div className="ios-group overflow-hidden">
                      <button
                        type="button"
                        className="ios-row ios-touch min-h-[58px] w-full text-left focus-visible:bg-accent/60 focus-visible:ring-0"
                        onClick={() => {
                          setMobileActionsOpen(false)
                          openExternalLink(PROJECT_DOCS_URL)
                        }}
                      >
                        <span className="flex items-center gap-3">
                          <span className="ios-symbol ios-symbol-sm ios-symbol-blue">
                            <BookOpen className="h-[18px] w-[18px]" />
                          </span>
                          <span className="text-[17px] font-medium leading-6">项目文档</span>
                        </span>
                      </button>
                      {themeOptions.map((option) => {
                        const OptionIcon = option.icon
                        const selected = theme === option.value

                        return (
                          <button
                            key={option.value}
                            type="button"
                            className="ios-row ios-touch min-h-[58px] w-full text-left focus-visible:bg-accent/60 focus-visible:ring-0"
                            onClick={(event) => {
                              toggleThemeWithTransition(option.value, setTheme, event)
                              setMobileActionsOpen(false)
                            }}
                          >
                            <span className="flex items-center gap-3">
                              <span className="ios-symbol ios-symbol-sm ios-symbol-purple">
                                <OptionIcon className="h-[18px] w-[18px]" />
                              </span>
                              <span className="text-[17px] font-medium leading-6">
                                {option.label}
                              </span>
                            </span>
                            {selected && (
                              <Check className="motion-selection h-4 w-4 text-primary" />
                            )}
                          </button>
                        )
                      })}
                      <button
                        type="button"
                        className="ios-row ios-touch text-destructive min-h-[58px] w-full text-left focus-visible:bg-accent/60 focus-visible:ring-0"
                        onClick={() => {
                          setMobileActionsOpen(false)
                          void handleLogout()
                        }}
                      >
                        <span className="flex items-center gap-3">
                          <span className="ios-symbol ios-symbol-sm ios-symbol-red">
                            <LogOut className="h-[18px] w-[18px]" />
                          </span>
                          <span className="text-[17px] font-medium leading-6">登出系统</span>
                        </span>
                      </button>
                    </div>

                    <button
                      type="button"
                      className="ios-group ios-touch flex min-h-[52px] w-full items-center justify-center px-4 text-[17px] font-semibold leading-6 text-primary focus-visible:bg-accent/60 focus-visible:ring-0"
                      onClick={() => setMobileActionsOpen(false)}
                    >
                      取消
                    </button>
                  </div>
                </DialogContent>
              </Dialog>
            </div>
          </header>

          {/* Page content */}
          <main
            ref={mainContentRef}
            id="main-content"
            tabIndex={-1}
            className="flex-1 overflow-hidden bg-background focus:outline-none"
          >
            <div key={pathname} className="motion-page h-full min-w-0">
              {children}
            </div>
          </main>
        </div>
      </div>
    </TooltipProvider>
  )
}
