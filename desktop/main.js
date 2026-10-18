const {
  app, BrowserWindow, Menu, Tray, shell, dialog, ipcMain, nativeImage, session,
} = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

const { Backend } = require('./lib/backend')

const APP_NAME = 'PhotoOS'
const LOCAL_ORIGIN = /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/
const ATTACH = process.argv.includes('--attach') || !!process.env.PHOTOOS_DESKTOP_URL
const ATTACH_URL = process.env.PHOTOOS_DESKTOP_URL || 'http://127.0.0.1:8760/'

app.setName(APP_NAME)
if (process.platform === 'win32') app.setAppUserModelId('local.photoos.desktop')

// --------------------------------------------------------------------------- 配置

const CONFIG_PATH = () => path.join(app.getPath('userData'), 'desktop.json')

function loadConfig() {
  const defaults = {
    // 关窗口 = 收进托盘（长任务不中断）。想"关掉就退出"改成 false。
    keepRunningOnClose: true,
    // 下面几项留空 = 自动推断
    python: null,
    projectRoot: null,
    dataRoot: null,
    port: 8760,
  }
  try {
    return { ...defaults, ...JSON.parse(fs.readFileSync(CONFIG_PATH(), 'utf8')) }
  } catch {
    return defaults
  }
}

function findProjectRoot(start) {
  let dir = path.resolve(start)
  for (;;) {
    if (fs.existsSync(path.join(dir, 'photoos.toml'))) return dir
    const parent = path.dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
}

function resolvePython(config, projectRoot) {
  const candidates = [
    config.python,
    process.env.PHOTOOS_PYTHON,
    // 打包后：随包带的解释器
    path.join(process.resourcesPath, 'runtime', 'python.exe'),
    // 开发/本机：项目自己的 venv
    projectRoot && path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
  ].filter(Boolean)
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate
  }
  return candidates[0] || 'python'
}

function resolveCwd(config, projectRoot) {
  if (config.projectRoot) return config.projectRoot
  if (projectRoot) return projectRoot
  // 打包后没有 photoos.toml：让后端按自己的规则落到 %LOCALAPPDATA%\PhotoOS
  return path.join(app.getPath('appData'), 'PhotoOS')
}

// --------------------------------------------------------------------------- 窗口状态

const STATE_PATH = () => path.join(app.getPath('userData'), 'window-state.json')

function loadWindowState() {
  try {
    const state = JSON.parse(fs.readFileSync(STATE_PATH(), 'utf8'))
    if (Number.isFinite(state.width) && Number.isFinite(state.height)) return state
  } catch { /* 第一次启动 */ }
  return { width: 1440, height: 920, maximized: false }
}

function saveWindowState(win) {
  if (!win || win.isDestroyed()) return
  const bounds = win.isMaximized() ? win.getNormalBounds() : win.getBounds()
  try {
    fs.writeFileSync(STATE_PATH(), JSON.stringify({
      x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height,
      maximized: win.isMaximized(),
    }, null, 2))
  } catch { /* 写不进去就算了，不该因此崩掉 */ }
}

// --------------------------------------------------------------------------- 图标

function assetPath(...parts) {
  return path.join(__dirname, 'assets', ...parts)
}

function trayImage() {
  const image = nativeImage.createFromPath(assetPath('tray.png'))
  return image.isEmpty() ? nativeImage.createFromPath(assetPath('icon.png')) : image
}

// --------------------------------------------------------------------------- 壳

let mainWindow = null
let tray = null
let backend = null
let quitting = false
let appUrl = null
let notifiedTray = false
const config = loadConfig()

function isLocalUrl(url) {
  return LOCAL_ORIGIN.test(url)
}

function appOrigin() {
  try { return new URL(appUrl).origin } catch { return null }
}

/** 外链一律交给系统浏览器；壳自己不开新窗口。 */
function openExternal(url) {
  if (/^https?:/i.test(url)) shell.openExternal(url)
}

