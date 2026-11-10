/**
 * Slack's Connections card needs a client saved under Settings → OAuth Apps.
 * Until one is, the gateway marks a `slack` row `connectionUnconfigured` and the
 * card leaves it alone: the row keeps the chat guidance, where the client the
 * user wrote in the entry signs in, instead of a Sign in that could only fail.
 * Once the record exists the card claims the row again.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { McpServer } from '../types'

const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpDiscover: vi.fn(),
  mcpProbe: vi.fn(),
  mcpApply: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  mcpResetProbeFailures: vi.fn(),
  kirocrewConfig: vi.fn(),
  acpBackends: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

const slackRow = (overrides: Partial<McpServer>): McpServer => ({
  name: 'slack',
  command: '',
  url: 'https://mcp.slack.com/mcp',
  status: 'needs_auth',
  source: 'mcp.json',
  enabled: true,
  tools: [],
  authChallenge: true,
  authGrantPresent: false,
  ...overrides,
})

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kiro' } })
  mockApi.acpBackends.mockResolvedValue({ backends: [], kiro_selection_runs_on_kas: false })
})

describe('McpTab: a slack row and its Connections card', () => {
  it('keeps the chat guidance while no OAuth app is saved', async () => {
    mockApi.mcpServers.mockResolvedValue([slackRow({ connectionUnconfigured: true })])
    renderTab()

    await waitFor(() => expect(screen.getByRole('link', { name: /Go to chat/ })).toBeInTheDocument())
    expect(screen.getByText('Sign-in required')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull()
    expect(screen.queryByText('Managed by Connections')).toBeNull()
  })

  it('belongs to the card once the OAuth app is saved', async () => {
    mockApi.mcpServers.mockResolvedValue([slackRow({})])
    renderTab()

    await waitFor(() => expect(screen.getByText('Managed by Connections')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Go to chat/ })).toBeNull()
  })
})
