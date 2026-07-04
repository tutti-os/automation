type TuttiActivityBridge = {
  activity?: {
    reportActive?: () => Promise<void> | void;
  };
};

function getTuttiActivityBridge() {
  if (typeof window === 'undefined') return null;
  return (window as Window & { tuttiExternal?: TuttiActivityBridge }).tuttiExternal?.activity ?? null;
}

export function reportUserActive(): void {
  const reportActive = getTuttiActivityBridge()?.reportActive;
  if (typeof reportActive !== 'function') return;
  try {
    void Promise.resolve(reportActive()).catch(() => undefined);
  } catch {
    // Activity reporting must not affect the app workflow.
  }
}