function createWindow(url) {
  appUrl = url
  const state = loadWindowState()

  const win = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    title: APP_NAME,
    backgroundColor: '#07090f',          // = --ink-stage，避免白屏闪一下
    icon: assetPath('icon.png'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
      backgroundThrottling: false,       // 切到后台时别把轮询任务节流掉
    },
  })

  if (state.maximized) win.maximize()

  // ---- 打开页面（后端刚就绪时偶发 ECONNREFUSED，重试几次）
  let attempts = 0
  const load = () => {
    attempts += 1
    win.loadURL(url).catch(() => { /* 具体错误走 did-fail-load */ })
  }
  win.webContents.on('did-fail-load', (_event, code, desc, failedUrl, isMainFrame) => {
    if (!isMainFrame) return
    if (attempts < 30 && (code === -102 || code === -100)) {
      setTimeout(load, 500)
    } else {
      dialog.showMessageBox(win, {
        type: 'error',
        title: `${APP_NAME} 打不开界面`,
        message: '本地服务没有响应',
        detail: `${desc}（${code}）\n${failedUrl}`,
        buttons: ['重试', '退出'],
        defaultId: 0,
      }).then(({ response }) => (response === 0 ? load() : app.quit()))
    }
  })

  win.once('ready-to-show', () => {
    win.show()
    if (process.env.PHOTOOS_DESKTOP_DEVTOOLS === '1') win.webContents.openDevTools({ mode: 'detach' })
    injectDesktopAffordances(win)
  })

  win.on('close', (event) => {
    if (quitting) return
    if (config.keepRunningOnClose && backend && backend.alive) {
      event.preventDefault()
      win.hide()
      if (!notifiedTray && tray) {
        notifiedTray = true
        tray.displayBalloon({
          title: `${APP_NAME} 还在运行`,
          content: '窗口已收进托盘，后台任务不会中断。右键托盘图标可以退出。',
        })
      }
    }
  })

  win.on('closed', () => { mainWindow = null })
  win.on('resize', () => saveWindowState(win))
  win.on('move', () => saveWindowState(win))

  // ---- 外链、导航、权限、下载
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    openExternal(target)
    return { action: 'deny' }
  })

  win.webContents.on('will-navigate', (event, target) => {
    if (isLocalUrl(target) && new URL(target).origin === appOrigin()) return
    event.preventDefault()
    openExternal(target)
  })

  win.webContents.on('will-attach-webview', (event) => event.preventDefault())

  mainWindow = win
  load()
  return win
}

/**
 * 给页面补两件"桌面壳才做得到"的小事。
 * 不改 PhotoOS 的源码，只往 DOM 上挂按钮 —— 前端保持一份。
 */
/**
 * PhotoOS 的界面里用了 14 处 `window.prompt()`（命名人物、新建主题、给动作传搜索词…），
 * 而 Electron **不支持** prompt（调用直接抛 `prompt() is not supported.`）。
 *
 * 这里补一个：真正的输入框窗口，`sendSync` 把结果同步交还给页面 ——
 * 页面代码一行都不用改。代价是调用期间页面线程被阻塞（与浏览器原生 prompt 的行为一致）。
 */
