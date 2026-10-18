'use strict'
/**
 * 桌面壳暴露给页面的最小 API。
 *
 * 只开两个口子（目录选择、打开数据目录）—— 不把 `require` 交给页面，
 * 页面仍然是那个用 HTTP 跟本机服务说话的普通页面。
 */
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('photoosDesktop', {
  isDesktop: true,
  platform: process.platform,
  pickDirectory: (current) => ipcRenderer.invoke('photoos:pick-directory', current || null),
  openDataFolder: () => ipcRenderer.invoke('photoos:open-data-folder'),
  // 页面上的「退出 PhotoOS」按钮用：主进程弹二次确认，确认后连后端一起收走。
  quitApp: () => ipcRenderer.invoke('photoos:quit'),
  // 同步桥：页面里的 window.prompt 由它实现（Electron 自身不支持 prompt）
  promptSync: (message, defaultValue) => ipcRenderer.sendSync('photoos:prompt-sync', { message, defaultValue }),
})
