import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ShieldCheck } from 'lucide-react'
import { api } from '../../api/client'
import type { McpToolGrants } from '../../api/client/mcp'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { refusalText } from '../../utils/toolGrantRefusal'

/** Per-tool "Always allow" on a server's tool list. The same `@server/tool` grant
 *  an approval card's "Always allow this tool" records, so both show and revoke
 *  one list. The server decides eligibility: its `refusal` code disables every
 *  toggle of that server and names why, in the note above the list. */

/** Server and tool names a grant can name exactly: no wildcard, no `/`. */
export function toolGrantRef(server: string, tool: string): string {
  const exact = (name: string) => !!name && !['*', '?', '[', '/'].some(ch => name.includes(ch)) && name.trim() === name
  return exact(server) && exact(tool) ? `@${server}/${tool}` : ''
}

export const toolGrantsKey = (server: string) => ['mcp-tool-grants', server]

export function useToolGrants(server: string) {
  return useQuery({ queryKey: toolGrantsKey(server), queryFn: () => api.mcpToolGrants(server) })
}

/** The one line above a server's tools when its tools cannot be always allowed. */
export function McpToolGrantsNote({ server }: { server: string }) {
  const { data } = useToolGrants(server)
  const text = refusalText(data?.refusal)
  if (!text) return null
  return <div className="text-[12px] text-muted mb-1" data-testid="mcp-always-allow-refusal">{text}</div>
}

export default function McpToolAlwaysAllow({ server, tool }: { server: string; tool: string }) {
  const qc = useQueryClient()
  const { data } = useToolGrants(server)
  const [failed, setFailed] = useState('')
  const ref = toolGrantRef(server, tool)
  const on = !!ref && !!data?.grants.some(g => g.ref === ref)
  const refusal = refusalText(data?.refusal)
  const set = useMutation({
    mutationFn: (next: boolean) => api.mcpToolGrantSet(ref, next),
    onSuccess: (snap: McpToolGrants) => { setFailed(''); qc.setQueryData(toolGrantsKey(server), snap) },
    onError: (err: unknown) => { setFailed(err instanceof Error ? err.message : ''); qc.invalidateQueries({ queryKey: toolGrantsKey(server) }) },
  })
  if (!ref) return null
  // A grant already in force stays removable even when new ones are refused.
  const disabled = !data || set.isPending || (!!refusal && !on)
  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        role="switch"
        aria-checked={on}
        disabled={disabled}
        title={refusal && !on ? refusal : i18nT('pages.overview.mcpTab.always_allow_hint')}
        onClick={() => set.mutate(!on)}
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[11px] cursor-pointer transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${on ? 'border-ok text-ok' : 'border-border text-muted hover:text-text'}`}
      >
        <ShieldCheck size={11} className="shrink-0" />
        {i18nT('pages.overview.mcpTab.always_allow')}
      </button>
      {failed !== '' || set.isError ? <ErrorNotice variant="inline" message={i18nT('pages.overview.mcpTab.always_allow_failed')} /> : null}
    </span>
  )
}
