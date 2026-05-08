import { useEffect, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import type { Booking, BookingStatus, Client, Service } from "../types";
import {
  formatDateTime,
  formatTime,
  localDateToUTCString,
  statusLabel,
  todayISODate,
} from "../utils/format";

const STATUSES: BookingStatus[] = [
  "new",
  "confirmed",
  "came",
  "cancelled",
  "no_show",
];

export function BookingsPage() {
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [services, setServices] = useState<Service[]>([]);
  const [clients, setClients] = useState<Client[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const reload = async () => {
    try {
      const [b, s, c] = await Promise.all([
        api.listBookings(),
        api.listServices(true),
        api.listClients(),
      ]);
      setBookings(b);
      setServices(s);
      setClients(c);
    } catch (err) {
      setError(String(err));
    }
  };

  useEffect(() => {
    void reload();
  }, []);

  const updateStatus = async (id: number, status: BookingStatus) => {
    setError(null);
    try {
      await api.updateBooking(id, { status });
      await reload();
    } catch (err) {
      setError(String(err));
    }
  };

  return (
    <div>
      <PageHeader
        title="Записи"
        action={
          <button className="btn btn--small" onClick={() => setCreating(true)}>
            + Новая
          </button>
        }
      />

      {error ? <div className="error-banner">{error}</div> : null}

      {bookings.length === 0 ? (
        <EmptyState
          icon="📋"
          title="Пока нет записей"
          description="Создайте первую запись или поделитесь ссылкой с клиентами."
        />
      ) : null}

      <div className="list">
        {bookings.map((b) => (
          <div className="list-item" key={b.id}>
            <div className="list-item__row">
              <div>
                <div className="list-item__title">
                  {formatDateTime(b.starts_at)} · {b.service_name}
                </div>
                <div className="list-item__meta">
                  {b.client_name}
                  {b.client_phone ? ` · ${b.client_phone}` : ""}
                </div>
              </div>
              <span className={`pill pill--${b.status}`}>
                {statusLabel(b.status)}
              </span>
            </div>
            <div className="list-item__actions">
              {STATUSES.filter((s) => s !== b.status).map((s) => (
                <button
                  key={s}
                  className="btn btn--ghost btn--small"
                  onClick={() => updateStatus(b.id, s)}
                >
                  {statusLabel(s)}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>

      <CreateBookingModal
        open={creating}
        onClose={() => setCreating(false)}
        services={services}
        clients={clients}
        onCreated={async () => {
          setCreating(false);
          await reload();
        }}
      />
    </div>
  );
}

function CreateBookingModal({
  open,
  onClose,
  services,
  clients,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  services: Service[];
  clients: Client[];
  onCreated: () => Promise<void> | void;
}) {
  const activeServices = services.filter((s) => s.is_active);
  const [serviceId, setServiceId] = useState<number | "">("");
  const [clientMode, setClientMode] = useState<"existing" | "new">("existing");
  const [clientId, setClientId] = useState<number | "">("");
  const [newName, setNewName] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [date, setDate] = useState(todayISODate());
  const [time, setTime] = useState("12:00");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) {
      setServiceId("");
      setClientId("");
      setNewName("");
      setNewPhone("");
      setError(null);
    }
  }, [open]);

  const submit = async () => {
    if (!serviceId) {
      setError("Выберите услугу");
      return;
    }
    if (clientMode === "existing" && !clientId) {
      setError("Выберите клиента");
      return;
    }
    if (clientMode === "new" && !newName.trim()) {
      setError("Укажите имя клиента");
      return;
    }
    setError(null);
    setSaving(true);
    try {
      const [hours, minutes] = time.split(":").map(Number);
      const [y, m, d] = date.split("-").map(Number);
      const localDate = new Date(y, m - 1, d, hours, minutes);
      await api.createBooking({
        service_id: Number(serviceId),
        starts_at: localDateToUTCString(localDate),
        ...(clientMode === "existing"
          ? { client_id: Number(clientId) }
          : {
              new_client_name: newName.trim(),
              new_client_phone: newPhone.trim() || null,
            }),
        status: "confirmed",
      });
      await onCreated();
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} title="Новая запись" onClose={onClose}>
      {error ? <div className="error-banner">{error}</div> : null}

      <div className="field">
        <label className="field__label">Услуга</label>
        <select
          className="field__select"
          value={serviceId}
          onChange={(e) => setServiceId(e.target.value ? Number(e.target.value) : "")}
        >
          <option value="">— выберите —</option>
          {activeServices.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name} · {s.price} ₽ · {s.duration_minutes} мин
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label className="field__label">Клиент</label>
        <div className="segment">
          <button
            type="button"
            className={
              "segment__btn" + (clientMode === "existing" ? " segment__btn--active" : "")
            }
            onClick={() => setClientMode("existing")}
          >
            Из списка
          </button>
          <button
            type="button"
            className={
              "segment__btn" + (clientMode === "new" ? " segment__btn--active" : "")
            }
            onClick={() => setClientMode("new")}
          >
            Новый
          </button>
        </div>
        {clientMode === "existing" ? (
          <select
            className="field__select"
            value={clientId}
            onChange={(e) => setClientId(e.target.value ? Number(e.target.value) : "")}
          >
            <option value="">— выберите —</option>
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
                {c.phone ? ` · ${c.phone}` : ""}
              </option>
            ))}
          </select>
        ) : (
          <>
            <input
              className="field__input"
              placeholder="Имя"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
            <input
              className="field__input"
              placeholder="Телефон"
              value={newPhone}
              onChange={(e) => setNewPhone(e.target.value)}
            />
          </>
        )}
      </div>

      <div className="field">
        <label className="field__label">Дата и время</label>
        <div className="field__row">
          <input
            className="field__input"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
          <input
            className="field__input"
            type="time"
            value={time}
            onChange={(e) => setTime(e.target.value)}
          />
        </div>
      </div>

      <button className="btn btn--block" disabled={saving} onClick={submit}>
        {saving ? "Сохраняем..." : "Создать запись"}
      </button>
      <div style={{ height: 8 }} />
      <div style={{ fontSize: 12, color: "var(--hint)" }}>
        В {formatTime(`${date}T${time}:00`)} — статус «Подтверждена»
      </div>
    </Modal>
  );
}
