/**
 * The MCP tool list's per-tool "Always allow" toggle and the approval card's
 * "Always allow this tool" row. Both read only server facts: the grant list and
 * refusal code from `/api/security/tool-grants`, and the card's
 * `always_allow_tool_ref`. `i18nT` is mocked to the key so assertions pin keys.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { grants, setGrant } = vi.hoisted(() => ({ grants: vi.fn(), setGrant: vi.fn() }))

vi.mock('../../api/client', () => ({
  api: {
    mcpToolGrants: (...a: unknown[]) => grants(...a),
    mcpToolGrantSet: (...a: unknown[]) => setGrant(...a),
  },
}))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return { ...orig, i18nT: (key: string) => key }
})

import McpToolAlwaysAllow, { McpToolGrantsNote, toolGrantRef } from './McpToolAlwaysAllow'
import TrustDropdown from '../../components/TrustDropdown'

const wrap = (node: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>)
}

describe('McpToolAlwaysAllow', () => {
  beforeEach(() => { grants.mockReset(); setGrant.mockReset() })

  it('turns the grant on with the exact @server/tool ref', async () => {
    grants.mockResolvedValue({ grants: [], server: 'notes', refusal: '' })
    setGrant.mockResolvedValue({ grants: [{ ref: '@notes/lookup' }], server: 'notes', refusal: '' })
    wrap(<McpToolAlwaysAllow server="notes" tool="lookup" />)
    const sw = await screen.findByRole('switch')
    await waitFor(() => expect(sw).not.toBeDisabled())
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    await waitFor(() => expect(setGrant).toHaveBeenCalledWith('@notes/lookup', true))
    await waitFor(() => expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true'))
  })

  it('turns an existing grant off', async () => {
    grants.mockResolvedValue({ grants: [{ ref: '@notes/lookup' }], server: 'notes', refusal: '' })
    setGrant.mockResolvedValue({ grants: [], server: 'notes', refusal: '' })
    wrap(<McpToolAlwaysAllow server="notes" tool="lookup" />)
    const sw = await screen.findByRole('switch')
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(sw)
    await waitFor(() => expect(setGrant).toHaveBeenCalledWith('@notes/lookup', false))
  })

  it('is disabled with the readable reason when the server refuses', async () => {
    grants.mockResolvedValue({ grants: [], server: 'notes', refusal: 'asks_every_time' })
    wrap(<><McpToolGrantsNote server="notes" /><McpToolAlwaysAllow server="notes" tool="lookup" /></>)
    expect(await screen.findByTestId('mcp-always-allow-refusal')).toHaveTextContent(
      'pages.overview.mcpTab.always_allow_refused_asks_every_time')
    expect(screen.getByRole('switch')).toBeDisabled()
  })

  it('offers nothing for a name that is not one exact tool', () => {
    expect(toolGrantRef('notes', 'look*')).toBe('')
    expect(toolGrantRef('a/b', 'x')).toBe('')
    expect(toolGrantRef('notes', 'lookup')).toBe('@notes/lookup')
  })
})

describe('TrustDropdown always-allow row', () => {
  it('fires always_allow_tool with the server-derived ref', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="" baseCommand="" isShell={false} hasCommand={false} showTrustAll={false} alwaysAllowTool="@notes/lookup" onAction={onAction} />)
    fireEvent.click(screen.getByRole('button', { name: /components.trustDropdown.always_allow_this_tool/ }))
    expect(onAction).toHaveBeenCalledWith('always_allow_tool', '@notes/lookup')
  })

  it('shows the refusal and does not fire when only a reason is given', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="" baseCommand="" isShell={false} hasCommand={false} showTrustAll={false} alwaysAllowRefusal="asks every time" onAction={onAction} />)
    const btn = screen.getByRole('button', { name: /always_allow_this_tool/ })
    expect(btn).toBeDisabled()
    expect(btn).toHaveTextContent('asks every time')
    fireEvent.click(btn)
    expect(onAction).not.toHaveBeenCalled()
  })
})
