import { reportUserActive } from './tuttiActivity';

type AutomationEventPayload = {
  action?: string;
  type?: string;
};

export function handleAutomationEventMessage(
  message: MessageEvent<string>,
  refresh: () => void,
): void {
  try {
    const payload = JSON.parse(message.data) as AutomationEventPayload;
    if (payload.type === 'automation_changed' && payload.action === 'created') {
      reportUserActive();
    }
  } catch {
    // Event refreshes should keep working even if a future payload is not JSON.
  }
  refresh();
}
