import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import type {
  PublicBookingResult,
  PublicMasterPage,
} from "../types";
import {
  formatDateTime,
  formatDuration,
  formatPrice,
  todayISODate,
} from "../utils/format";

export function PublicBookingPage({ slug }: { slug: string }) {
  const [page, setPage] = useState<PublicMasterPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [serviceId, setServiceId] = useState<number | null>(null);
  const [date, setDate] = useState<string>(todayISODate());
  const [slots, setSlots] = useState<string[]>([]);
  const [selectedSlot, setSelectedSlot] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState<PublicBookingResult | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const p = await api.getPublicMaster(slug);
        setPage(p);
        if (p.services.length > 0) {
          setServiceId(p.services[0].id);
        }
      } catch (err) {
        setError(String(err));
      }
    })();
  }, [slug]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!serviceId) {
        setSlots([]);
        return;
      }
      try {
        const list = await api.getAvailability(slug, serviceId, date);
        if (cancelled) return;
        setSlots(list);
        setSelectedSlot(null);
      } catch (err) {
        if (!cancelled) setError(String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [slug, serviceId, date]);

  const selectedService = useMemo(() => {
    if (!page || serviceId === null) return null;
    return page.services.find((s) => s.id === serviceId) ?? null;
  }, [page, serviceId]);

  const submit = async () => {
    if (!serviceId || !selectedSlot || !name.trim()) {
      setError("Заполните услугу, время и имя");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.createPublicBooking(slug, {
        service_id: serviceId,
        starts_at: selectedSlot,
        name: name.trim(),
        phone: phone.trim() || null,
      });
      setDone(result);
    } catch (err) {
      setError(String(err));
    } finally {
      setSubmitting(false);
    }
  };

  if (error && !page) {
    return (
      <div className="app-shell">
        <main className="app-shell__main">
          <EmptyState icon="❌" title="Мастер не найден" description={error} />
        </main>
      </div>
    );
  }

  if (!page) {
    return (
      <div className="app-shell">
        <main className="app-shell__main">
          <PageHeader title="Загрузка..." />
        </main>
      </div>
    );
  }

  if (done) {
    return (
      <div className="app-shell">
        <main className="app-shell__main">
          <EmptyState
            icon="🎉"
            title="Записали!"
            description={`${done.master_display_name}, ${formatDateTime(done.starts_at)}. Мы пришлём напоминание перед визитом.`}
          />
        </main>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <main className="app-shell__main">
        <PageHeader
          title={`Запись к ${page.master.display_name}`}
          subtitle="Выберите услугу и время"
        />

        {error ? <div className="error-banner">{error}</div> : null}

        {page.services.length === 0 ? (
          <EmptyState
            icon="🛠️"
            title="Услуги пока не добавлены"
            description="Загляните чуть позже."
          />
        ) : null}

        {page.services.length > 0 ? (
          <div className="field">
            <label className="field__label">Услуга</label>
            <div className="list">
              {page.services.map((s) => (
                <button
                  type="button"
                  className="list-item"
                  key={s.id}
                  onClick={() => setServiceId(s.id)}
                  style={{
                    border:
                      "1px solid " +
                      (s.id === serviceId ? "var(--button)" : "var(--border)"),
                    background:
                      s.id === serviceId
                        ? "rgba(36, 129, 204, 0.06)"
                        : "var(--section)",
                    textAlign: "left",
                  }}
                >
                  <div className="list-item__row">
                    <div>
                      <div className="list-item__title">{s.name}</div>
                      <div className="list-item__meta">
                        {formatDuration(s.duration_minutes)} · {formatPrice(s.price)}
                      </div>
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {selectedService ? (
          <>
            <div className="field">
              <label className="field__label">Дата</label>
              <input
                className="field__input"
                type="date"
                value={date}
                min={todayISODate()}
                onChange={(e) => setDate(e.target.value)}
              />
            </div>

            <div className="field">
              <label className="field__label">Время</label>
              {slots.length === 0 ? (
                <div className="empty-state__description" style={{ padding: "8px 0" }}>
                  На эту дату свободного времени нет — выберите другую.
                </div>
              ) : (
                <div className="slot-grid">
                  {slots.map((slot) => (
                    <button
                      key={slot}
                      type="button"
                      className={
                        "slot" + (selectedSlot === slot ? " slot--active" : "")
                      }
                      onClick={() => setSelectedSlot(slot)}
                    >
                      {slot.slice(11, 16)}
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div className="field">
              <label className="field__label">Ваше имя</label>
              <input
                className="field__input"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label">Телефон (необязательно)</label>
              <input
                className="field__input"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
              />
            </div>

            <button
              className="btn btn--block"
              disabled={submitting || !selectedSlot || !name.trim()}
              onClick={submit}
            >
              {submitting ? "Отправляем..." : "Записаться"}
            </button>
          </>
        ) : null}
      </main>
    </div>
  );
}
