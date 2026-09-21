import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const bundlePath = new URL('../../plugins/kanban/dashboard/dist/index.js', import.meta.url)
const bundle = fs.readFileSync(bundlePath, 'utf8')

const boardFixture = {
  columns: [{
    name: 'running',
    tasks: [{
      id: 'task-telemetry',
      title: 'Inspect telemetry',
      status: 'running',
      assignee: 'builder',
      priority: 0,
      created_at: 1,
      comment_count: 0,
      link_counts: { parents: 0, children: 0 },
    }],
  }],
  tenants: [],
  assignees: ['builder'],
  latest_event_id: 0,
}

function sameDeps(left, right) {
  return Array.isArray(left) && Array.isArray(right) && left.length === right.length &&
    left.every((value, index) => Object.is(value, right[index]))
}

function makeSdkFixture(workers) {
  const instances = new Map()
  const pendingEffects = []
  const badges = []
  const requests = []
  let current = null

  function instanceFor(component) {
    if (!instances.has(component)) instances.set(component, { hooks: [], index: 0 })
    return instances.get(component)
  }

  function renderComponent(component, props) {
    const previous = current
    current = instanceFor(component)
    current.index = 0
    try {
      return component(props)
    } finally {
      current = previous
    }
  }

  function useState(initial) {
    const index = current.index++
    if (!current.hooks[index]) {
      current.hooks[index] = { value: typeof initial === 'function' ? initial() : initial }
    }
    const slot = current.hooks[index]
    return [slot.value, value => {
      slot.value = typeof value === 'function' ? value(slot.value) : value
    }]
  }

  function useRef(initial) {
    const index = current.index++
    if (!current.hooks[index]) current.hooks[index] = { current: initial }
    return current.hooks[index]
  }

  function useMemo(factory, deps) {
    const index = current.index++
    const prior = current.hooks[index]
    if (prior && sameDeps(prior.deps, deps)) return prior.value
    const value = factory()
    current.hooks[index] = { deps, value }
    return value
  }

  function useEffect(effect, deps) {
    const index = current.index++
    const prior = current.hooks[index]
    if (prior && sameDeps(prior.deps, deps)) return
    current.hooks[index] = { deps }
    pendingEffects.push(effect)
  }

  function flushEffects() {
    while (pendingEffects.length) pendingEffects.shift()()
  }

  const Badge = function Badge() {}
  const passthrough = function Passthrough() {}
  const React = {
    Component: class {},
    createElement(type, props, ...children) {
      if (type === Badge && props?.className?.includes('hermes-kanban-operator-badge')) {
        badges.push({ props, children })
      }
      if (typeof type === 'function' && ['BoardColumns', 'Column', 'TaskCard', 'OperatorStateBadge'].includes(type.name)) {
        return renderComponent(type, props || {})
      }
      return { type, props: props || {}, children }
    },
    useState,
    useEffect,
    useCallback: (callback, deps) => useMemo(() => callback, deps),
    useMemo,
    useRef,
  }

  const window = {
    __HERMES_PLUGIN_SDK__: {
      React,
      components: {
        Badge,
        Card: passthrough,
        CardContent: passthrough,
        Button: passthrough,
        Input: passthrough,
        Label: passthrough,
        Select: passthrough,
        SelectOption: passthrough,
      },
      hooks: { useState, useEffect, useCallback: React.useCallback, useMemo, useRef },
      useI18n: () => ({ t: { kanban: null }, locale: 'en' }),
      utils: { cn: (...values) => values.filter(Boolean).join(' '), timeAgo: () => 'now' },
      fetchJSON(url) {
        requests.push(url)
        if (url.includes('/config')) return Promise.resolve({ render_markdown: true })
        if (url.includes('/boards')) return Promise.resolve({ boards: [{ slug: 'default', total: 1 }], current: 'default' })
        if (url.includes('/workers/active')) return Promise.resolve({ workers })
        if (url.includes('/board')) return Promise.resolve(boardFixture)
        throw new Error(`unexpected dashboard request: ${url}`)
      },
      buildWsUrl: () => new Promise(() => {}),
    },
    __HERMES_PLUGINS__: {
      register(_name, component) { window.page = component },
    },
    localStorage: { getItem: () => 'default', setItem() {}, removeItem() {} },
    addEventListener() {},
    removeEventListener() {},
  }

  vm.runInNewContext(bundle, {
    window,
    URLSearchParams,
    Promise,
    console,
    setTimeout,
    clearTimeout,
  }, { filename: bundlePath.pathname })

  async function render() {
    renderComponent(window.page, {})
    flushEffects()
    await new Promise(resolve => setImmediate(resolve))
    renderComponent(window.page, {})
    flushEffects()
    return { badges, requests }
  }

  return { render }
}

async function renderWorker(worker) {
  return makeSdkFixture(worker ? [worker] : []).render()
}

test('renders direct redacted operator telemetry without raw payload data', async () => {
  const secret = 'Bearer secret-that-must-not-leak'
  const { badges, requests } = await renderWorker({
    task_id: 'task-telemetry',
    operator_state: 'WAITING_HUMAN',
    activity: {
      runtime: 'codex_app_server',
      tool: 'terminal',
      waiting_for: 'approval',
      thread_status: 'active',
      error: 'auth_error',
      ignored_payload: secret,
    },
  })

  assert.ok(requests.some(url => url.includes('/workers/active')), 'dashboard must read the I03 worker projection directly')
  assert.equal(badges.length, 1)
  assert.equal(badges[0].children[0], 'WAITING_HUMAN')
  assert.match(badges[0].props.title, /runtime: codex_app_server/)
  assert.match(badges[0].props.title, /waiting: approval/)
  assert.doesNotMatch(badges[0].props.title, /ignored_payload|Bearer|secret-that-must-not-leak/)
})

test('renders each backend operator state, including reserved FROZEN_CONFIRMED only when received', async () => {
  for (const state of ['ACTIVE', 'WAITING_HUMAN', 'PROCESS_GONE', 'UNKNOWN', 'FROZEN_CONFIRMED']) {
    const { badges } = await renderWorker({ task_id: 'task-telemetry', operator_state: state, activity: { runtime: 'hermes' } })
    assert.equal(badges.length, 1)
    assert.equal(badges[0].children[0], state)
  }
})

test('keeps the historical card free of telemetry when the flag-off response has no operator state', async () => {
  const { badges } = await renderWorker({ task_id: 'task-telemetry' })
  assert.deepEqual(badges, [])
})
