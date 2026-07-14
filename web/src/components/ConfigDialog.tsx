import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Input,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Spinner,
} from '@tutti-os/ui-system';
import { ChevronDown, X } from 'lucide-react';
import { useI18n } from '../i18n';
import { fetchRunnerOptions } from '../lib/runnerOptionsApi';
import { resolveAutomationAgentTargetId } from '../lib/runnerAgentSelection';
import { PromptRichTextInput } from './PromptRichTextInput';
import { RunnerSelectMenu } from './RunnerSelectMenu';
import { TemplateIcon } from './TemplateIcon';
import {
  backendScheduleFromDraft,
  defaultScheduleDraft,
  isValidTimeOfDay,
  scheduleLabelFromDraft,
} from '../lib/schedule';
import type { ScheduleDraft } from '../types';
import { templateDefinitions, templateScheduleDraft, type TemplateDefinition } from '../lib/templates';
import type {
  AppContext,
  Automation,
  AutomationFormPayload,
  CwdOption,
  RunnerModel,
  RunnerOptions,
  RunnerPermissionMode,
  RunnerAgentTarget,
  RunnerReasoningLevel,
} from '../types';

const CUSTOM_CWD_VALUE = '__custom__';

type ConfigDialogProps = {
  automation: Automation | null;
  initialTemplate?: TemplateDefinition | null;
  context: AppContext | null;
  runnerOptions: RunnerOptions;
  isLoadingRunnerOptions: boolean;
  cwdOptions: CwdOption[];
  scheduleDraft: ScheduleDraft;
  error: string | null;
  onScheduleDraftChange: (draft: ScheduleDraft) => void;
  onClose: () => void;
  onSave: (payload: AutomationFormPayload) => void;
};