function showPromptWindow({ message, defaultValue }) {
  return new Promise((resolve) => {
    const dialogWindow = new BrowserWindow({
      parent: mainWindow || undefined,
      modal: true,
      width: 500,
      height: 210,
      resizable: false,
      minimizable: false,
      maximizable: false,
      title: APP_NAME,
      backgroundColor: '#0e1424',
      icon: assetPath('icon.png'),
      autoHideMenuBar: true,
      webPreferences: {
        preload: path.join(__dirname, 'preload-prompt.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    })

    let settled = false
    const finish = (value) => {
      if (settled) return
      settled = true
      ipcMain.removeListener('photoos:prompt-reply', onReply)
      if (!dialogWindow.isDestroyed()) dialogWindow.destroy()
      resolve(value)
    }
    const onReply = (event, value) => {
      if (event.sender === dialogWindow.webContents) finish(value)
    }

    ipcMain.on('photoos:prompt-reply', onReply)
    dialogWindow.on('closed', () => finish(null))
    dialogWindow.webContents.once('did-finish-load', () => {
      dialogWindow.webContents.send('photoos:prompt-init', {
        message: message || '',
        defaultValue: defaultValue || '',
      })
    })
    dialogWindow.loadFile(path.join(__dirname, 'prompt.html'))
  })
}

function installPromptBridge() {
  ipcMain.on('photoos:prompt-sync', (event, payload) => {
    showPromptWindow(payload || {})
      .then((value) => { event.returnValue = value })
      .catch(() => { event.returnValue = null })
  })
}

async function injectDesktopAffordances(win) {
  // prompt 桥：只在壳提供的 API 存在时替换，且保留"取消 = null"的语义
  await win.webContents.executeJavaScript(`(() => {
    const api = window.photoosDesktop;
    if (!api || typeof api.promptSync !== 'function') return 'no-bridge';
    if (window.__photoosPromptInstalled) return 'already';
    window.prompt = (message, defaultValue) => api.promptSync(
      String(message ?? ''), defaultValue === undefined ? '' : String(defaultValue));
    window.__photoosPromptInstalled = true;
    return 'prompt-installed';
  })()`, true).catch(() => {})

  const script = `(() => {
    if (document.getElementById('photoos-pick-dir')) return 'already';
    const input = document.getElementById('import-source');
    if (!input) return 'no-import-input';
    const button = document.createElement('button');
    button.id = 'photoos-pick-dir';
    button.type = 'button';
    button.className = (input.className || '') + ' btn small';
    button.textContent = '选目录…';
    button.title = '用系统目录选择器挑一个文件夹（桌面壳提供）';
    button.style.width = 'auto';
    button.style.flex = '0 0 auto';
    // 与输入框**同一行**：包一个 flex 容器，否则这一格会比其它字段高出一截
    const pickerRow = document.createElement('div');
    pickerRow.style.display = 'flex';
    pickerRow.style.gap = '8px';
    pickerRow.style.alignItems = 'center';
    input.parentNode.insertBefore(pickerRow, input);
    // 输入框在行里要能收缩，否则 width:100% 会把「选目录…」挤出格子
    input.style.flex = '1 1 auto';
    input.style.minWidth = '0';
    input.style.width = 'auto';
    pickerRow.appendChild(input);
    pickerRow.appendChild(button);
    button.addEventListener('click', async () => {
      const dir = await window.photoosDesktop.pickDirectory(input.value);
      if (!dir) return;
      input.value = dir;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    input.insertAdjacentElement('afterend', button);
    return 'injected';
  })()`
  try {
    await win.webContents.executeJavaScript(script, true)
  } catch { /* 注入失败不影响主流程 */ }
}

// --------------------------------------------------------------------------- 菜单

function buildMenu() {
  const template = [
    {
      label: APP_NAME,
      submenu: [
        { role: 'about', label: `关于 ${APP_NAME}` },
        { type: 'separator' },
        {
          label: '在浏览器中打开',
          click: () => { if (appUrl) openExternal(appUrl) },
        },
        {
          label: '打开日志目录',
          click: () => { if (backend) shell.showItemInFolder(backend.opts.logFile) },
        },
        { type: 'separator' },
        { label: '退出', accelerator: 'CmdOrCtrl+Q', click: () => { quitting = true; app.quit() } },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载' },
        { role: 'forceReload', label: '强制重新加载' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '数据目录',
          click: () => {
            const root = (backend && backend.opts.dataRoot) || path.join(app.getPath('appData'), 'PhotoOS', 'data')
            shell.openPath(root)
          },
        },
      ],
    },
  ]
  return Menu.buildFromTemplate(template)
}

// --------------------------------------------------------------------------- 托盘

function ensureTray() {
  if (tray) return tray
  tray = new Tray(trayImage())
  tray.setToolTip(APP_NAME)
  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: '显示窗口',
      click: () => {
        if (!mainWindow) return
        if (mainWindow.isMinimized()) mainWindow.restore()
        mainWindow.show()
        mainWindow.focus()
      },
    },
    { label: '在浏览器中打开', click: () => { if (appUrl) openExternal(appUrl) } },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit() } },
  ]))
  tray.on('click', () => {
    if (!mainWindow) return
    if (mainWindow.isVisible()) mainWindow.hide()
    else { mainWindow.show(); mainWindow.focus() }
  })
  return tray
}

// --------------------------------------------------------------------------- 启动 / 退出

