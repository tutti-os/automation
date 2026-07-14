import { api } from '../api';
import type { RunnerOptions } from '../types';

export async function fetchRunnerOptions(agentTargetId: string, locale: string): Promise<RunnerOptions> {
  const params = new URLSearchParams({ locale });
  const normalizedAgentTargetId = String(agentTargetId ?? '').trim();
  if (normalizedAgentTargetId) {
    params.set('agentTargetId', normalizedAgentTargetId);
  }
  return api<RunnerOptions>(`/api/runner-options?${params.toString()}`);
}

export function runnerOptionsAgentTargetId(runnerOptions: RunnerOptions): string {
  return String(runnerOptions.agentTargetId ?? '').trim();
}

export function runnerOptionsMatchAgentTarget(runnerOptions: RunnerOptions, agentTargetId: string): boolean {
  const normalizedAgentTargetId = String(agentTargetId ?? '').trim();
  if (!normalizedAgentTargetId) return true;
  return runnerOptionsAgentTargetId(runnerOptions) === normalizedAgentTargetId;
}
