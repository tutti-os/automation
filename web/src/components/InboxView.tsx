import { Badge, Button, Spinner } from '@tutti-os/ui-system';
import { Info } from 'lucide-react';
import { useCallback, useLayoutEffect, useRef } from 'react';
import { useI18n } from '../i18n';
import { api } from '../api';
import { formatDate, isAutomationActive } from '../lib/schedule';
import {
  isRunActive,
  runActionLabel,
  runStatusBadgeVariant,
  runStatusClass,
  runStatusLabel,
} from '../lib/runs';
import { AutomationStatusSection } from './AutomationStatusSection';
import { RunSummaryMarkdown } from './RunSummaryMarkdown';
import type { Automation, AutomationRun, InboxFilter, RunnerOptions } from '../types';
import type { KeyboardEvent, MouseEvent, RefObject } from 'react';

const filters: InboxFilter[] = ['all', 'success', 'fail', 'skip'];

function inboxFilterLabelKey(filter: InboxFilter): string {
  if (filter === 'fail') return 'filter.failed';
  if (filter === 'skip') return 'filter.skipped';
  return `filter.${filter}`;
}

type InboxViewProps = {
  automation: Automation | null;
  runs: AutomationRun[];
  filter: InboxFilter;
  isLoading: boolean;
  runnerOptions: RunnerOptions;
  statusSectionRef?: RefObject<HTMLElement | null>;
  infoButtonRef?: RefObject<HTMLButtonElement | null>;
  detailInfoOpen?: boolean;
  onFilterChange: (filter: InboxFilter) => void;
  onRefresh: () => void;
  onToggleDetailInfo?: () => void;
  onStatusSectionClick?: (event: MouseEvent<HTMLElement>) => void;
};

export function InboxView({
  automation,
  runs,
  filter,
  isLoading,
  runnerOptions,
  statusSectionRef,
  infoButtonRef,
  detailInfoOpen = false,
  onFilterChange,
  onRefresh,
  onToggleDetailInfo,
  onStatusSectionClick,
}: InboxViewProps) {
  const { t, locale } = useI18n();

  if (!automation) return null;

  const emptyKey =
    filter === 'all'
      ? 'empty.runs'
      : filter === 'success'
        ? 'empty.success'
        : filter === 'fail'
          ? 'empty.wrongs'
          : 'empty.skipped';
  const automationActive = isAutomationActive(automation);
  const hasActiveRunCard = runs.some(isRunActive);

  return (
    <section className="inbox-surface" aria-label={t('aria.automationInbox')}>
      <div className="inbox-tabs-header">
        <InboxFilterTabs filter={filter} onFilterChange={onFilterChange} />
        <Button
          ref={infoButtonRef}
          id="detailInfoButton"
          variant="ghost"
          size="icon-sm"
          className="detail-info-button inbox-tabs-info-button"
          type="button"
          aria-label={t('aria.automationStatus')}
          aria-controls="statusSection"
          aria-expanded={detailInfoOpen}
          aria-haspopup="dialog"
          onClick={(event) => {
            event.stopPropagation();
            onToggleDetailInfo?.();
          }}
        >
          <Info size={16} aria-hidden="true" />
        </Button>
      </div>

      <div className="tsh-custom-scroll-area run-inbox-scroll-area">
        <div className="run-inbox">
          {isLoading ? (
            <div className="empty-inbox inbox-loading" role="status" aria-label={t('loading.runs')}>
              <Spinner size={22} />
            </div>
          ) : (
            <>
              {automationActive && !hasActiveRunCard ? (
                <article className="run-item run-item-active" role="status" aria-live="polite">
                  <div className="run-progress">
                    <Spinner size={15} />
                    <span>{runActionLabel(automation.activeRunStatus, t)}</span>
                  </div>
                </article>
              ) : null}
              {runs.length === 0 && !automationActive ? (
                <div className="empty-inbox">
                  <h2>{t(emptyKey)}</h2>
                </div>
              ) : (
                runs.map((run) => <RunCard key={run.id} run={run} onRefresh={onRefresh} />)
              )}
            </>
          )}
        </div>
      </div>

      <AutomationStatusSection
        automation={automation}
        runnerOptions={runnerOptions}
        statusSectionRef={statusSectionRef}
        onSectionClick={onStatusSectionClick}
      />
    </section>
  );
}

