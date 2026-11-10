// Speckle for Hermes — Desktop half: a 3D viewer pane that follows the conversation,
// and an inline ::speckle-model{url="…"} viewer card in the transcript.
import * as sdk from '@hermes/plugin-sdk'
import { SandboxedFrame, atom, host, useValue } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'speckle'
const MODEL_URL_RE = /^https?:\/\/[^\s/]+\/projects\/[0-9a-zA-Z]+\/models\/[^\s/?#]+$/

let $focus = atom(null) // { url, reason, headline? }
// The viewer page lives in viewer/index.html and is served by the plugin backend; it only
// ever runs inside SandboxedFrame, never in this renderer realm.
let $viewerSrc = atom(null) // data: URL, or { error }
// The latest speckle_viewer command, replayed by any frame showing that url once it has loaded.
let $command = atom(null) // { url, command, seq }
let commandSeq = 0
let restRef = null
// No client-side cache: the backend serves per-version files from disk, and a renderer cache
// outlives gateway restarts, so a plugin update kept showing the previous object list.
function fetchFor(path, url) {
  return restRef(`${path}?url=${encodeURIComponent(url)}`)
}

async function loadViewerPage() {
  try {
    const { html } = await restRef('/viewer-page')
    $viewerSrc.set('data:text/html;charset=utf-8,' + encodeURIComponent(html))
  } catch (e) {
    $viewerSrc.set({ error: 'Speckle backend unavailable — enable the speckle plugin and restart the gateway.' })
  }
}

// Fetched once and shared by every viewer frame (the pane and each inline card); a failure is
// not cached, so the next frame retries.
let viewerCode = null
function loadViewerCode() {
  if (!viewerCode) {
    viewerCode = restRef('/viewer-bundle').then((r) => r.code)
    viewerCode.catch(() => { viewerCode = null })
  }
  return viewerCode
}

function errorText(e) {
  return String((e && (e.detail || e.message)) || e)
}

function SpeckleFrame({ url, className }) {
  const src = useValue($viewerSrc)
  const ref = useRef(null)
  const urlRef = useRef(url)
  urlRef.current = url
  const [ready, setReady] = useState(false)
  const [status, setStatus] = useState('')
  const [preview, setPreview] = useState(null)
  const [shown, setShown] = useState(false)
  const [selectedCount, setSelectedCount] = useState(0)
  const command = useValue($command)

  useEffect(() => {
    const onMessage = (e) => {
      if (!ref.current || e.source !== ref.current.contentWindow || !e.data) return
      if (e.data.type === 'speckle-viewer-need-code') {
        const frame = ref.current.contentWindow
        loadViewerCode().then(
          (code) => frame.postMessage({ type: 'speckle-viewer-code', code }, '*'),
          (err) => frame.postMessage({ type: 'speckle-viewer-code-error', error: 'could not load the viewer: ' + errorText(err) }, '*')
        )
      }
      if (e.data.type === 'speckle-viewer-ready') setReady(true)
      if (e.data.type === 'speckle-viewer-status') {
        setStatus(e.data.msg)
        if (e.data.msg === '') setShown(true)
      }
      if (e.data.type === 'speckle-selection' && Array.isArray(e.data.ids)) {
        setSelectedCount(e.data.ids.length)
        restRef('/selection', { method: 'POST', body: { url: urlRef.current, ids: e.data.ids } }).catch(() => {})
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  useEffect(() => {
    let live = true
    setShown(false)
    setPreview(null)
    fetchFor('/model-info', url).then((i) => live && setPreview(i.preview || null), () => {})
    return () => { live = false }
  }, [url])

  useEffect(() => {
    if (!ready || !url) return
    let live = true
    const frame = () => ref.current && ref.current.contentWindow
    frame() && frame().postMessage({ type: 'speckle-status', msg: 'receiving model…' }, '*')
    fetchFor('/viewer-objects', url).then(
      (objects) => live && frame() && frame().postMessage({ type: 'speckle-load', resource: url, objects }, '*'),
      (e) => live && setStatus('could not receive model: ' + errorText(e))
    )
    return () => { live = false }
  }, [ready, url])

  // Declared after the load effect, so for a new url the frame gets the objects first; the guest
  // page also waits for its load before running a command.
  useEffect(() => {
    if (!ready || !command || command.url !== url) return
    const frame = ref.current && ref.current.contentWindow
    frame && frame.postMessage({ type: 'speckle-command', command: command.command }, '*')
  }, [ready, url, command])

  if (!src || typeof src !== 'string') {
    return jsx('div', {
      className: 'flex items-center justify-center p-3 text-center text-xs text-(--ui-text-tertiary) ' + (className || ''),
      children: (src && src.error) || 'loading viewer…'
    })
  }
  return jsxs('div', {
    className: 'relative min-h-0 ' + (className || ''),
    children: [
      jsx(SandboxedFrame, { ref, src, title: 'Speckle 3D viewer', className: 'h-full w-full' }),
      // The version's server-rendered preview covers the frame until the 3D model is built.
      preview && !shown
        ? jsx('img', { src: preview, alt: '', className: 'pointer-events-none absolute inset-0 h-full w-full object-contain opacity-60' })
        : null,
      status && status !== 'ready'
        ? jsx('div', { className: 'pointer-events-none absolute left-2 top-2 text-[0.6875rem] text-(--ui-text-tertiary)', children: status })
        : null,
      selectedCount
        ? jsx('div', {
            className: 'pointer-events-none absolute bottom-2 right-2 rounded bg-(--ui-bg-tertiary) px-2 py-0.5 text-[0.6875rem] text-(--ui-text-secondary)',
            children: `${selectedCount} selected — ask Hermes about ${selectedCount === 1 ? 'it' : 'them'}`
          })
        : null
    ]
  })
}

function shortName(url) {
  const m = /\/projects\/([^/]+)\/models\/([^/?#]+)/.exec(url || '')
  return m ? `${m[1]} / ${m[2]}` : ''
}

// The backend keeps the last selection and pre_llm_call reports it to the agent for an hour, so
// clearing only the pane would leave Hermes talking about elements the user no longer sees.
function clearPane() {
  const focus = $focus.get()
  if (focus && focus.url) {
    restRef('/selection', { method: 'POST', body: { url: focus.url, ids: [] } }).catch(() => {})
  }
  $command.set(null)
  $focus.set(null)
}

function ViewerPane() {
  const focus = useValue($focus)
  const [draft, setDraft] = useState('')
  const url = focus && focus.url

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 p-2 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx('span', { className: 'font-medium', children: 'Speckle' }),
          jsx('span', { className: 'truncate text-xs text-(--ui-text-tertiary)', title: url || '', children: shortName(url) }),
          url
            ? jsx('button', {
                type: 'button',
                className: 'ml-auto text-xs text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
                onClick: () => ctxOs && ctxOs.openExternal(url),
                children: 'Open in Speckle ↗'
              })
            : null,
          url
            ? jsx('button', {
                type: 'button',
                className: 'text-xs text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
                title: 'Remove the model from this pane and forget the selection',
                onClick: () => {
                  setDraft('')
                  clearPane()
                },
                children: 'Clear'
              })
            : null
        ]
      }),
      focus && focus.headline
        ? jsx('div', {
            className: 'rounded border border-(--ui-border) px-2 py-1 text-xs',
            children: `New version: ${focus.headline}`
          })
        : null,
      url
        ? jsx(SpeckleFrame, { url, className: 'flex-1 rounded border border-(--ui-border)' })
        : jsxs('div', {
            className: 'flex flex-1 flex-col items-center justify-center gap-2 text-center text-xs text-(--ui-text-tertiary)',
            children: [
              jsx('div', { children: 'Ask Hermes about a Speckle model, or paste a model URL:' }),
              jsx('input', {
                className: 'w-full max-w-sm rounded border border-(--ui-border) bg-transparent px-2 py-1 text-xs',
                placeholder: 'https://app.speckle.systems/projects/…/models/…',
                value: draft,
                onChange: (e) => setDraft(e.target.value),
                onKeyDown: (e) => {
                  if (e.key === 'Enter' && MODEL_URL_RE.test(draft.trim())) $focus.set({ url: draft.trim(), reason: 'manual' })
                }
              })
            ]
          })
    ]
  })
}

function ModelCard({ attrs }) {
  const url = attrs && typeof attrs.url === 'string' ? attrs.url.trim() : ''
  if (!MODEL_URL_RE.test(url)) return null
  return jsxs('div', {
    className: 'my-2 overflow-hidden rounded-lg border border-(--ui-border)',
    children: [
      jsx(SpeckleFrame, { url, className: 'h-[340px] w-full' }),
      jsxs('div', {
        className: 'flex items-center gap-3 px-2 py-1 text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', { className: 'truncate', children: shortName(url) }),
          jsx('button', {
            type: 'button',
            className: 'ml-auto hover:text-(--ui-text-primary)',
            onClick: () => $focus.set({ url, reason: 'card' }),
            children: 'Open in pane'
          }),
          jsx('button', {
            type: 'button',
            className: 'hover:text-(--ui-text-primary)',
            onClick: () => ctxOs && ctxOs.openExternal(url),
            children: 'Speckle ↗'
          })
        ]
      })
    ]
  })
}

