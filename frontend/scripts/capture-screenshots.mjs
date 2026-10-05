/**
 * 抓取 UI 截图并在 PR 里发布（不把图片塞进 PR 正文，也不留悬空 blob 链接）。
 *
 * 设计要点：
 * - 不需要后端：前端 API 走相对路径，用 page.route 打桩即可（与 e2e 同思路）。
 * - 输出文件名固定（login.png / dashboard.png / chat.png），
 *   便于 PR 评论里用稳定 URL 引用。
 *
 * 用法：
 *   node scripts/capture-screenshots.mjs <输出目录>
 *
 * 环境变量：
 *   CAPTURE_BASE_URL  已运行的站点地址（给了就复用，不再自起 server）
 *   CAPTURE_PORT      dev server 端口（默认 5199，避开 e2e 用的 5173）
 */

import { mkdir, writeFile } from 'node:fs/promises'
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
import { chromium } from '@playwright/test'

const OUT_DIR = process.argv[2] || 'screenshots'
const PORT = Number(process.env.CAPTURE_PORT || 5199)
const BASE_URL = process.env.CAPTURE_BASE_URL || `http://127.0.0.1:${PORT}`
const VIEWPORT = { width: 1440, height: 900 }

/** 打桩：让受保护页面能渲染出来，而不需要真实后端 */
async function stubAuth(page) {
  await page.route('**/api/v1/auth/login/json', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 0,
        message: 'success',
        data: { access_token: 'screenshot-token', token_type: 'bearer', expires_in: 3600 },
      }),
    }),
  )
  await page.route('**/api/v1/users/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 1,
        username: 'guest',
        email: 'guest@example.com',
        role: 'user',
        is_active: true,
        is_superuser: false,
        organization_id: 1,
      }),
    }),
  )
  // 其它接口一律返回空成功，避免页面因后端缺失而白屏
  await page.route('**/api/v1/**', (route) => {
    const url = route.request().url()
    if (url.includes('/users/me') || url.includes('/auth/login')) return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ code: 0, message: 'success', data: null }),
    })
  })
}

async function waitForServer(url, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { redirect: 'manual' })
      if (res.status > 0) return true
    } catch {
      /* 还没起来 */
    }
    await sleep(1000)
  }
  throw new Error(`dev server 在 ${timeoutMs}ms 内没起来: ${url}`)
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true })

  let server = null
  if (!process.env.CAPTURE_BASE_URL) {
    console.log(`[capture] 启动 dev server :${PORT}`)
    server = spawn(
      process.platform === 'win32' ? 'npx.cmd' : 'npx',
      ['vite', '--port', String(PORT), '--strictPort', '--host', '127.0.0.1'],
      { cwd: process.cwd(), stdio: 'inherit', shell: process.platform === 'win32' },
    )
    await waitForServer(BASE_URL)
  }

  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1 })
  const page = await context.newPage()
  const consoleErrors = []
  page.on('pageerror', (e) => consoleErrors.push(String(e)))

  const shots = [
    { name: 'login', path: '/login', auth: false, desc: '登录页' },
    { name: 'dashboard', path: '/dashboard', auth: true, desc: '仪表盘' },
    { name: 'chat', path: '/chat', auth: true, desc: '对话页' },
  ]

  try {
    for (const shot of shots) {
      await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' }).catch(() => {})
      await page.evaluate((t) => {
        if (t) {
          localStorage.setItem('docmind_token', 'screenshot-token')
          // 必须同时种过期时间：路由守卫有 `if (token && isTokenExpired())` 分支，
          // 而 isTokenExpired() 在**缺少过期条目时返回 true** → token 会被直接清掉、
          // 页面被踢回登录页。此前只种 token 不种过期时间，导致三张截图全是登录页。
          localStorage.setItem(
            'docmind_token_expires',
            String(Date.now() + 3600 * 1000),
          )
        } else {
          localStorage.removeItem('docmind_token')
          localStorage.removeItem('docmind_token_expires')
        }
      }, shot.auth)
      await stubAuth(page)
      await page.goto(`${BASE_URL}${shot.path}`, { waitUntil: 'networkidle', timeout: 30_000 }).catch(() => {})
      await sleep(1200) // 等动画/图表落定

      // 断言：必须停在目标页，而不是被重定向走（通常是踢回登录页）。
      //
      // 没有这条断言时，脚本会把**登录页**当成仪表盘/对话页拍下来，而 workflow 依然绿 ——
      // 它当时就真的这么干过：三张图里两张是同一个登录页，字节数完全相同。抓到了错误
      // 页面还不报错，比没有截图更糟：它给的是"有图有真相"的假象。
      //
      // 必须比较 pathname，不能用 `url.includes(path)` —— 被踢回登录页时 URL 是
      // `/login?redirect=/dashboard`，**子串里就含目标路径**，includes 会返回 true，
      // 断言形同虚设（这版断言的第一稿就是这么写的，负向测试当场把它抓了出来）。
      const finalUrl = page.url()
      let finalPath = ''
      try {
        finalPath = new URL(finalUrl).pathname
      } catch {
        finalPath = finalUrl
      }
      if (finalPath !== shot.path) {
        throw new Error(
          `截图 "${shot.desc}" 被重定向：期望停在 ${shot.path}，实际落在 ${finalPath}（完整 URL: ${finalUrl}）`,
        )
      }

      const file = `${OUT_DIR}/${shot.name}.png`
      await page.screenshot({ path: file, fullPage: false })
      console.log(`[capture] ${shot.desc} -> ${file}  (实际 url=${finalUrl})`)
    }
  } finally {
    await browser.close()
    if (server) server.kill()
  }

  if (consoleErrors.length) {
    console.warn(`[capture] 页面 JS 报错 ${consoleErrors.length} 条（前 3 条）：`)
    consoleErrors.slice(0, 3).forEach((e) => console.warn('  -', e))
  }
  await writeFile(`${OUT_DIR}/.captured`, new Date().toISOString(), 'utf8')
  console.log('[capture] 完成')
}

main().catch((err) => {
  console.error('[capture] 失败:', err)
  process.exit(1)
})
