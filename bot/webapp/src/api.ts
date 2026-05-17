import type {
  AdminUser,
  AdminUserRole,
  Booking,
  Client,
  ClientDetail,
  Me,
  PublicBookingResult,
  PublicMasterPage,
  ReturnClient,
  Service,
  Stats,
} from "./types";

// Accept both `VITE_API_BASE` and the more conventional `VITE_API_BASE_URL`
// so a deployment doesn't silently fall back to same-origin (which would 404
// on Vercel since there's no backend there).
const RAW_API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined) ??
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ??
  "";
const API_BASE = RAW_API_BASE.replace(/\/$/, "");

function authHeaders(): Record<string, string> {
  const initData = window.Telegram?.WebApp?.initData ?? "";
  if (!initData) return {};
  return {
    Authorization: `tma ${initData}`,
    "X-Telegram-Init-Data": initData,
  };
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const json = await res.json();
      if (json?.detail) detail = String(json.detail);
    } catch {
      // ignore
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

export const api = {
  getMe: () => request<Me>("GET", "/api/me"),
  updateMe: (payload: Partial<Me>) => request<Me>("PATCH", "/api/me", payload),

  listServices: (includeHidden = false) =>
    request<Service[]>(
      "GET",
      `/api/services${includeHidden ? "?include_hidden=true" : ""}`,
    ),
  createService: (payload: {
    name: string;
    price: number;
    duration_minutes: number;
    is_active?: boolean;
  }) => request<Service>("POST", "/api/services", payload),
  updateService: (id: number, payload: Partial<Service>) =>
    request<Service>("PATCH", `/api/services/${id}`, payload),
  deleteService: (id: number) => request<void>("DELETE", `/api/services/${id}`),

  listClients: (q?: string) =>
    request<Client[]>(
      "GET",
      q ? `/api/clients?q=${encodeURIComponent(q)}` : "/api/clients",
    ),
  createClient: (payload: {
    name: string;
    phone?: string | null;
    tg_username?: string | null;
    notes?: string | null;
  }) => request<Client>("POST", "/api/clients", payload),
  getClient: (id: number) => request<ClientDetail>("GET", `/api/clients/${id}`),
  updateClient: (id: number, payload: Partial<Client>) =>
    request<Client>("PATCH", `/api/clients/${id}`, payload),
  deleteClient: (id: number) => request<void>("DELETE", `/api/clients/${id}`),

  listBookings: (params?: {
    date_from?: string;
    date_to?: string;
    status?: string;
  }) => {
    const qs = new URLSearchParams();
    if (params?.date_from) qs.set("date_from", params.date_from);
    if (params?.date_to) qs.set("date_to", params.date_to);
    if (params?.status) qs.set("status", params.status);
    const q = qs.toString();
    return request<Booking[]>("GET", `/api/bookings${q ? "?" + q : ""}`);
  },
  listBookingsToday: () => request<Booking[]>("GET", "/api/bookings/today"),
  createBooking: (payload: {
    client_id?: number | null;
    new_client_name?: string | null;
    new_client_phone?: string | null;
    new_client_tg_username?: string | null;
    service_id: number;
    starts_at: string;
    notes?: string | null;
    status?: string;
  }) => request<Booking>("POST", "/api/bookings", payload),
  updateBooking: (
    id: number,
    payload: {
      starts_at?: string;
      status?: string;
      notes?: string | null;
      service_id?: number;
    },
  ) => request<Booking>("PATCH", `/api/bookings/${id}`, payload),
  deleteBooking: (id: number) =>
    request<void>("DELETE", `/api/bookings/${id}`),

  getStats: (period: "day" | "week" | "month") =>
    request<Stats>("GET", `/api/stats?period=${period}`),
  getReturnClients: (thresholdDays = 30) =>
    request<ReturnClient[]>(
      "GET",
      `/api/stats/return-clients?threshold_days=${thresholdDays}`,
    ),

  listAdminUsers: (role?: AdminUserRole) =>
    request<AdminUser[]>(
      "GET",
      role ? `/api/admin/users?role=${role}` : "/api/admin/users",
    ),
  addMasterByTgId: (payload: {
    tg_user_id: number;
    display_name?: string | null;
    tg_username?: string | null;
  }) => request<AdminUser>("POST", "/api/admin/users", payload),
  setUserRoles: (
    userId: number,
    payload: { is_master?: boolean; is_admin?: boolean },
  ) => request<AdminUser>("PATCH", `/api/admin/users/${userId}`, payload),
  demoteMaster: (userId: number) =>
    request<AdminUser>("DELETE", `/api/admin/users/${userId}/master`),

  getPublicMaster: (slug: string) =>
    request<PublicMasterPage>("GET", `/api/public/${slug}`),
  getAvailability: (slug: string, serviceId: number, date: string) =>
    request<string[]>(
      "GET",
      `/api/public/${slug}/availability?service_id=${serviceId}&date=${date}`,
    ),
  createPublicBooking: (
    slug: string,
    payload: {
      service_id: number;
      starts_at: string;
      name: string;
      phone?: string | null;
    },
  ) =>
    request<PublicBookingResult>(
      "POST",
      `/api/public/${slug}/bookings`,
      payload,
    ),
};
