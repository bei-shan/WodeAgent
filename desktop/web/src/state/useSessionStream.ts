import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'
import * as api from '../api'
import type { AgentEvent } from '../api'

type HistoryMessages = Awaited<ReturnType<typeof api.sessions.history>>['messages']

interface StreamBindings {
  activeSidRef: MutableRefObject<string | null>
  onEvent: (event: AgentEvent) => void
  onBusy: (busy: boolean) => void
  onRestore: (messages: HistoryMessages) => void
}

/**
 * Own one WebSocket stream and its reconnection epoch.
 *
 * Every callback, including the asynchronous history refresh following a
 * reconnect, is scoped to the same connection generation AND session id.
 * This prevents a stale socket from changing the newly selected session.
 */
export function useSessionStream(bindings: StreamBindings) {
  const handlers = useRef(bindings)
  handlers.current = bindings
  const disconnectRef = useRef<(() => void) | null>(null)
  const generationRef = useRef(0)
  const [connected, setConnected] = useState(false)

  const close = useCallback(() => {
    generationRef.current += 1
    disconnectRef.current?.()
    disconnectRef.current = null
  }, [])

  const disconnect = useCallback(() => {
    close()
    setConnected(false)
  }, [close])

  const connect = useCallback((sid: string, replayFromStart = false) => {
    close()
    setConnected(false)
    const generation = generationRef.current
    const valid = () =>
      generationRef.current === generation && handlers.current.activeSidRef.current === sid
    let previouslyConnected = false

    disconnectRef.current = api.connectStream(
      sid,
      event => {
        if (valid()) handlers.current.onEvent(event)
      },
      () => {
        if (valid()) setConnected(false)
      },
      () => {
        if (!valid()) return
        setConnected(true)
        if (previouslyConnected) {
          // The journal may have lost the final event during disconnection.
          // Restore durable status and history without leaking across epochs.
          void api.sessions.get(sid).then(meta => {
            if (!valid()) return
            handlers.current.onBusy(meta.busy)
            if (!meta.busy) {
              void api.sessions.history(sid).then(data => {
                if (valid() && data.messages.length > 0) {
                  handlers.current.onRestore(data.messages)
                }
              }).catch(() => {})
            }
          }).catch(() => {})
        }
        previouslyConnected = true
      },
      replayFromStart,
    )
  }, [close])

  useEffect(() => () => close(), [close])

  return { connected, connect, disconnect }
}
