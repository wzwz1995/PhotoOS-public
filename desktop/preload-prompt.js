'use strict'
/** 输入框窗口的 preload：只为那一张 460×190 的对话框服务。 */
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('promptHost', {
  // >0 = 自动化验收：等这么多毫秒后自动提交（正常运行时是 0，不会触发）
  autoTest: Number(process.env.PHOTOOS_PROMPT_AUTOTEST || 0),
  onInit: (fn) => ipcRenderer.once('photoos:prompt-init', (_event, payload) => fn(payload)),
  submit: (value) => ipcRenderer.send('photoos:prompt-reply', value),
})
