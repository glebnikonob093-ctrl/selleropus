export function formatPrice(rubles: number): string {
  return `${rubles.toLocaleString("ru-RU")} ₽`;
}

export function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes} мин`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (m === 0) return `${h} ч`;
  return `${h} ч ${m} мин`;
}

export function parseISOAsLocal(iso: string): Date {
  // Backend returns naive UTC. We treat it as the master's local time
  // for display purposes in the MVP.
  const trimmed = iso.endsWith("Z") ? iso.slice(0, -1) : iso;
  const [datePart, timePart = "00:00:00"] = trimmed.split("T");
  const [y, mo, d] = datePart.split("-").map(Number);
  const [h, mi, s = "0"] = timePart.split(":");
  return new Date(y, (mo ?? 1) - 1, d ?? 1, Number(h), Number(mi), Number(s));
}

export function formatDateTime(iso: string): string {
  const dt = parseISOAsLocal(iso);
  const date = dt.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
  const time = dt.toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${date}, ${time}`;
}

export function formatTime(iso: string): string {
  return parseISOAsLocal(iso).toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDateShort(iso: string): string {
  return parseISOAsLocal(iso).toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "long",
  });
}

export function statusLabel(status: string): string {
  switch (status) {
    case "new":
      return "Новая";
    case "confirmed":
      return "Подтверждена";
    case "came":
      return "Пришёл";
    case "cancelled":
      return "Отмена";
    case "no_show":
      return "Не пришёл";
    default:
      return status;
  }
}

export function todayISODate(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

export function localDateToUTCString(d: Date): string {
  // Convert a Date object (built from local Y/M/D/h/m) into the naive
  // ISO string the backend expects (no timezone suffix).
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const h = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${y}-${m}-${dd}T${h}:${mi}:00`;
}
