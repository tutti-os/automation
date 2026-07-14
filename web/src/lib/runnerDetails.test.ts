import { describe, expect, it } from 'vitest';

import type { Automation, RunnerOptions } from '../types';
import { runnerDetailsFromSettings } from './runnerDetails';

const runnerOptions: RunnerOptions = {
  available: true,
  agentTargetId: 'team:primary',
  defaultAgentTargetId: 'team:primary',
  agents: [
    {
      agentTargetId: 'team:primary',
      providerId: 'shared-runtime',
      displayName: 'Primary Agent',
    },
    {
      agentTargetId: 'team:reviewer',
      providerId: 'shared-runtime',
      displayName: 'Review Agent',
    },
  ],
  models: [],
};

function automation(runnerSettings: Automation['runnerSettings']): Automation {
  return {
    id: 'automation-1',
    name: 'Review',
    prompt: 'Review the workspace.',
    cwd: '/workspace',
    enabled: true,
    scheduleType: 'daily',
    schedule: {},
    concurrency: 'queue',
    runnerSettings,
    runnerArgs: [],
    env: {},
    createdAt: '',
    updatedAt: '',
    nextRunAt: null,
  };
}

const translate = (key: string) => key;

describe('runnerDetailsFromSettings', () => {
  it('uses the exact Agent Target identity', () => {
    const details = runnerDetailsFromSettings(
      automation({ agentTargetId: 'team:reviewer' }),
      runnerOptions,
      translate,
    );

    expect(details.agent).toBe('Review Agent');
  });

  it('does not silently select a default for an ambiguous legacy provider', () => {
    const details = runnerDetailsFromSettings(
      automation({ provider: 'shared-runtime' }),
      runnerOptions,
      translate,
    );

    expect(details.agent).toBe('');
  });
});
