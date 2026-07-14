import { describe, expect, it } from 'vitest';

import type { Automation, RunnerOptions } from '../types';
import { resolveAutomationAgentTargetId } from './runnerAgentSelection';

const agents: RunnerOptions['agents'] = [
  { agentTargetId: 'team:one', providerId: 'shared-runtime' },
  { agentTargetId: 'team:two', providerId: 'shared-runtime' },
  { agentTargetId: 'team:three', providerId: 'unique-runtime' },
];

function automation(runnerSettings: Automation['runnerSettings']) {
  return { runnerSettings } as Pick<Automation, 'runnerSettings'>;
}

describe('resolveAutomationAgentTargetId', () => {
  it('preserves an exact target without substituting a default', () => {
    expect(
      resolveAutomationAgentTargetId(
        automation({ agentTargetId: 'team:missing' }),
        { agents },
      ),
    ).toBe('team:missing');
  });

  it('migrates a legacy provider only when the full catalog match is unique', () => {
    expect(
      resolveAutomationAgentTargetId(
        automation({ provider: 'unique-runtime' }),
        { agents },
      ),
    ).toBe('team:three');
    expect(
      resolveAutomationAgentTargetId(
        automation({ provider: 'shared-runtime' }),
        { agents },
      ),
    ).toBe('');
  });
});
