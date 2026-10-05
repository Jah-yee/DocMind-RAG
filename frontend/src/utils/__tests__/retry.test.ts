import { describe, it, expect, vi } from 'vitest'
import { withRetry, resolveRetryDelay } from '../retry'

describe('resolveRetryDelay', () => {
  const FALLBACK = 1234

  it('数字形式是 delay-seconds：按秒 × 1000，而不是 Date.parse 的年份/时间戳', () => {
    expect(resolveRetryDelay('0', FALLBACK)).toBe(0)
    expect(resolveRetryDelay('5', FALLBACK)).toBe(5000)
    expect(resolveRetryDelay('30', FALLBACK)).toBe(30000)
    expect(resolveRetryDelay('120', FALLBACK)).toBe(120000)
    expect(resolveRetryDelay('3600', FALLBACK)).toBe(3600000)
  })

  it('数字形式容忍首尾空白', () => {
    expect(resolveRetryDelay(' 120 ', FALLBACK)).toBe(120000)
  })

  it('HTTP-date 形式返回距 now 的毫秒差，而不是绝对时间戳', () => {
    // HTTP-date（toUTCString）只有秒级精度，所以 now 取整秒，差值才恰好是 60000
    const now = Date.parse('2026-10-05T11:13:02Z')
    const in60s = new Date(now + 60000).toUTCString()
    expect(resolveRetryDelay(in60s, FALLBACK, now)).toBe(60000)
  })

  it('已过去的 HTTP-date 返回 0（不是负数、不是绝对时间戳）', () => {
    const now = Date.parse('2026-10-05T11:13:02Z')
    const past60s = new Date(now - 60000).toUTCString()
    expect(resolveRetryDelay(past60s, FALLBACK, now)).toBe(0)
    expect(resolveRetryDelay('Fri, 31 Dec 1999 23:59:59 GMT', FALLBACK, now)).toBe(0)
  })

  it('无法解析的值回退到传入的默认延迟', () => {
    expect(resolveRetryDelay('abc', FALLBACK)).toBe(FALLBACK)
    expect(resolveRetryDelay('', FALLBACK)).toBe(FALLBACK)
  })

  it('形似数字但非法的值（"-1"/"1.5"/"5."）不回退成 0（V8 会把它们 Date.parse 成 2001 年前后的日期）', () => {
    for (const bad of ['-1', '1.5', '1e3', '+5', '5.', '.5', '1,5']) {
      expect(resolveRetryDelay(bad, FALLBACK)).toBe(FALLBACK)
    }
  })

  it('超大秒数封顶到 setTimeout 可表达的上限（2^31 - 1），不会溢出', () => {
    const MAX_TIMEOUT_MS = 2147483647
    expect(resolveRetryDelay('99999999999999999999', FALLBACK)).toBe(MAX_TIMEOUT_MS)
    expect(resolveRetryDelay('9'.repeat(400), FALLBACK)).toBe(MAX_TIMEOUT_MS)
  })

  it('返回值始终是有限的非负数', () => {
    const now = Date.now()
    const samples = ['0', '5', '120', 'abc', '-1', '1.5', 'Fri, 31 Dec 1999 23:59:59 GMT', '9'.repeat(400), '']
    for (const sample of samples) {
      const value = resolveRetryDelay(sample, FALLBACK, now)
      expect(Number.isFinite(value)).toBe(true)
      expect(value).toBeGreaterThanOrEqual(0)
    }
  })
})