function InboxFilterTabs({
  filter,
  onFilterChange,
}: {
  filter: InboxFilter;
  onFilterChange: (filter: InboxFilter) => void;
}) {
  const { t, locale } = useI18n();
  const rowRef = useRef<HTMLDivElement>(null);
  const indicatorRef = useRef<HTMLDivElement>(null);
  const tabRefs = useRef(new Map<InboxFilter, HTMLButtonElement>());

  const setTabRef = useCallback((item: InboxFilter, node: HTMLButtonElement | null) => {
    if (node) {
      tabRefs.current.set(item, node);
      return;
    }
    tabRefs.current.delete(item);
  }, []);

  const syncIndicator = useCallback(() => {
    const active = tabRefs.current.get(filter);
    const indicator = indicatorRef.current;
    if (!active || !indicator) return;

    indicator.style.transform = `translateX(${active.offsetLeft}px)`;
    indicator.style.width = `${active.offsetWidth}px`;
    if (indicator.dataset.ready === 'true') return;
    window.requestAnimationFrame(() => {
      indicator.dataset.ready = 'true';
    });
  }, [filter]);

  useLayoutEffect(() => {
    syncIndicator();
  }, [filter, locale, syncIndicator, t]);

  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row || typeof ResizeObserver === 'undefined') return;

    const observer = new ResizeObserver(() => {
      syncIndicator();
    });
    observer.observe(row);
    for (const tab of tabRefs.current.values()) {
      observer.observe(tab);
    }
    return () => observer.disconnect();
  }, [filter, locale, syncIndicator]);

  return (
    <div className="inbox-tabs" data-slot="underline-tabs" role="tablist" aria-label={t('aria.resultStatus')}>
      <div className="inbox-tabs-viewport" data-slot="underline-tabs-viewport">
        <div ref={rowRef} className="inbox-tabs-row" data-slot="underline-tabs-row">
          {filters.map((item) => (
            <button
              key={item}
              ref={(node) => setTabRef(item, node)}
              className="inbox-tab"
              type="button"
              role="tab"
              data-active={filter === item ? 'true' : 'false'}
              data-slot="underline-tabs-tab"
              aria-selected={filter === item}
              onClick={() => {
                if (filter === item) return;
                onFilterChange(item);
              }}
            >
              <span>{t(inboxFilterLabelKey(item))}</span>
            </button>
          ))}
          <div
            ref={indicatorRef}
            className="inbox-tabs-indicator"
            data-slot="underline-tabs-indicator"
            aria-hidden="true"
          />
        </div>
      </div>
    </div>
  );
}

function RunCard({ run, onRefresh }: { run: AutomationRun; onRefresh: () => void }) {
  const { t, locale } = useI18n();
  const hasAgentSession = Boolean(run.agentSessionId);
  const showProgress = isRunActive(run) && !run.summary?.trim() && !run.error?.trim();

  const openAgentSession = async () => {
    if (!run.agentSessionId) return;
    try {
      await api(`/api/runs/${encodeURIComponent(run.id)}/open-agent`, { method: 'POST' });
      onRefresh();
    } catch {
      // Errors surface on next refresh; keep card interaction silent like the original.
    }
  };

  const handleCardClick = (event: MouseEvent<HTMLElement>) => {
    if (!hasAgentSession) return;
    if ((event.target as HTMLElement).closest('a, button, input, select, textarea')) return;
    void openAgentSession();
  };

  const handleCardKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (!hasAgentSession) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    void openAgentSession();
  };

  const resultContent = showProgress ? (
    <div className="run-progress">
      <Spinner size={15} />
      <span>{runActionLabel(run.runStatus, t)}</span>
    </div>
  ) : (
    <RunSummaryMarkdown content={run.summary?.trim() || run.error?.trim() || t('result.empty')} />
  );

  return (
    <article
      className={`run-item${hasAgentSession ? ' has-agent-session' : ''}`}
      role={hasAgentSession ? 'button' : undefined}
      tabIndex={hasAgentSession ? 0 : undefined}
      aria-label={hasAgentSession ? t('aria.openAgentSession') : undefined}
      onClick={handleCardClick}
      onKeyDown={handleCardKeyDown}
    >
      <header className="run-header">
        <div className="run-time">
          <strong>{formatDate(run.startedAt ?? run.queuedAt, locale)}</strong>
        </div>
        <div className="run-header-actions">
          <Badge
            variant={runStatusBadgeVariant(run)}
            className="run-status"
            data-status={runStatusClass(run)}
          >
            {runStatusLabel(run, t)}
          </Badge>
        </div>
      </header>
      <div className="run-result rich-text markdown-body">{resultContent}</div>
    </article>
  );
}
