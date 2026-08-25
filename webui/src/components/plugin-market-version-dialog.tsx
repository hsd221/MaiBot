import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowDownToLine,
  ArrowUpCircle,
  CheckCircle2,
  Loader2,
  RotateCcw,
  Tag,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import {
  compareSemanticVersions,
  getDefaultMarketVersion,
  isMarketVersionCompatible,
  isMarketVersionSelectable,
  type MaimaiVersion,
} from '@/lib/plugin-market-ui'
import type { PluginInfo, PluginMarketVersion } from '@/types/plugin'

interface PluginMarketVersionDialogProps {
  plugin: PluginInfo | null
  runtimeVersion: MaimaiVersion | null
  busy: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: (plugin: PluginInfo, version: string) => void | Promise<void>
}

function versionStatusLabel(version: PluginMarketVersion): string {
  if (version.status === 'yanked') return '已撤回'
  if (version.status === 'blocked') return '已封禁'
  return version.installable ? '已审核' : '不可安装'
}

function versionStatusClasses(version: PluginMarketVersion): string {
  if (version.status === 'blocked') {
    return 'border-0 bg-destructive/10 text-destructive'
  }
  if (version.status === 'yanked' || !version.installable) {
    return 'border-0 bg-[rgb(255_149_0_/_0.14)] text-[color:rgb(176_96_0)] dark:text-[color:rgb(255_208_153)]'
  }
  return 'border-0 bg-[rgb(52_199_89_/_0.14)] text-[color:rgb(36_138_61)] dark:text-[color:rgb(99_230_131)]'
}

function operationLabel(plugin: PluginInfo, selectedVersion: string): string {
  if (!plugin.installed) return '安装所选版本'

  const comparison = compareSemanticVersions(selectedVersion, plugin.installed_version ?? '')
  if (comparison < 0) return '回退到所选版本'
  if (comparison > 0) return '更新到所选版本'
  return '当前已安装此版本'
}

function OperationIcon({
  plugin,
  selectedVersion,
}: {
  plugin: PluginInfo
  selectedVersion: string
}) {
  if (!plugin.installed) return <ArrowDownToLine className="mr-2 h-4 w-4" />

  const comparison = compareSemanticVersions(selectedVersion, plugin.installed_version ?? '')
  if (comparison < 0) return <RotateCcw className="mr-2 h-4 w-4" />
  if (comparison > 0) return <ArrowUpCircle className="mr-2 h-4 w-4" />
  return <CheckCircle2 className="mr-2 h-4 w-4" />
}

export function PluginMarketVersionDialog({
  plugin,
  runtimeVersion,
  busy,
  onOpenChange,
  onConfirm,
}: PluginMarketVersionDialogProps) {
  const [selectedVersion, setSelectedVersion] = useState('')
  const defaultVersion = useMemo(
    () => (plugin ? getDefaultMarketVersion(plugin, runtimeVersion) : null),
    [plugin, runtimeVersion]
  )

  useEffect(() => {
    setSelectedVersion(defaultVersion?.version ?? '')
  }, [defaultVersion, plugin?.id])

  const selectedRelease = plugin?.market_versions?.find(
    (version) => version.version === selectedVersion
  )
  const isCurrentVersion =
    plugin?.installed === true && plugin.installed_version === selectedVersion
  const canConfirm =
    plugin !== null &&
    selectedRelease !== undefined &&
    isMarketVersionSelectable(selectedRelease, runtimeVersion) &&
    !isCurrentVersion &&
    !busy

  return (
    <Dialog
      open={plugin !== null}
      onOpenChange={(open) => {
        if (!busy) onOpenChange(open)
      }}
    >
      {plugin ? (
        <DialogContent
          className="flex max-h-[88vh] flex-col overflow-hidden p-0 sm:max-w-xl"
          preventOutsideClose={busy}
          aria-busy={busy}
        >
          <DialogHeader className="px-5 pt-5 sm:px-6 sm:pt-6">
            <DialogTitle>选择 {plugin.manifest.name} 版本</DialogTitle>
            <DialogDescription>
              当前{plugin.installed ? `安装 v${plugin.installed_version ?? '未知'}` : '尚未安装'}
              ，请选择 Registry 中经过审核且可安装的版本。
            </DialogDescription>
          </DialogHeader>

          <div className="min-h-0 flex-1 overflow-y-auto px-5 sm:px-6">
            {plugin.market_versions && plugin.market_versions.length > 0 ? (
              <RadioGroup
                value={selectedVersion}
                onValueChange={setSelectedVersion}
                aria-label={`${plugin.manifest.name} 可用版本`}
                className="ios-group gap-0 overflow-hidden"
              >
                {plugin.market_versions.map((version, index) => {
                  const compatible = isMarketVersionCompatible(version, runtimeVersion)
                  const selectable = version.installable && compatible
                  const inputId = `market-version-${index}`

                  return (
                    <label
                      key={`${version.version}-${version.ref}`}
                      htmlFor={inputId}
                      className={`ios-row min-h-[78px] items-start gap-3 py-3 text-left ${
                        selectable ? 'ios-touch cursor-pointer' : 'cursor-not-allowed opacity-65'
                      }`}
                    >
                      <RadioGroupItem
                        id={inputId}
                        value={version.version}
                        disabled={!version.installable || !compatible || busy}
                        className="mt-0.5 shrink-0"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex min-w-0 flex-wrap items-center gap-2">
                          <span className="text-[15px] font-semibold leading-5">
                            v{version.version}
                          </span>
                          {version.stable ? <Badge variant="secondary">稳定版</Badge> : null}
                          <Badge className={versionStatusClasses(version)}>
                            {versionStatusLabel(version)}
                          </Badge>
                          {!compatible ? <Badge variant="outline">不兼容</Badge> : null}
                        </span>
                        <span className="mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[12px] leading-4 text-muted-foreground">
                          <span className="inline-flex min-w-0 items-center gap-1">
                            <Tag className="h-3 w-3 shrink-0" />
                            <span className="break-all font-mono">{version.ref}</span>
                          </span>
                          <span>{version.released_at.slice(0, 10)}</span>
                        </span>
                        <span className="mt-1 block text-[12px] leading-4 text-muted-foreground">
                          支持 RiyaBot {version.host_application.min_version}
                          {version.host_application.max_version
                            ? ` - ${version.host_application.max_version}`
                            : ' 及以上'}
                        </span>
                      </span>
                    </label>
                  )
                })}
              </RadioGroup>
            ) : (
              <div className="ios-group overflow-hidden">
                <div className="ios-row min-h-[76px] justify-start gap-3">
                  <span className="ios-symbol ios-symbol-sm ios-symbol-orange">
                    <AlertTriangle className="h-4 w-4" />
                  </span>
                  <span className="text-[14px] leading-5 text-muted-foreground">
                    Registry 暂未提供可选择的审核版本。
                  </span>
                </div>
              </div>
            )}
          </div>

          <DialogFooter className="border-t border-border/55 px-5 py-4 sm:px-6">
            <Button variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button
              disabled={!canConfirm}
              onClick={() => selectedRelease && onConfirm(plugin, selectedRelease.version)}
            >
              {busy ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <OperationIcon plugin={plugin} selectedVersion={selectedVersion} />
              )}
              {busy ? '处理中' : operationLabel(plugin, selectedVersion)}
            </Button>
          </DialogFooter>
        </DialogContent>
      ) : null}
    </Dialog>
  )
}
