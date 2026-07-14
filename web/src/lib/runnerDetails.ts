import type {
  Automation,
  RunnerModel,
  RunnerOptions,
  RunnerPermissionMode,
  RunnerAgentTarget,
  RunnerReasoningLevel,
} from '../types';
import { resolveAutomationAgentTargetId } from './runnerAgentSelection';

export type RunnerDisplayDetails = {
  agent: string;
  model: string;
  reasoning: string;
  review: string;
  showReasoning: boolean;
  showReview: boolean;
};

function normalizeText(value: string | null | undefined): string {
  return String(value ?? '').trim();
}

function titleize(value: string): string {
  if (!value) return value;
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function optionLabel(
  option: { label?: string; name?: string; id?: string } | null | undefined,
  fallback: string,
): string {
  return normalizeText(option?.label) || normalizeText(option?.name) || normalizeText(option?.id) || fallback;
}

function parseRunnerArgs(args: readonly string[]) {
  const result = { model: '', reasoningEffort: '' };
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index] ?? '';
    if ((arg === '--model' || arg === '-m') && args[index + 1]) {
      result.model = args[index + 1] ?? '';
      index += 1;
      continue;
    }
    if (arg.startsWith('--model=')) {
      result.model = arg.slice('--model='.length);
      continue;
    }
    if ((arg === '--config' || arg === '-c') && args[index + 1]) {
      readReasoningConfig(args[index + 1] ?? '', result);
      index += 1;
      continue;
    }
    if (arg.startsWith('--config=')) readReasoningConfig(arg.slice('--config='.length), result);
  }
  return result;
}