export function ConfigDialog({
  automation,
  initialTemplate = null,
  context,
  runnerOptions,
  isLoadingRunnerOptions,
  cwdOptions,
  scheduleDraft,
  error,
  onScheduleDraftChange,
  onClose,
  onSave,
}: ConfigDialogProps) {
  const { locale, t } = useI18n();
  const initialRunnerSelection = useMemo(
    () => resolveRunnerSelection(automation, runnerOptions),
    [automation, runnerOptions],
  );
  const [dialogRunnerOptions, setDialogRunnerOptions] = useState(runnerOptions);
  const [name, setName] = useState(automation?.name ?? (initialTemplate ? t(initialTemplate.nameKey) : ''));
  const [prompt, setPrompt] = useState(automation?.prompt ?? (initialTemplate ? t(initialTemplate.promptKey) : ''));
  const [cwd, setCwd] = useState(automation?.cwd ?? context?.workspaceRoot ?? '');
  const [agentTargetId, setAgentTargetId] = useState(initialRunnerSelection.agentTargetId);
  const [model, setModel] = useState(initialRunnerSelection.model);
  const [reasoningEffort, setReasoningEffort] = useState(initialRunnerSelection.reasoningEffort);
  const [permissionMode, setPermissionMode] = useState(initialRunnerSelection.permissionMode);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [cwdCustomMode, setCwdCustomMode] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [isLoadingAgentOptions, setIsLoadingAgentOptions] = useState(false);
  const customCwdInputRef = useRef<HTMLInputElement>(null);
  const isRunnerOptionsLoading = isLoadingRunnerOptions || isLoadingAgentOptions;

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown, true);
    return () => window.removeEventListener('keydown', handleKeyDown, true);
  }, [onClose]);

  useEffect(() => {
    const nextRunnerSelection = resolveRunnerSelection(automation, runnerOptions);
    const nextCwd = automation?.cwd ?? context?.workspaceRoot ?? '';
    const preferredAgentTargetId = resolveAutomationAgentTargetId(automation, runnerOptions);
    const globalAgentTargetId = normalizeText(runnerOptions.agentTargetId);
    const agentCatalogMatches =
      !preferredAgentTargetId || preferredAgentTargetId === globalAgentTargetId;
    if (agentCatalogMatches) {
      setDialogRunnerOptions(runnerOptions);
    }
    setName(automation?.name ?? (initialTemplate ? t(initialTemplate.nameKey) : ''));
    setPrompt(automation?.prompt ?? (initialTemplate ? t(initialTemplate.promptKey) : ''));
    setCwd(nextCwd);
    setCwdCustomMode(!isKnownCwdPath(nextCwd, cwdOptions));
    setAgentTargetId(nextRunnerSelection.agentTargetId);
    setModel(nextRunnerSelection.model);
    setReasoningEffort(nextRunnerSelection.reasoningEffort);
    setPermissionMode(nextRunnerSelection.permissionMode);
    setScheduleOpen(false);
    setFormError(null);
  }, [automation, context, cwdOptions, initialTemplate, runnerOptions, t]);

  useEffect(() => {
    const preferredAgentTargetId = resolveAutomationAgentTargetId(automation, runnerOptions);
    if (!preferredAgentTargetId) return;
    if (normalizeText(agentTargetId) !== preferredAgentTargetId) return;
    const loadedAgentTargetId = normalizeText(dialogRunnerOptions.agentTargetId);
    if (loadedAgentTargetId === preferredAgentTargetId && dialogRunnerOptions.available) return;

    let cancelled = false;
    setIsLoadingAgentOptions(true);
    void fetchRunnerOptions(preferredAgentTargetId, locale)
      .then((nextOptions) => {
        if (cancelled) return;
        const nextSelection = resolveRunnerSelection(automation, nextOptions);
        setDialogRunnerOptions(nextOptions);
        setAgentTargetId(nextSelection.agentTargetId);
        setModel(nextSelection.model);
        setReasoningEffort(nextSelection.reasoningEffort);
        setPermissionMode(nextSelection.permissionMode);
      })
      .catch(() => {
        if (cancelled) return;
        setFormError(t('request.failed'));
      })
      .finally(() => {
        if (!cancelled) setIsLoadingAgentOptions(false);
      });

    return () => {
      cancelled = true;
    };
  }, [automation, agentTargetId, dialogRunnerOptions.agentTargetId, dialogRunnerOptions.available, locale, runnerOptions, t]);

  const scheduleLabel = useMemo(
    () => scheduleLabelFromDraft(scheduleDraft, t, locale),
    [locale, scheduleDraft, t],
  );
  const agentTargetOptions = useMemo(
    () => availableRunnerAgentTargets(dialogRunnerOptions),
    [dialogRunnerOptions],
  );
  const selectedAgentTarget = useMemo(
    () => agentTargetOptions.find((item) => runnerAgentTargetId(item) === agentTargetId) ?? null,
    [agentTargetId, agentTargetOptions],
  );
  const modelOptions = dialogRunnerOptions.models ?? [];
  const selectedModel = useMemo(() => {
    const matched = modelOptions.find((item) => item.id === model);
    if (matched) return matched;
    const savedModel = normalizeText(model);
    if (savedModel) return { id: savedModel, label: savedModel };
    return modelOptions[0] ?? null;
  }, [model, modelOptions]);
  const reasoningOptions = selectedModel?.reasoningLevels ?? [];
  const selectedReasoning = useMemo(
    () => reasoningOptions.find((item) => runnerReasoningId(item) === reasoningEffort) ?? reasoningOptions[0] ?? null,
    [reasoningEffort, reasoningOptions],
  );
  const permissionOptions = dialogRunnerOptions.permissionConfig?.modes ?? [];
  const selectedPermission = useMemo(
    () => permissionOptions.find((item) => item.id === permissionMode) ?? permissionOptions[0] ?? null,
    [permissionMode, permissionOptions],
  );

  useEffect(() => {
    if (!dialogRunnerOptions.available || isRunnerOptionsLoading) return;
    if (normalizeText(dialogRunnerOptions.agentTargetId) !== normalizeText(agentTargetId)) return;

    const matchedModel = modelOptions.find((item) => item.id === model);
    if (!matchedModel) {
      const fallbackModel =
        modelOptions.length === 0 ? normalizeText(dialogRunnerOptions.currentModel) : '';
      if (!normalizeText(model) && fallbackModel) {
        setModel(fallbackModel);
      } else if (!normalizeText(model) && modelOptions[0]) {
        setModel(modelOptions[0].id);
      }
    }

    const nextReasoning = selectedReasoning ? runnerReasoningId(selectedReasoning) : '';
    if (nextReasoning !== reasoningEffort) setReasoningEffort(nextReasoning);
    const nextPermission = selectedPermission?.id ?? '';
    if (nextPermission !== permissionMode) setPermissionMode(nextPermission);
  }, [
    dialogRunnerOptions.available,
    dialogRunnerOptions.currentModel,
    dialogRunnerOptions.agentTargetId,
    isRunnerOptionsLoading,
    model,
    modelOptions,
    permissionMode,
    agentTargetId,
    reasoningEffort,
    selectedPermission,
    selectedReasoning,
  ]);

  const applyTemplate = (template: TemplateDefinition) => {
    setName(t(template.nameKey));
    setPrompt(t(template.promptKey));
    onScheduleDraftChange(templateScheduleDraft(template));
  };

  const selectAgentTarget = async (nextAgentTargetId: string) => {
    if (nextAgentTargetId === agentTargetId) return;
    const previousAgentTargetId = agentTargetId;
    setAgentTargetId(nextAgentTargetId);
    setModel('');
    setReasoningEffort('');
    setPermissionMode('');
    setFormError(null);
    if (!nextAgentTargetId) return;
    setIsLoadingAgentOptions(true);
    try {
      const nextOptions = await fetchRunnerOptions(nextAgentTargetId, locale);
      const nextSelection = resolveRunnerSelection(
        {
          runnerSettings: {
            agentTargetId: nextAgentTargetId,
          },
          runnerArgs: [],
        },
        nextOptions,
      );
      setDialogRunnerOptions(nextOptions);
      setAgentTargetId(nextSelection.agentTargetId);
      setModel(nextSelection.model);
      setReasoningEffort(nextSelection.reasoningEffort);
      setPermissionMode(nextSelection.permissionMode);
    } catch {
      setFormError(t('request.failed'));
      setAgentTargetId(previousAgentTargetId);
    } finally {
      setIsLoadingAgentOptions(false);
    }
  };

  const runnerSelectionReady = isRunnerSelectionReady(
    dialogRunnerOptions,
    agentTargetId,
    model,
    isRunnerOptionsLoading,
  );
  const modelsUnavailable =
    !isRunnerOptionsLoading &&
    dialogRunnerOptions.available &&
    agentTargetRequiresModel(agentTargetId) &&
    modelOptions.length === 0 &&
    !normalizeText(dialogRunnerOptions.currentModel);
  const runnerOptionsDegraded =
    !isRunnerOptionsLoading &&
    dialogRunnerOptions.available &&
    Boolean(dialogRunnerOptions.optionsUnavailable);

  const submit = () => {
    const trimmedName = name.trim();
    const trimmedPrompt = prompt.trim();
    if (!trimmedName) {
      setFormError(t('form.titleRequired'));
      return;
    }
    if (!trimmedPrompt) {
      setFormError(t('form.promptRequired'));
      return;
    }
    if (isRunnerOptionsLoading) {
      setFormError(t('loading.runnerOptions'));
      return;
    }
    if (!dialogRunnerOptions.available) {
      setFormError(t('form.runnerOptionsUnavailable'));
      return;
    }
    if (dialogRunnerOptions.optionsUnavailable) {
      setFormError(t('form.runnerOptionsDegraded'));
      return;
    }
    if (!normalizeText(agentTargetId)) {
      setFormError(t('form.agentRequired'));
      return;
    }
    if (!runnerSelectionReady) {
      setFormError(t('form.modelRequired'));
      return;
    }
    if (
      scheduleDraft.frequency !== 'hourly' &&
      scheduleDraft.frequency !== 'custom' &&
      !isValidTimeOfDay(scheduleDraft.timeOfDay)
    ) {
      setFormError(t('form.timeInvalid'));
      return;
    }
    setFormError(null);
    const scheduleConfig = backendScheduleFromDraft(scheduleDraft);
    onSave({
      name,
      prompt,
      cwd: cwd || context?.workspaceRoot || '',
      enabled: automation?.enabled ?? true,
      scheduleType: scheduleConfig.scheduleType,
      schedule: scheduleConfig.schedule,
      concurrency: 'queue',
      runnerSettings: {
        agentTargetId,
        model: model || undefined,
        reasoningEffort: reasoningEffort || undefined,
        permissionMode: permissionMode || undefined,
      },
      runnerArgs: collectRunnerArgs(dialogRunnerOptions, model, reasoningEffort, automation?.runnerArgs ?? []),
      env: automation?.env ?? {},
    });
  };

  return (
    <div
      className="dialog-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="configDialogTitle"
    >
      <section className={`config-dialog ${automation ? 'config-dialog-editing' : ''}`}>
        <Button
          variant="ghost"
          size="icon-sm"
          className="dialog-close-button"
          type="button"
          aria-label={t('common.close')}
          onClick={onClose}
        >
          <X size={16} aria-hidden="true" />
        </Button>

        {!automation ? (
          <aside className="template-panel" aria-label={t('aria.templates')}>
            <div className="template-panel-header">
              <h2>{t('templates.title')}</h2>
            </div>
            <div className="template-list">
              {templateDefinitions.map((template) => (
                <button
                  key={template.id}
                  className={`template-card template-tone-${template.id}`}
                  type="button"
                  onClick={() => applyTemplate(template)}
                >
                  <span className={`template-icon template-icon-${template.id}`} aria-hidden="true">
                    <TemplateIcon name={template.icon} />
                  </span>
                  <span className="template-card-copy">
                    <strong>{t(template.nameKey)}</strong>
                    <span>{t(template.titleKey)}</span>
                  </span>
                </button>
              ))}
            </div>
          </aside>
        ) : null}

        <section className="config-panel">
          <header className="config-header">
            <h2 id="configDialogTitle">{automation ? t('common.edit') : t('common.createAutomation')}</h2>
          </header>

          {error || formError ? <div className="error-banner">{error ?? formError}</div> : null}

          <div className="form-scroll">
            <div className="config-form-fields">
              <label className="field">
                <span>{t('form.title')}</span>
                <Input
                  className="title-input"
                  value={name}
                  autoComplete="off"
                  placeholder={t('form.titlePlaceholder')}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>

              <label className="field">
                <span>{t('form.prompt')}</span>
                <PromptRichTextInput
                  value={prompt}
                  placeholder={t('form.promptPlaceholder')}
                  workspaceId={context?.workspaceId}
                  sessionCwd={cwd || context?.workspaceRoot}
                  onChange={setPrompt}
                />
              </label>

              <div className="config-toolbar">
                <RunnerSelectMenu
                  className="cwd-menu-field"
                  label={
                    cwdCustomMode ? t('cwd.customPath') : cwdLabel(cwd, cwdOptions, context?.workspaceRoot, t)
                  }
                  title={t('aria.workingDirectory')}
                  value={cwdCustomMode ? CUSTOM_CWD_VALUE : cwd}
                  options={[
                    ...cwdOptions.map((option) => ({
                      value: option.path,
                      label: cwdOptionLabel(option, t),
                    })),
                    { value: CUSTOM_CWD_VALUE, label: t('cwd.customPath') },
                  ]}
                  onValueChange={(value) => {
                    if (value === CUSTOM_CWD_VALUE) {
                      setCwdCustomMode(true);
                      window.requestAnimationFrame(() => customCwdInputRef.current?.focus());
                      return;
                    }
                    setCwd(value);
                    setCwdCustomMode(false);
                  }}
                />
                {cwdCustomMode ? (
                  <Input
                    ref={customCwdInputRef}
                    className="cwd-custom-input"
                    value={cwd}
                    aria-label={t('aria.workingDirectory')}
                    onChange={(event) => setCwd(event.target.value)}
                  />
                ) : null}

                <Popover modal={false} open={scheduleOpen} onOpenChange={setScheduleOpen}>
                  <PopoverTrigger asChild>
                    <Button variant="ghost" size="sm" className="tool-button schedule-tool" type="button">
                      {scheduleLabel}
                      <ChevronDown size={16} data-icon="chevron-down" aria-hidden="true" />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent
                    className="schedule-panel w-auto gap-0 p-0 shadow-none"
                    align="start"
                    style={{ zIndex: 'var(--z-dialog-popover)' }}
                  >
                    <div className="schedule-section">
                      <span className="schedule-section-label">{t('form.schedule')}</span>
                      <div className="schedule-segments" role="radiogroup" aria-label={t('aria.scheduleFrequency')}>
                        {(['hourly', 'daily', 'weekdays', 'weekly'] as const).map((frequency) => (
                          <button
                            key={frequency}
                            className={`schedule-segment ${scheduleDraft.frequency === frequency ? 'active' : ''}`}
                            type="button"
                            role="radio"
                            aria-checked={scheduleDraft.frequency === frequency}
                            onClick={() =>
                              onScheduleDraftChange({
                                ...defaultScheduleDraft,
                                frequency,
                                timeOfDay: scheduleDraft.timeOfDay,
                                daysOfWeek: scheduleDraft.daysOfWeek,
                              })
                            }
                          >
                            {t(`schedule.frequency.${frequency}`)}
                          </button>
                        ))}
                      </div>
                    </div>
                    {scheduleDraft.frequency !== 'hourly' && scheduleDraft.frequency !== 'custom' ? (
                      <label className="schedule-time-field">
                        <span className="schedule-section-label">{t('form.time')}</span>
                        <input
                          type="text"
                          inputMode="numeric"
                          autoComplete="off"
                          maxLength={5}
                          value={scheduleDraft.timeOfDay}
                          placeholder="09:00"
                          onChange={(event) =>
                            onScheduleDraftChange({ ...scheduleDraft, timeOfDay: event.target.value })
                          }
                        />
                      </label>
                    ) : null}
                    {scheduleDraft.frequency === 'weekly' ? (
                      <div className="schedule-section">
                        <span className="schedule-section-label">{t('form.days')}</span>
                        <div className="schedule-day-grid" role="group" aria-label={t('aria.weeklyDays')}>
                          {[1, 2, 3, 4, 5, 6, 7].map((day) => {
                            const active = scheduleDraft.daysOfWeek.includes(day);
                            const dayKeys = [
                              'monday',
                              'tuesday',
                              'wednesday',
                              'thursday',
                              'friday',
                              'saturday',
                              'sunday',
                            ] as const;
                            return (
                              <button
                                key={day}
                                className={`schedule-day ${active ? 'active' : ''}`}
                                type="button"
                                aria-pressed={active}
                                onClick={() =>
                                  onScheduleDraftChange({
                                    ...scheduleDraft,
                                    daysOfWeek: active
                                      ? scheduleDraft.daysOfWeek.filter((value: number) => value !== day)
                                      : [...scheduleDraft.daysOfWeek, day].sort((a, b) => a - b),
                                  })
                                }
                              >
                                {t(`schedule.day.${dayKeys[day - 1]}`)}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    ) : null}
                  </PopoverContent>
                </Popover>

                {isRunnerOptionsLoading ? (
                  <div className="runner-options-loading" role="status" aria-live="polite">
                    <Spinner size={16} />
                    <span>{t('loading.runnerOptions')}</span>
                  </div>
                ) : dialogRunnerOptions.available ? (
                  <div className="runner-options">
                    <RunnerSelectMenu
                      className="runner-select-tool"
                      label={agentTargetLabel(selectedAgentTarget) || t('form.agentRequired')}
                      title={t('form.agent')}
                      value={agentTargetId}
                      options={agentTargetOptions.map((item) => ({
                        value: runnerAgentTargetId(item),
                        label: agentTargetLabel(item),
                      }))}
                      onValueChange={(value) => void selectAgentTarget(value)}
                    />

                    {modelOptions.length > 0 ? (
                      <RunnerSelectMenu
                        className="runner-select-tool"
                        label={modelLabel(selectedModel, t)}
                        title={t('form.model')}
                        value={model}
                        options={modelOptions.map((item) => ({
                          value: item.id,
                          label: modelLabel(item, t),
                          title: optionDescription(item),
                        }))}
                        onValueChange={(value) => {
                          const item = modelOptions.find((entry) => entry.id === value);
                          setModel(value);
                          setReasoningEffort(
                            item?.defaultReasoningLevel ?? runnerReasoningId(item?.reasoningLevels?.[0]) ?? '',
                          );
                        }}
                      />
                    ) : agentTargetRequiresModel(agentTargetId) && dialogRunnerOptions.currentModel ? (
                      <RunnerSelectMenu
                        className="runner-select-tool"
                        label={modelLabel({ id: model || dialogRunnerOptions.currentModel || '' }, t)}
                        title={t('form.model')}
                        value={model || dialogRunnerOptions.currentModel || ''}
                        options={[
                          {
                            value: model || dialogRunnerOptions.currentModel || '',
                            label: modelLabel({ id: model || dialogRunnerOptions.currentModel || '' }, t),
                          },
                        ]}
                        onValueChange={() => {}}
                      />
                    ) : null}

                    {modelsUnavailable ? (
                      <div className="runner-options-unavailable" role="alert">
                        {t('form.modelsUnavailable')}
                      </div>
                    ) : null}

                    {runnerOptionsDegraded ? (
                      <div className="runner-options-unavailable" role="status">
                        {t('form.runnerOptionsDegraded')}
                      </div>
                    ) : null}

                    {reasoningOptions.length > 0 ? (
                      <RunnerSelectMenu
                        className="runner-select-tool"
                        label={reasoningLabel(selectedReasoning, t)}
                        title={t('form.reasoningLevel')}
                        value={reasoningEffort}
                        options={reasoningOptions.map((item) => ({
                          value: runnerReasoningId(item),
                          label: reasoningLabel(item, t),
                          title: optionDescription(item),
                        }))}
                        onValueChange={setReasoningEffort}
                      />
                    ) : null}

                    {dialogRunnerOptions.permissionConfig?.configurable && permissionOptions.length > 0 ? (
                      <RunnerSelectMenu
                        className="runner-select-tool"
                        label={permissionModeLabel(selectedPermission, t)}
                        title={t('form.review')}
                        value={permissionMode}
                        options={permissionOptions.map((item) => ({
                          value: item.id,
                          label: permissionModeLabel(item, t),
                          title: optionDescription(item),
                        }))}
                        onValueChange={setPermissionMode}
                      />
                    ) : null}
                  </div>
                ) : (
                  <div className="runner-options-unavailable" role="status">
                    {t('form.runnerOptionsUnavailable')}
                  </div>
                )}
              </div>
            </div>
          </div>

          <footer className="config-footer">
            <div className="form-controls">
              <Button variant="ghost" size="dialog" type="button" onClick={onClose}>
                {t('common.cancel')}
              </Button>
              <Button
                className="create-submit"
                size="dialog"
                type="button"
                disabled={!runnerSelectionReady}
                onClick={submit}
              >
                {automation ? t('common.save') : t('common.create')}
              </Button>
            </div>
          </footer>
        </section>
      </section>
    </div>
  );
}

function agentTargetRequiresModel(agentTargetId: string): boolean {
  void agentTargetId;
  return false;
}

function agentTargetAllowsDefaultModel(agentTargetId: string): boolean {
  return Boolean(normalizeText(agentTargetId));
}

function isRunnerSelectionReady(
  runnerOptions: RunnerOptions,
  agentTargetId: string,
  model: string,
  isLoading: boolean,
): boolean {
  if (isLoading || !runnerOptions.available) return false;
  if (runnerOptions.optionsUnavailable) return false;
  if (normalizeText(runnerOptions.agentTargetId) !== normalizeText(agentTargetId)) return false;
  if (agentTargetAllowsDefaultModel(agentTargetId) && !normalizeText(model)) return true;
  if (!agentTargetRequiresModel(agentTargetId)) return true;
  const normalizedModel = normalizeText(model);
  if (!normalizedModel) return false;
  const modelOptions = runnerOptions.models ?? [];
  if (modelOptions.length === 0) {
    return Boolean(normalizeText(runnerOptions.currentModel));
  }
  return modelOptions.some((item) => item.id === normalizedModel);
}

function resolveRunnerSelection(
  automation:
    | Pick<Automation, 'runnerSettings' | 'runnerArgs'>
    | null
    | undefined,
  runnerOptions: RunnerOptions,
) {
  const parsed = parseRunnerArgs(automation?.runnerArgs ?? []);
  const agents = runnerAgentTargets(runnerOptions);
  const configuredAgentTargetId = normalizeText(automation?.runnerSettings?.agentTargetId);
  const configuredLegacyProvider = normalizeText(automation?.runnerSettings?.provider);
  const preferredAgentTargetId = resolveAutomationAgentTargetId(automation, runnerOptions);
  const agentTargetId =
    preferredAgentTargetId ||
    (configuredAgentTargetId || configuredLegacyProvider ? '' :
      normalizeText(runnerOptions.agentTargetId) ||
      normalizeText(runnerOptions.defaultAgentTargetId) ||
      runnerAgentTargetId(agents[0]));
  const model = normalizeText(automation?.runnerSettings?.model) || parsed.model || runnerOptions.currentModel || runnerOptions.models?.[0]?.id || '';
  const selectedModel = runnerOptions.models?.find((item) => item.id === model) ?? runnerOptions.models?.[0] ?? null;
  const reasoningEffort =
    normalizeText(automation?.runnerSettings?.reasoningEffort) ||
    parsed.reasoningEffort ||
    normalizeText(runnerOptions.currentReasoningLevel) ||
    normalizeText(selectedModel?.defaultReasoningLevel) ||
    runnerReasoningId(selectedModel?.reasoningLevels?.[0]) ||
    '';
  const permissionMode =
    normalizeText(automation?.runnerSettings?.permissionMode) ||
    normalizeText(runnerOptions.permissionMode) ||
    normalizeText(runnerOptions.permissionConfig?.defaultValue) ||
    runnerOptions.permissionConfig?.modes?.find((item) => item.current || item.effective)?.id ||
    runnerOptions.permissionConfig?.modes?.[0]?.id ||
    '';
  return {
    agentTargetId,
    model,
    reasoningEffort,
    permissionMode,
  };
}

function collectRunnerArgs(
  runnerOptions: RunnerOptions,
  model: string,
  reasoningEffort: string,
  fallbackArgs: string[],
): string[] {
  if (!runnerOptions.available) return fallbackArgs;
  const args: string[] = [];
  if (model) args.push('--model', model);
  if (reasoningEffort) args.push('-c', `model_reasoning_effort="${reasoningEffort}"`);
  return args;
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

function availableRunnerAgentTargets(runnerOptions: RunnerOptions): RunnerAgentTarget[] {
  return runnerAgentTargets(runnerOptions).filter((agent) => {
    const status = normalizeText(agent.status).toLowerCase();
    return !status || status === 'available' || status === 'ready';
  });
}

function runnerAgentTargetId(agent?: RunnerAgentTarget | null): string {
  return normalizeText(agent?.agentTargetId);
}

function runnerReasoningId(reasoning?: RunnerReasoningLevel | null): string {
  return normalizeText(reasoning?.effort) || normalizeText(reasoning?.value) || normalizeText(reasoning?.id);
}

function agentTargetLabel(agent?: RunnerAgentTarget | null): string {
  const explicit =
    normalizeText(agent?.displayName) || normalizeText(agent?.label) || normalizeText(agent?.name);
  if (explicit) return explicit;
  return runnerAgentTargetId(agent);
}

function modelLabel(model: RunnerModel | null | undefined, t: (key: string) => string): string {
  return optionLabel(model, model?.id || t('common.default'));
}

function reasoningLabel(
  reasoning: RunnerReasoningLevel | null | undefined,
  t: (key: string) => string,
): string {
  const explicit = optionLabel(reasoning, '');
  if (explicit) return explicit;
  const id = runnerReasoningId(reasoning);
  const messageKey = `reasoning.${id.toLowerCase()}`;
  const translated = t(messageKey);
  return translated === messageKey ? titleize(id) : translated;
}

function permissionModeLabel(mode: RunnerPermissionMode | null | undefined, t: (key: string) => string): string {
  const explicit = optionLabel(mode, '');
  if (explicit) return explicit;
  const id = normalizeText(mode?.id);
  const messageKey = `permission.${id}`;
  const translated = t(messageKey);
  return translated === messageKey ? titleize(id) : translated;
}

function optionLabel(option: { label?: string; name?: string; id?: string } | null | undefined, fallback: string): string {
  return normalizeText(option?.label) || normalizeText(option?.name) || normalizeText(option?.id) || fallback;
}

function optionDescription(option: { description?: string } | null | undefined): string {
  return normalizeText(option?.description);
}

function isKnownCwdPath(path: string, options: CwdOption[]): boolean {
  return options.some((option) => option.path === path);
}

function cwdOptionLabel(
  option: CwdOption,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  if (option.kind === 'worktree') return t('cwd.worktree', { label: option.label });
  return option.label;
}

function cwdLabel(
  cwd: string,
  options: CwdOption[],
  workspaceRoot: string | null | undefined,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  const selected = options.find((option) => option.path === cwd);
  if (selected) return cwdOptionLabel(selected, t);
  return cwd || workspaceRoot || '';
}

function normalizeText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function titleize(value: string): string {
  return value
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}
