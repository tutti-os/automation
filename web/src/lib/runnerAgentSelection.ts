import type { Automation, RunnerOptions } from '../types';

function normalizeText(value: string | null | undefined): string {
  return String(value ?? '').trim();
}

/**
 * Resolve only a persisted selection. Exact ids are preserved even when stale;
 * deprecated providers migrate only when the complete catalog has one match.
 */
export function resolveAutomationAgentTargetId(
  automation: Pick<Automation, 'runnerSettings'> | null | undefined,
  runnerOptions: Pick<RunnerOptions, 'agents'>,
): string {
  const exact = normalizeText(automation?.runnerSettings?.agentTargetId);
  if (exact) return exact;
  const legacyProvider = normalizeText(automation?.runnerSettings?.provider);
  if (!legacyProvider) return '';
  const matches = (runnerOptions.agents ?? []).filter(
    (agent) => normalizeText(agent.providerId) === legacyProvider,
  );
  return matches.length === 1
    ? normalizeText(matches[0]?.agentTargetId)
    : '';
}