function readReasoningConfig(value: string, result: { reasoningEffort: string }) {
  const match = value.match(/^model_reasoning_effort=(.*)$/);
  if (match) result.reasoningEffort = (match[1] ?? '').replace(/^["']|["']$/g, '');
}

function runnerAgentTargets(runnerOptions: RunnerOptions): RunnerAgentTarget[] {
  return runnerOptions.agents ?? [];
}

function runnerAgentTargetId(agent?: RunnerAgentTarget | null): string {
  return normalizeText(agent?.agentTargetId);
}

function runnerReasoningId(reasoning?: RunnerReasoningLevel | null): string {
  return normalizeText(reasoning?.effort) || normalizeText(reasoning?.value) || normalizeText(reasoning?.id);
}

function agentTargetLabel(agent?: RunnerAgentTarget | null): string {
  const explicit = normalizeText(agent?.displayName) || normalizeText(agent?.label) || normalizeText(agent?.name);
  if (explicit) return explicit;
  return runnerAgentTargetId(agent);
}

function agentTargetLabelForId(agentTargetId: string, runnerOptions: RunnerOptions): string {
  const matched = runnerAgentTargets(runnerOptions).find(
    (item) => runnerAgentTargetId(item) === agentTargetId,
  );
  return matched ? agentTargetLabel(matched) : agentTargetId;
}

function modelLabel(model: RunnerModel | null | undefined, t: (key: string) => string): string {
  return optionLabel(model, model?.id || t('common.default'));
}

function reasoningLabel(
  reasoning: RunnerReasoningLevel | string | null | undefined,
  t: (key: string) => string,
): string {
  if (typeof reasoning === 'string') {
    const messageKey = `reasoning.${reasoning.toLowerCase()}`;
    const translated = t(messageKey);
    return translated === messageKey ? titleize(reasoning) : translated;
  }
  const explicit = optionLabel(reasoning, '');
  if (explicit) return explicit;
  const id = runnerReasoningId(reasoning);
  const messageKey = `reasoning.${id.toLowerCase()}`;
  const translated = t(messageKey);
  return translated === messageKey ? titleize(id) : translated;
}

function permissionModeLabel(
  mode: RunnerPermissionMode | string | null | undefined,
  t: (key: string) => string,
): string {
  if (typeof mode === 'string') {
    const messageKey = `permission.${mode}`;
    const translated = t(messageKey);
    return translated === messageKey ? titleize(mode) : translated;
  }
  const explicit = optionLabel(mode, '');
  if (explicit) return explicit;
  const id = normalizeText(mode?.id);
  const messageKey = `permission.${id}`;
  const translated = t(messageKey);
  return translated === messageKey ? titleize(id) : translated;
}

function findRunnerModel(runnerOptions: RunnerOptions, modelId: string): RunnerModel | null {
  return runnerOptions.models?.find((item) => item.id === modelId) ?? runnerOptions.models?.[0] ?? null;
}

function findReasoningLevel(
  runnerOptions: RunnerOptions,
  reasoningId: string,
  model?: RunnerModel | null,
): RunnerReasoningLevel | null {
  const levels = model?.reasoningLevels ?? runnerOptions.models?.[0]?.reasoningLevels ?? [];
  return levels.find((item) => runnerReasoningId(item) === reasoningId) ?? null;
}

function findPermissionMode(
  runnerOptions: RunnerOptions,
  modeId: string,
): RunnerPermissionMode | null {
  return runnerOptions.permissionConfig?.modes?.find((item) => normalizeText(item.id) === modeId) ?? null;
}

function resolveRunnerSelection(
  automation: Pick<Automation, 'runnerSettings' | 'runnerArgs'>,
  runnerOptions: RunnerOptions,
) {
  const parsed = parseRunnerArgs(automation.runnerArgs ?? []);
  const agents = runnerAgentTargets(runnerOptions);
  const exactAgentTargetId = normalizeText(automation.runnerSettings?.agentTargetId);
  const legacyProvider = normalizeText(automation.runnerSettings?.provider);
  const preferredAgentTargetId = resolveAutomationAgentTargetId(automation, runnerOptions);
  const agentTargetId =
    preferredAgentTargetId ||
    (exactAgentTargetId || legacyProvider ? '' :
      normalizeText(runnerOptions.agentTargetId) ||
      normalizeText(runnerOptions.defaultAgentTargetId) ||
      runnerAgentTargetId(agents[0]) ||
      '');
  const runnerOptionsMatchAgentTarget =
    !normalizeText(runnerOptions.agentTargetId) ||
    normalizeText(runnerOptions.agentTargetId) === agentTargetId;
  const model =
    normalizeText(automation.runnerSettings?.model) ||
    parsed.model ||
    (runnerOptionsMatchAgentTarget ? normalizeText(runnerOptions.currentModel) : '') ||
    (runnerOptionsMatchAgentTarget ? runnerOptions.models?.[0]?.id ?? '' : '') ||
    '';
  const selectedModel = runnerOptionsMatchAgentTarget ? findRunnerModel(runnerOptions, model) : null;
  const reasoningEffort =
    normalizeText(automation.runnerSettings?.reasoningEffort) ||
    parsed.reasoningEffort ||
    (runnerOptionsMatchAgentTarget ? normalizeText(runnerOptions.currentReasoningLevel) : '') ||
    (runnerOptionsMatchAgentTarget ? normalizeText(selectedModel?.defaultReasoningLevel) : '') ||
    (runnerOptionsMatchAgentTarget ? runnerReasoningId(selectedModel?.reasoningLevels?.[0]) : '') ||
    '';
  const permissionMode =
    normalizeText(automation.runnerSettings?.permissionMode) ||
    (runnerOptionsMatchAgentTarget ? normalizeText(runnerOptions.permissionMode) : '') ||
    (runnerOptionsMatchAgentTarget ? normalizeText(runnerOptions.permissionConfig?.defaultValue) : '') ||
    (runnerOptionsMatchAgentTarget ?
      runnerOptions.permissionConfig?.modes?.find((item) => item.current || item.effective)?.id
    : '') ||
    (runnerOptionsMatchAgentTarget ? runnerOptions.permissionConfig?.modes?.[0]?.id ?? '' : '') ||
    '';
  return { agentTargetId, model, reasoningEffort, permissionMode, selectedModel };
}

export function runnerDetailsFromSettings(
  automation: Pick<Automation, 'runnerSettings' | 'runnerArgs'>,
  runnerOptions: RunnerOptions,
  t: (key: string) => string,
): RunnerDisplayDetails {
  const selection = resolveRunnerSelection(automation, runnerOptions);
  const agent = agentTargetLabelForId(selection.agentTargetId, runnerOptions);
  const reasoningId = selection.reasoningEffort;
  const reviewMode = selection.permissionMode;

  if (!runnerOptions.available) {
    const reasoning = reasoningId ? reasoningLabel(reasoningId, t) : '';
    const review = reviewMode ? permissionModeLabel(reviewMode, t) : '';
    return {
      agent,
      model: selection.model || t('common.default'),
      reasoning,
      review,
      showReasoning: Boolean(reasoning),
      showReview: Boolean(review),
    };
  }

  const model = selection.selectedModel;
  const reasoning = reasoningId
    ? reasoningLabel(findReasoningLevel(runnerOptions, reasoningId, model) ?? reasoningId, t)
    : '';
  const review = reviewMode
    ? permissionModeLabel(findPermissionMode(runnerOptions, reviewMode) ?? reviewMode, t)
    : '';

  return {
    agent,
    model: model ? modelLabel(model, t) : selection.model || t('common.default'),
    reasoning,
    review,
    showReasoning: Boolean(reasoning),
    showReview: Boolean(review),
  };
}
