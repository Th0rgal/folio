interface Bridge {
  core?: {
    invoke: <T = unknown>(command: string, args?: Record<string, unknown>) => Promise<T>;
  };
  event?: {
    listen: (
      eventName: string,
      handler: (ev: { event: string; payload: unknown }) => void,
    ) => Promise<() => void>;
  };
}

declare global {
  interface Window {
    __FOLIO_DESKTOP__?: Bridge;
    __ORB_DESKTOP__?: Bridge;
    __TAURI__?: Bridge;
  }
}

function getBridge(): Bridge | undefined {
  if (typeof window === "undefined") return undefined;
  return window.__FOLIO_DESKTOP__ ?? window.__ORB_DESKTOP__ ?? window.__TAURI__;
}

export async function rpcInvoke<T = unknown>(
  command: string,
  args: Record<string, unknown> = {},
): Promise<T> {
  const bridge = getBridge();
  if (bridge?.core?.invoke) {
    return bridge.core.invoke<T>(command, args);
  }
  const resp = await fetch("/__folio_rpc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, args }),
  });
  const body = (await resp.json()) as { result?: T; error?: string };
  if (!resp.ok || body.error) {
    throw new Error(body.error || `RPC failed: ${command}`);
  }
  return body.result as T;
}

export async function listenDesktopEvent(
  eventName: string,
  handler: (payload: unknown) => void,
): Promise<() => void> {
  const bridge = getBridge();
  if (bridge?.event?.listen) {
    return bridge.event.listen(eventName, (ev) => handler(ev.payload));
  }
  let stopped = false;
  const timer = window.setInterval(async () => {
    if (stopped) return;
    try {
      const events = await rpcInvoke<Array<{ event: string; payload: unknown }>>("poll_events");
      for (const item of events) {
        if (item.event === eventName) handler(item.payload);
      }
    } catch {
      // Ignore transient reconnects during dev reload.
    }
  }, 600);
  return () => {
    stopped = true;
    window.clearInterval(timer);
  };
}

export function detectPlatform(): "linux" | "macos" | "web" {
  if (typeof navigator === "undefined") return "linux";
  const ua = navigator.userAgent.toLowerCase();
  if (ua.includes("mac os") || ua.includes("macintosh")) return "macos";
  if (ua.includes("linux") || ua.includes("wayland")) return "linux";
  return "web";
}
