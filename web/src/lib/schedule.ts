import type { Automation, ScheduleDraft } from '../types';

export const defaultScheduleDraft: ScheduleDraft = {
  frequency: 'daily',
  timeOfDay: '09:00',
  daysOfWeek: [1],
  cronExpression: '0 9 * * *',
};

export const schedulePresets: Record<string, ScheduleDraft> = {
  'daily-0900': {
    frequency: 'daily',
    timeOfDay: '09:00',
    daysOfWeek: [1],
    cronExpression: '0 9 * * *',
  },
  'weekday-0900': {
    frequency: 'weekdays',
    timeOfDay: '09:00',
    daysOfWeek: [1, 2, 3, 4, 5],
    cronExpression: '0 9 * * 1-5',
  },
  'weekly-0900': {
    frequency: 'weekly',
    timeOfDay: '09:00',
    daysOfWeek: [1],
    cronExpression: '0 9 * * 1',
  },
  hourly: {
    frequency: 'hourly',
    timeOfDay: '09:00',
    daysOfWeek: [1],
    cronExpression: '0 * * * *',
  },
};

export function normalizeTimeOfDay(value: unknown): string {
  const text = String(value ?? '09:00').trim();
  const match = text.match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return '09:00';
  const hour = Math.min(23, Math.max(0, Number(match[1])));
  const minute = Math.min(59, Math.max(0, Number(match[2])));
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

export function normalizeDaysOfWeek(value: unknown): number[] {
  if (!Array.isArray(value) || value.length === 0) return [1];
  return [...new Set(value.map((day) => Number(day)).filter((day) => day >= 1 && day <= 7))].sort(
    (a, b) => a - b,
  );
}

export function scheduleDraftFromAutomation(item: Automation): ScheduleDraft {
  if (item.scheduleType === 'interval' && item.schedule?.intervalMinutes === 60) {
    return schedulePresets.hourly;
  }
  if (item.scheduleType === 'daily') {
    return {
      frequency: 'daily',
      timeOfDay: normalizeTimeOfDay(item.schedule?.timeOfDay),
      daysOfWeek: [1],
      cronExpression: '0 9 * * *',
    };
  }
  if (item.scheduleType === 'weekly') {
    const days = normalizeDaysOfWeek(item.schedule?.daysOfWeek);
    const timeOfDay = normalizeTimeOfDay(item.schedule?.timeOfDay);
    if (days.join(',') === '1,2,3,4,5') {
      return { frequency: 'weekdays', timeOfDay, daysOfWeek: days, cronExpression: '0 9 * * 1-5' };
    }
    return { frequency: 'weekly', timeOfDay, daysOfWeek: days, cronExpression: '0 9 * * 1' };
  }
  if (item.scheduleType === 'cron') {
    return {
      frequency: 'custom',
      timeOfDay: '09:00',
      daysOfWeek: [1],
      cronExpression: String(item.schedule?.expression ?? '0 9 * * *'),
    };
  }
  return defaultScheduleDraft;
}

export function backendScheduleFromDraft(draft: ScheduleDraft): {
  scheduleType: string;
  schedule: Record<string, unknown>;
} {
  if (draft.frequency === 'hourly') {
    return { scheduleType: 'interval', schedule: { intervalMinutes: 60 } };
  }
  if (draft.frequency === 'daily') {
    return { scheduleType: 'daily', schedule: { timeOfDay: normalizeTimeOfDay(draft.timeOfDay) } };
  }
  if (draft.frequency === 'weekdays') {
    return {
      scheduleType: 'weekly',
      schedule: {
        daysOfWeek: [1, 2, 3, 4, 5],
        timeOfDay: normalizeTimeOfDay(draft.timeOfDay),
      },
    };
  }
  if (draft.frequency === 'weekly') {
    return {
      scheduleType: 'weekly',
      schedule: {
        daysOfWeek: normalizeDaysOfWeek(draft.daysOfWeek),
        timeOfDay: normalizeTimeOfDay(draft.timeOfDay),
      },
    };
  }
  if (draft.frequency === 'custom') {
    return { scheduleType: 'cron', schedule: { expression: draft.cronExpression.trim() || '0 9 * * *' } };
  }
  return { scheduleType: 'daily', schedule: { timeOfDay: normalizeTimeOfDay(draft.timeOfDay) } };
}

export function automationScheduleLabel(
  item: Automation,
  t: (key: string, params?: Record<string, string | number>) => string,
  locale?: string,
): string {
  if (item.scheduleType === 'manual') return t('schedule.manual');
  return scheduleLabelFromDraft(scheduleDraftFromAutomation(item), t, locale);
}

export function scheduleLabelFromDraft(
  draft: ScheduleDraft,
  t: (key: string, params?: Record<string, string | number>) => string,
  locale?: string,
): string {
  if (draft.frequency === 'hourly') {
    return t(`schedule.frequency.${draft.frequency}`);
  }
  if (draft.frequency === 'weekly') {
    return t('schedule.weeklyAt', {
      days: weeklyDaysLabel(draft.daysOfWeek, t),
      time: formatTimeLabel(draft.timeOfDay, locale),
    });
  }
  if (draft.frequency === 'custom') {
    return t('schedule.custom');
  }
  return t('schedule.frequencyAt', {
    frequency: t(`schedule.frequency.${draft.frequency}`),
    time: formatTimeLabel(draft.timeOfDay, locale),
  });
}

export function weeklyDaysLabel(
  days: number[],
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  const dayKeys = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
  return normalizeDaysOfWeek(days)
    .map((day) => t(`schedule.day.${dayKeys[day - 1]}Short`))
    .join(', ');
}

export function formatTimeLabel(value: string, locale?: string): string {
  const [hourText, minuteText] = normalizeTimeOfDay(value).split(':');
  const hour = Number(hourText);
  const minute = Number(minuteText);
  if (locale === 'zh-CN') {
    return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  }
  const period = hour >= 12 ? 'PM' : 'AM';
  const hour12 = hour % 12 || 12;
  return minute === 0 ? `${hour12}:00 ${period}` : `${hour12}:${String(minute).padStart(2, '0')} ${period}`;
}

export function formatDate(value: string | null | undefined, locale?: string): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(locale);
}

export function isAutomationActive(item: Automation): boolean {
  return Boolean(item.activeRunId && ['queued', 'running', 'canceling'].includes(item.activeRunStatus ?? ''));
}

export const ACTIVE_AUTOMATION_POLL_MS = 3000;