/**
 * 绿色包自带的运行时（解压即用，不依赖本机 Python）。
 *
 * 布局：<exe 同级>/runtime/python/python.exe + runtime/backend。
 * 找不到就返回 null，退回开发期的解析逻辑（photoos.toml / PATH）。
 */
function bundledRuntime() {
  const exeDir = path.dirname(process.execPath)
  const python = path.join(exeDir, 'runtime', 'python', 'python.exe')
  const backendDir = path.join(exeDir, 'runtime', 'backend')
  if (!fs.existsSync(python) || !fs.existsSync(backendDir)) return null
  return { python, backendDir }
}

/** 首次运行：库还不存在就先 `photoos init`（用户不该看到"未初始化"）。 */
function ensureInitialized(runtime, dataRoot, log) {
  const dbPath = path.join(dataRoot, 'photoos.db')
  if (fs.existsSync(dbPath)) return true
  log(`首次运行，初始化数据根：${dataRoot}`)
  const result = spawnSync(runtime.python, ['-m', 'photoos', 'init'], {
    cwd: runtime.backendDir,
    env: { ...process.env, PYTHONPATH: runtime.backendDir, PHOTOOS_DATA_ROOT: dataRoot,
           PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    encoding: 'utf-8',
    windowsHide: true,
  })
  if (result.status !== 0) {
    throw new Error(`初始化失败：${result.stderr || result.stdout || result.status}`)
  }
  return true
}

let splash = null

/** 启动画面：没有它，用户双击后 1~2 秒内**什么都看不到**（会以为没点动）。 */
function showSplash() {
  if (splash) return splash
  splash = new BrowserWindow({
    width: 420, height: 220, frame: false, resizable: false, show: true,
    backgroundColor: '#0a1226', alwaysOnTop: true, skipTaskbar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  })
  splash.loadFile(path.join(__dirname, 'loading.html'), { query: { v: app.getVersion() } })
  return splash
}

function closeSplash() {
  if (splash) { splash.destroy(); splash = null }
}

async function startBackendAndWindow() {
  const bundled = bundledRuntime()
  const projectRoot = bundled ? bundled.backendDir
    : (findProjectRoot(app.getAppPath()) || findProjectRoot(__dirname))
  const python = bundled ? bundled.python : resolvePython(config, projectRoot)
  const cwd = bundled ? bundled.backendDir : resolveCwd(config, projectRoot)
  // 打包后没有 photoos.toml：数据根必须显式，否则会落到"看起来像空库"的默认位置
  const dataRoot = config.dataRoot || process.env.PHOTOOS_DATA_ROOT
    || (bundled ? path.join(app.getPath('appData'), 'PhotoOS', 'data')
                : (projectRoot ? path.join(projectRoot, 'data') : null))

  const env = { ...(config.dataRoot ? { PHOTOOS_DATA_ROOT: config.dataRoot } : {}) }
  if (bundled) {
    env.PYTHONPATH = bundled.backendDir
    env.PHOTOOS_DATA_ROOT = dataRoot
  }

  backend = new Backend({
    python,
    cwd,
    dataRoot,
    port: config.port,
    host: '127.0.0.1',
    logFile: path.join(app.getPath('userData'), 'backend.log'),
    env,
  })

  if (bundled && dataRoot) {
    try { ensureInitialized(bundled, dataRoot, (m) => backend && backend.log ? backend.log(m) : null) }
    catch (error) { throw new Error(`首次初始化失败：${error.message}`) }
  }

  backend.onUnexpectedExit(async (error) => {
    if (quitting) return
    const { response } = await dialog.showMessageBox({
      type: 'error',
      title: `${APP_NAME} 服务中断`,
      message: '本地服务意外退出了',
      detail: `${error.message}\n\n最近日志：\n${backend.tail(12)}`,
      buttons: ['重新启动', '退出'],
      defaultId: 0,
      cancelId: 1,
    })
    if (response === 0) {
      try {
        const { url } = await backend.start()
        appUrl = url
        if (mainWindow) mainWindow.loadURL(url)
      } catch (err) {
        dialog.showErrorBox('重启失败', String(err.message || err))
        quitting = true
        app.quit()
      }
    } else {
      quitting = true
      app.quit()
    }
  })

  const { url } = await backend.start()
  ensureTray()
  createWindow(url)
  closeSplash()
}

const gotTheLock = app.requestSingleInstanceLock()
if (!gotTheLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.show()
    mainWindow.focus()
  })

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(buildMenu())
    installPromptBridge()

    // 只放行剪贴板（界面里「复制路径」要用），其余权限一律不给。
    // ⚠️ 必须**两个 handler 都设**：writeText 走的是 permission *check*，不是 request ——
    // 只设 request 时实测报 `NotAllowedError: Write permission denied`。
    const clipboardAllowed = (permission) => /^clipboard/i.test(permission)
    session.defaultSession.setPermissionCheckHandler((_wc, permission) => clipboardAllowed(permission))
    session.defaultSession.setPermissionRequestHandler((_wc, permission, callback) => {
      callback(clipboardAllowed(permission))
    })

    // 导出/查看原片时，Chromium 默认会静默落盘；桌面壳里给一个"另存为"
    session.defaultSession.on('will-download', (_event, item) => {
      const target = dialog.showSaveDialogSync(mainWindow || undefined, {
        title: '保存',
        defaultPath: path.join(app.getPath('downloads'), item.getFilename()),
      })
      if (!target) { item.cancel(); return }
      item.setSavePath(target)
    })

    ipcMain.handle('photoos:pick-directory', async (_event, current) => {
      const result = await dialog.showOpenDialog(mainWindow || undefined, {
        title: '选择文件夹',
        defaultPath: current && fs.existsSync(current) ? current : undefined,
        properties: ['openDirectory'],
      })
      return result.canceled ? null : (result.filePaths[0] || null)
    })

    ipcMain.handle('photoos:open-data-folder', async () => {
      const root = (backend && backend.opts.dataRoot) || path.join(app.getPath('appData'), 'PhotoOS', 'data')
      return shell.openPath(root)
    })

    // 页面上的「退出 PhotoOS」按钮（维护页）。必须先二次确认：
    // 这个动作会连后端进程一起收走 —— 如果正有哈希/预览任务在跑，它会被中断
    // （任务表里留 PAUSED/PENDING，下次启动能续；但用户该知道）。
    ipcMain.handle('photoos:quit', async () => {
      const running = backend && backend.alive
      const { response } = await dialog.showMessageBox(mainWindow || undefined, {
        type: 'question',
        buttons: ['退出', '取消'],
        defaultId: 1,
        cancelId: 1,
        title: '退出 PhotoOS',
        message: '要退出 PhotoOS 吗？',
        detail: running
          ? '后端进程会一起关闭。若还有后台任务在跑，它会被中断（下次启动可续）。'
          : '后端进程当前不在运行。',
      })
      if (response !== 0) return { quit: false }
      quitting = true
      // 让渲染进程先把 IPC 回执收回去，再真正退出
      setTimeout(() => app.quit(), 50)
      return { quit: true }
    })

    // 开发期：允许 CDP 检查（打包后不需要，也就不开）
    if (process.argv.includes('--inspect-shell')) {
      app.commandLine.appendSwitch('remote-debugging-port', '9333')
    }

    try {
      if (ATTACH) {
        ensureTray()
        createWindow(ATTACH_URL)
      } else {
        showSplash()
        await startBackendAndWindow()
      }
    } catch (error) {
      closeSplash()
      const detail = [
        String(error && error.message ? error.message : error),
        '',
        backend ? `最近日志：\n${backend.tail(15)}` : '（后端还没起来，没有日志）',
        '',
        '日志文件：' + path.join(app.getPath('userData'), 'backend.log'),
      ].join('\n')
      dialog.showErrorBox(`${APP_NAME} 启动失败`, detail)
      quitting = true
      app.quit()
    }
  })

  app.on('before-quit', () => { quitting = true })
  app.on('will-quit', () => {
    if (backend) backend.stop()
    if (tray) { tray.destroy(); tray = null }
  })
  app.on('window-all-closed', () => {
    // 托盘模式下窗口关掉但服务还在，不退出
    if (!config.keepRunningOnClose || !backend || !backend.alive) app.quit()
  })
  app.on('activate', () => {
    if (mainWindow) mainWindow.show()
  })
}
