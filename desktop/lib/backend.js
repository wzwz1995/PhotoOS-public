'use strict'
/**
 * PhotoOS 后端的进程托管。
 *
 * 为什么要有这一层：PhotoOS 的界面是 uvicorn 起的 FastAPI 服务（127.0.0.1:8760），
 * 桌面壳不能只做一个 `loadURL` —— 它得负责把服务**拉起来、等到健康、再关掉**，
 * 而且这三件事每一步都有踩过的坑（端口被占、拉起来但没就绪、退出留下孤儿进程）。
 */

const { spawn } = require('node:child_process')
const net = require('node:net')
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')

const DEFAULT_HOST = '127.0.0.1'
const DEFAULT_PORT = 8760
const RING_LINES = 200

/** 挑一个端口：优先用 PhotoOS 的默认端口，被占就让系统分配。 */
function pickPort(preferred = DEFAULT_PORT) {
  return new Promise((resolve, reject) => {
    const probe = net.createServer()
    probe.once('error', () => {
      // 默认端口被占（比如浏览器版正开着）—— 让系统给一个空闲端口
      const any = net.createServer()
      any.once('error', reject)
      any.listen(0, DEFAULT_HOST, () => {
        const { port } = any.address()
        any.close(() => resolve(port))
      })
    })
    probe.listen(preferred, DEFAULT_HOST, () => {
      probe.close(() => resolve(preferred))
    })
  })
}

/** 探一次 /api/health。就绪返回 true，没起来返回 false（不抛）。 */
function probeHealth(host, port, timeoutMs = 1200) {
  return new Promise((resolve) => {
    const req = http.get(
      { host, port, path: '/api/health', timeout: timeoutMs, headers: { Accept: 'application/json' } },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => { body += chunk })
        res.on('end', () => {
          try {
            const data = JSON.parse(body)
            resolve(res.statusCode === 200 && data && data.status === 'ok' ? data : false)
          } catch { resolve(false) }
        })
      },
    )
    req.on('timeout', () => { req.destroy(); resolve(false) })
    req.on('error', () => resolve(false))
  })
}

class Backend {
  /**
   * @param {object} opts
   * @param {string} opts.python     解释器（带 photoos 的那个环境）
   * @param {string} opts.cwd        工作目录（项目根；数据根靠 photoos.toml / PHOTOOS_DATA_ROOT 定位）
   * @param {string} opts.logFile    壳自己的日志（后端的 --log-file 另写一份）
   * @param {number} [opts.port]     期望端口
   * @param {string} [opts.host]
   * @param {NodeJS.ProcessEnv} [opts.env]
   */
  constructor(opts) {
    this.opts = { host: DEFAULT_HOST, ...opts }
    this.child = null
    this.port = null
    this.url = null
    this.lines = []
    this.logStream = null
    this.exited = false
    this._onExit = null
  }

  get alive() {
    return !!this.child && this.child.exitCode === null && !this.exited
  }

  /** 最近若干行输出（出错对话框里给用户看的那段）。 */
  tail(n = 25) {
    return this.lines.slice(-n).join('\n')
  }

  _log(text) {
    const line = `[${new Date().toISOString()}] ${text}`
    this.lines.push(line)
    if (this.lines.length > RING_LINES) this.lines.splice(0, this.lines.length - RING_LINES)
    if (this.logStream) this.logStream.write(line + '\n')
  }

  async start() {
    if (this.child) throw new Error('后端已经在运行')

    fs.mkdirSync(path.dirname(this.opts.logFile), { recursive: true })
    this.logStream = fs.createWriteStream(this.opts.logFile, { flags: 'a' })

    const port = await pickPort(this.opts.port || DEFAULT_PORT)
    this.port = port
    this.url = `http://${this.opts.host}:${port}/`

    const args = ['-m', 'photoos', 'serve', '--host', this.opts.host, '--port', String(port), '--log-file']
    this._log(`启动后端：${this.opts.python} ${args.join(' ')}`)
    this._log(`工作目录：${this.opts.cwd}`)

    this.child = spawn(this.opts.python, args, {
      cwd: this.opts.cwd,
      windowsHide: true,          // 不弹黑窗口：这是桌面壳，不是 cmd
      env: {
        ...process.env,
        ...this.opts.env,
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        // 后端自己不要试图开浏览器（浏览器版启动器靠这个变量做同样的事）
        PHOTOOS_NO_BROWSER: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })

    const pipe = (stream) => {
      stream.setEncoding('utf8')
      stream.on('data', (chunk) => {
        for (const line of String(chunk).split(/\r?\n/)) {
          if (line.trim()) this._log(line.trimEnd())
        }
      })
    }
    pipe(this.child.stdout)
    pipe(this.child.stderr)

    this.child.on('error', (err) => {
      this._log(`后端启动失败：${err.message}`)
      this.exited = true
      if (this._onExit) this._onExit(err)
    })
    this.child.on('exit', (code, signal) => {
      this._log(`后端退出：code=${code} signal=${signal}`)
      this.exited = true
      if (this._onExit) this._onExit(new Error(`后端进程退出（code=${code}）`))
    })

    const deadline = Date.now() + (this.opts.healthTimeoutMs || 90_000)
    for (;;) {
      if (this.exited) throw new Error(`后端没起来就退出了。\n\n${this.tail()}`)
      const health = await probeHealth(this.opts.host, port)
      if (health) {
        this._log(`后端就绪：${this.url}（schema ${health.schema_version}）`)
        return { url: this.url, port, health }
      }
      if (Date.now() > deadline) {
        this.stop()
        throw new Error(`等了 ${Math.round((this.opts.healthTimeoutMs || 90_000) / 1000)} 秒，后端仍未就绪。\n\n${this.tail()}`)
      }
      await new Promise((r) => setTimeout(r, 350))
    }
  }

  onUnexpectedExit(fn) {
    this._onExit = fn
  }

  /** 关掉后端；Windows 上必须连子进程树一起杀（AI 任务会再起 python）。 */
  stop() {
    const child = this.child
    this.child = null
    if (!child || child.exitCode !== null) {
      if (this.logStream) this.logStream.end()
      return
    }
    this._log('停止后端')
    if (process.platform === 'win32') {
      try {
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
      } catch {
        child.kill('SIGTERM')
      }
    } else {
      child.kill('SIGTERM')
      setTimeout(() => { try { child.kill('SIGKILL') } catch { /* 已退出 */ } }, 3000).unref()
    }
    if (this.logStream) this.logStream.end()
  }
}

module.exports = { Backend, pickPort, probeHealth, DEFAULT_PORT }
