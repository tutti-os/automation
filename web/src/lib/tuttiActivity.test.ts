import { afterEach, describe, expect, it, vi } from 'vitest';

import { reportUserActive } from './tuttiActivity';

describe('tutti activity bridge', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reports user activity through the host bridge', async () => {
    const reportActive = vi.fn(async () => undefined);
    vi.stubGlobal('window', { tuttiExternal: { activity: { reportActive } } });

    reportUserActive();
    await Promise.resolve();

    expect(reportActive).toHaveBeenCalledTimes(1);
  });

  it('does not throw when the host bridge is unavailable', () => {
    vi.stubGlobal('window', {});

    expect(() => reportUserActive()).not.toThrow();
  });
});