let ctxOs = null

export default {
  id: PLUGIN_ID,
  name: 'Speckle',
  register(ctx) {
    $focus = atom(null)
    $viewerSrc = atom(null)
    $command = atom(null)
    viewerCode = null
    restRef = (path, opts) => ctx.rest(path, opts)
    ctxOs = ctx.os

    loadViewerPage()

    ctx.register({
      id: 'viewer',
      area: sdk.PANES_AREA || 'panes',
      title: 'speckle',
      data: { placement: 'right', width: '520px' },
      render: () => jsx(ViewerPane, {})
    })

    if (typeof sdk.TRANSCRIPT_DIRECTIVE_AREA === 'string') {
      ctx.register({
        id: 'model-card',
        area: sdk.TRANSCRIPT_DIRECTIVE_AREA,
        data: { name: 'speckle-model', render: (props) => jsx(ModelCard, props) }
      })
    }

    ctx.onEvent(`plugin.${PLUGIN_ID}.model.focus`, ({ payload }) => {
      if (payload && payload.url) $focus.set({ url: payload.url, reason: payload.reason })
    })
    ctx.onEvent(`plugin.${PLUGIN_ID}.viewer.command`, ({ payload }) => {
      if (!payload || !payload.url || !payload.command) return
      const focus = $focus.get()
      if (!focus || focus.url !== payload.url) $focus.set({ url: payload.url, reason: 'command' })
      $command.set({ url: payload.url, command: payload.command, seq: ++commandSeq })
    })
    ctx.onEvent(`plugin.${PLUGIN_ID}.model.updated`, ({ payload }) => {
      if (!payload || !payload.url) return
      $focus.set({ url: payload.url, reason: 'new-version', headline: payload.headline })
      host.notify({ kind: 'info', message: `Speckle: new version — ${payload.headline}` })
    })
  }
}