describe('withRetry', () => {
  it('returns result on first success', async () => {
    const fn = vi.fn().mockResolvedValue('ok')
    const result = await withRetry(fn)
    expect(result).toBe('ok')
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('retries on retryable error and succeeds', async () => {
    const fn = vi.fn()
      .mockRejectedValueOnce({ status: 429 })
      .mockResolvedValue('ok')

    const result = await withRetry(fn, { retryDelay: 1 })
    expect(result).toBe('ok')
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('throws after max retries exhausted', async () => {
    const fn = vi.fn().mockRejectedValue({ status: 503 })

    await expect(
      withRetry(fn, { maxRetries: 2, retryDelay: 1 })
    ).rejects.toMatchObject({ status: 503 })
    expect(fn).toHaveBeenCalledTimes(3) // 1 initial + 2 retries
  })

  it('does not retry non-retryable errors', async () => {
    const fn = vi.fn().mockRejectedValue({ status: 400 })

    await expect(
      withRetry(fn, { retryDelay: 1 })
    ).rejects.toMatchObject({ status: 400 })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('calls onRetry callback', async () => {
    const onRetry = vi.fn()
    const fn = vi.fn()
      .mockRejectedValueOnce({ status: 504 })
      .mockResolvedValue('ok')

    await withRetry(fn, { retryDelay: 1, onRetry })
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onRetry).toHaveBeenCalledWith({ status: 504 }, 1)
  })

  it('uses custom retry condition', async () => {
    const fn = vi.fn()
      .mockRejectedValueOnce(new Error('custom'))
      .mockResolvedValue('ok')

    const result = await withRetry(fn, {
      retryDelay: 1,
      retryCondition: (err) => (err as Error).message === 'custom',
    })
    expect(result).toBe('ok')
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('数字秒数：retry-after "5" → 恰好等待 5000ms 才重试', async () => {
    vi.useFakeTimers()
    try {
      const fn = vi.fn()
        .mockRejectedValueOnce({
          status: 429,
          response: { headers: { 'retry-after': '5' } },
        })
        .mockResolvedValue('ok')

      const promise = withRetry(fn, { retryDelay: 1000 })
      await vi.advanceTimersByTimeAsync(0) // 让首轮失败的 reject 走完微任务、注册好定时器
      expect(fn).toHaveBeenCalledTimes(1)

      await vi.advanceTimersByTimeAsync(4999)
      expect(fn).toHaveBeenCalledTimes(1) // 4999ms 时还不应重试
      await vi.advanceTimersByTimeAsync(1)
      expect(fn).toHaveBeenCalledTimes(2) // 到 5000ms 才重试
      await expect(promise).resolves.toBe('ok')
    } finally {
      vi.useRealTimers()
    }
  })

  it('数字秒数：retry-after "0" → 不等待（旧实现会让 setTimeout 收到 946656000000ms）', async () => {
    vi.useFakeTimers()
    try {
      const fn = vi.fn()
        .mockRejectedValueOnce({
          status: 429,
          response: { headers: { 'retry-after': '0' } },
        })
        .mockResolvedValue('ok')

      const promise = withRetry(fn, { retryDelay: 1000 })
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(1)
      expect(fn).toHaveBeenCalledTimes(2) // 0ms 等待：1ms 内必然已重试
      await expect(promise).resolves.toBe('ok')
    } finally {
      vi.useRealTimers()
    }
  })

  it('HTTP-date：距 now 60 秒的日期 → 等待 60000ms', async () => {
    // 固定整秒起点：HTTP-date 只有秒级精度，起点取整秒后差值才恰好是 60000
    vi.useFakeTimers({ now: Date.parse('2026-10-05T11:13:02Z') })
    try {
      const now = Date.now()
      const fn = vi.fn()
        .mockRejectedValueOnce({
          status: 429,
          response: { headers: { 'retry-after': new Date(now + 60000).toUTCString() } },
        })
        .mockResolvedValue('ok')

      const promise = withRetry(fn, { retryDelay: 1 })
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(59999)
      expect(fn).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(1)
      expect(fn).toHaveBeenCalledTimes(2)
      await expect(promise).resolves.toBe('ok')
    } finally {
      vi.useRealTimers()
    }
  })

  it('无法解析（"abc"）→ 回退到默认退避延迟（retryDelay）', async () => {
    vi.useFakeTimers()
    try {
      const fn = vi.fn()
        .mockRejectedValueOnce({
          status: 429,
          response: { headers: { 'retry-after': 'abc' } },
        })
        .mockResolvedValue('ok')

      const promise = withRetry(fn, { retryDelay: 2000 })
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(1999)
      expect(fn).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(1)
      expect(fn).toHaveBeenCalledTimes(2)
      await expect(promise).resolves.toBe('ok')
    } finally {
      vi.useRealTimers()
    }
  })

  it('handles zero max retries (no retries)', async () => {
    const fn = vi.fn().mockRejectedValue({ status: 503 })

    await expect(
      withRetry(fn, { maxRetries: 0, retryDelay: 1 })
    ).rejects.toMatchObject({ status: 503 })
    expect(fn).toHaveBeenCalledTimes(1)
  })
})
