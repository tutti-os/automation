import type { AutomationRun, InboxFilter } from '../types';

const ACTIVE_RUN_STATUSES = new Set(['queued', 'running', 'canceling']);

export function isRunActive(run: AutomationRun): boolean {
  return ACTIVE_RUN_STATUSES.has(String(run.runStatus || '').toLowerCase());
}

export function runActionLabel(
  status: string | null | undefined,
  t: (key: string) => string,
): string {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'queued') return t('run.status.queued');
  if (normalized === 'canceling') return t('run.status.canceling');
  return t('run.status.running');
}

export function normalizedResultStatus(run: AutomationRun): string {
  if (isRunActive(run)) return run.runStatus || 'queued';
  const value = String(run.taskStatus || '').toLowerCase();
  if (['success', 'fail', 'skip'].includes(value)) return value;
  if (run.runStatus === 'succeeded') return 'success';
  if (['failed', 'timed_out'].includes(run.runStatus)) return 'fail';
  return run.runStatus || 'queued';
}

export function runStatusClass(run: AutomationRun): string {
  return normalizedResultStatus(run).replaceAll('_', '-');
}

export function runMatchesInboxFilter(run: AutomationRun, filter: InboxFilter): boolean {
  if (filter === 'all') return true;
  const status = normalizedResultStatus(run);
  if (filter === 'fail') return status === 'fail' || status === 'canceled';
  return status === filter;
}

export function runStatusLabel(
  run: AutomationRun,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  const status = normalizedResultStatus(run);
  const key = `run.status.${status}`;
  const translated = t(key);
  if (translated !== key) return translated;
  return status.charAt(0).toUpperCase() + status.slice(1);
}

export function runStatusBadgeVariant(run: AutomationRun): 'destructive' | 'default' {
  return normalizedResultStatus(run) === 'fail' ? 'destructive' : 'default';
}
