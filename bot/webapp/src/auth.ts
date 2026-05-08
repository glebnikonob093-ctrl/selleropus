// Helpers around Telegram WebApp init data on the client side.
export function getInitData(): string {
  return window.Telegram?.WebApp?.initData ?? "";
}

export function isInsideTelegram(): boolean {
  return Boolean(window.Telegram?.WebApp?.initData);
}
