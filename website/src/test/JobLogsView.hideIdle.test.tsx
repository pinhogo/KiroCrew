import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobLogsView from '../components/JobLogsView'
import { api } from '../api/client'

// A script job that polls every few minutes ends nearly every run in Skip(), so
// its Logs tab buries the runs that acted under pages of idle rows. "Hide idle
// runs" asks the server to leave them out before paging, says how many it hid,
// and is remembered for the next job opened.

const run = (id: string, summary: string) => ({ run_id: id, status: 'success', started_at: 1_700_000_000, trigger: 'scheduled', summary })

vi.mock('../api/client', () => ({
  api: {
    cronHistory: vi.fn(async (_job: string, _offset?: number, _limit?: number, hideIdle?: boolean) =>
      hideIdle
        ? { runs: [run('r2', 'acted')], total: 1, idle_hidden: 98 }
        : { runs: [run('r1', 'idle tick'), run('r2', 'acted')], total: 2, idle_hidden: 0 }),
  },
}))

describe('JobLogsView hide idle runs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('hides idle runs on request, shows the count and remembers the choice', async () => {
    const view = renderWithProviders(<JobLogsView jobId="job-1" canHideIdle />)
    expect(await screen.findByText('idle tick')).toBeInTheDocument()
    const toggle = screen.getByRole('checkbox', { name: 'Hide idle runs' })
    expect(toggle).not.toBeChecked()
    expect(api.cronHistory).toHaveBeenLastCalledWith('job-1', 0, 10, false)

    fireEvent.click(toggle)
    expect(await screen.findByText('98 idle runs hidden')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('idle tick')).not.toBeInTheDocument())
    expect(screen.getByText('acted')).toBeInTheDocument()
    expect(api.cronHistory).toHaveBeenLastCalledWith('job-1', 0, 10, true)

    view.unmount()
    renderWithProviders(<JobLogsView jobId="job-2" canHideIdle />)
    expect(await screen.findByRole('checkbox', { name: 'Hide idle runs' })).toBeChecked()
    await waitFor(() => expect(api.cronHistory).toHaveBeenLastCalledWith('job-2', 0, 10, true))
  })

  it('offers no toggle and hides nothing for a job that cannot Skip', async () => {
    localStorage.setItem('kc-cron-logs-hide-idle', '1')
    renderWithProviders(<JobLogsView jobId="job-1" />)
    expect(await screen.findByText('idle tick')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(api.cronHistory).toHaveBeenLastCalledWith('job-1', 0, 10, false)
  })
})
