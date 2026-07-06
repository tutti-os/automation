import { afterEach, describe, expect, it, vi } from 'vitest';

import { handleAutomationEventMessage } from './automationEvents';
import { reportUserActive } from './tuttiActivity';

vi.mock('./tuttiActivity', () => ({
  reportUserActive: vi.fn(),
}));

describe('automation event handling', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('reports active app when an automation is created from any source', () => {
    const refresh = vi.fn();

    handleAutomationEventMessage(
      new MessageEvent('message', {
        data: JSON.stringify({
          type: 'automation_changed',
          action: 'created',
          automationId: 'aut_1',
        }),
      }),
      refresh,
    );

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(reportUserActive).toHaveBeenCalledTimes(1);
  });

  it('does not report active app for non-create events', () => {
    const refresh = vi.fn();

    handleAutomationEventMessage(
      new MessageEvent('message', {
        data: JSON.stringify({
          type: 'automation_changed',
          action: 'updated',
          automationId: 'aut_1',
        }),
      }),
      refresh,
    );

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(reportUserActive).not.toHaveBeenCalled();
  });
});
