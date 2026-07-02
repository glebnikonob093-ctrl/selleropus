export type BookingStatus =
  | "new"
  | "confirmed"
  | "came"
  | "cancelled"
  | "no_show";

export interface Me {
  id: number;
  tg_user_id: number;
  tg_username: string | null;
  display_name: string;
  slug: string;
  is_master: boolean;
  timezone: string;
  language: string;
  work_start_minutes: number;
  work_end_minutes: number;
  slot_step_minutes: number;
  public_link_path: string;
}

export interface Service {
  id: number;
  name: string;
  price: number;
  duration_minutes: number;
  is_active: boolean;
}

export interface Client {
  id: number;
  name: string;
  phone: string | null;
  tg_username: string | null;
  tg_user_id: number | null;
  notes: string | null;
  last_visit_at: string | null;
  created_at: string;
  is_blocked: boolean;
}

export interface ClientDetail extends Client {
  bookings: Array<{
    id: number;
    starts_at: string;
    ends_at: string;
    status: BookingStatus;
    service_id: number;
    service_name: string;
    price_snapshot: number;
  }>;
}

export interface Booking {
  id: number;
  starts_at: string;
  ends_at: string;
  status: BookingStatus;
  source: string;
  notes: string | null;
  price_snapshot: number;
  client_id: number;
  client_name: string;
  client_phone: string | null;
  client_tg_username: string | null;
  service_id: number;
  service_name: string;
  service_duration_minutes: number;
}

export interface Stats {
  period: string;
  starts_at: string;
  ends_at: string;
  revenue: number;
  bookings_total: number;
  bookings_came: number;
  top_services: Array<{
    service_id: number;
    service_name: string;
    bookings: number;
    revenue: number;
  }>;
}

export interface ReturnClient {
  client_id: number;
  name: string;
  last_visit_at: string | null;
  days_since: number | null;
}

export interface PublicMasterPage {
  master: { slug: string; display_name: string };
  services: Array<{ id: number; name: string; price: number; duration_minutes: number }>;
}

export interface PublicBookingResult {
  booking_id: number;
  starts_at: string;
  ends_at: string;
  status: BookingStatus;
  master_display_name: string;
}

declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData: string;
        ready: () => void;
        expand: () => void;
        close: () => void;
        themeParams: Record<string, string>;
        colorScheme: "light" | "dark";
        HapticFeedback?: {
          impactOccurred: (style: "light" | "medium" | "heavy") => void;
          notificationOccurred: (type: "error" | "success" | "warning") => void;
        };
        showAlert?: (message: string) => void;
        showConfirm?: (message: string, cb: (ok: boolean) => void) => void;
      };
    };
  }
}
