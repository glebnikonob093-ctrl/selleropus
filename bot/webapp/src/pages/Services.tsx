import { useEffect, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import type { Service } from "../types";
import { formatDuration, formatPrice } from "../utils/format";

export function ServicesPage() {
  const [services, setServices] = useState<Service[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Service | null>(null);

  const reload = async () => {
    try {
      const list = await api.listServices(true);
      setServices(list);
    } catch (err) {
      setError(String(err));
    }
  };

  useEffect(() => {
    void reload();
  }, []);

  return (
    <div>
      <PageHeader
        title="Услуги"
        action={
          <button className="btn btn--small" onClick={() => setCreating(true)}>
            + Услуга
          </button>
        }
      />

      {error ? <div className="error-banner">{error}</div> : null}

      {services.length === 0 ? (
        <EmptyState
          icon="💼"
          title="Добавьте свои услуги"
          description="Услуги нужны, чтобы клиенты могли выбрать их при записи."
          action={
            <button className="btn" onClick={() => setCreating(true)}>
              Создать услугу
            </button>
          }
        />
      ) : null}

      <div className="list">
        {services.map((s) => (
          <button
            type="button"
            className="list-item"
            key={s.id}
            onClick={() => setEditing(s)}
            style={{ textAlign: "left", opacity: s.is_active ? 1 : 0.55 }}
          >
            <div className="list-item__row">
              <div>
                <div className="list-item__title">{s.name}</div>
                <div className="list-item__meta">
                  {formatPrice(s.price)} · {formatDuration(s.duration_minutes)}
                  {s.is_active ? "" : " · скрыта"}
                </div>
              </div>
            </div>
          </button>
        ))}
      </div>

      <ServiceFormModal
        open={creating}
        title="Новая услуга"
        onClose={() => setCreating(false)}
        onSubmit={async (payload) => {
          await api.createService(payload);
          setCreating(false);
          await reload();
        }}
      />

      <ServiceFormModal
        open={!!editing}
        title="Редактирование услуги"
        service={editing ?? undefined}
        onClose={() => setEditing(null)}
        onSubmit={async (payload) => {
          if (!editing) return;
          await api.updateService(editing.id, payload);
          setEditing(null);
          await reload();
        }}
        onDelete={
          editing
            ? async () => {
                if (!editing) return;
                const ok = window.confirm(
                  `Удалить услугу "${editing.name}"? Это действие нельзя отменить.`,
                );
                if (!ok) return;
                await api.deleteService(editing.id);
                setEditing(null);
                await reload();
              }
            : undefined
        }
      />
    </div>
  );
}

function ServiceFormModal({
  open,
  title,
  service,
  onClose,
  onSubmit,
  onDelete,
}: {
  open: boolean;
  title: string;
  service?: Service;
  onClose: () => void;
  onSubmit: (payload: {
    name: string;
    price: number;
    duration_minutes: number;
    is_active?: boolean;
  }) => Promise<void>;
  onDelete?: () => Promise<void>;
}) {
  const [name, setName] = useState(service?.name ?? "");
  const [price, setPrice] = useState<string>(String(service?.price ?? 1500));
  const [duration, setDuration] = useState<string>(
    String(service?.duration_minutes ?? 60),
  );
  const [isActive, setIsActive] = useState<boolean>(service?.is_active ?? true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setName(service?.name ?? "");
    setPrice(String(service?.price ?? 1500));
    setDuration(String(service?.duration_minutes ?? 60));
    setIsActive(service?.is_active ?? true);
    setError(null);
  }, [service, open]);

  const submit = async () => {
    if (!name.trim()) {
      setError("Введите название");
      return;
    }
    const priceNum = Number(price);
    const durNum = Number(duration);
    if (!Number.isFinite(priceNum) || priceNum < 0) {
      setError("Цена должна быть числом ≥ 0");
      return;
    }
    if (!Number.isFinite(durNum) || durNum < 5) {
      setError("Длительность минимум 5 минут");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSubmit({
        name: name.trim(),
        price: Math.round(priceNum),
        duration_minutes: Math.round(durNum),
        is_active: isActive,
      });
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} title={title} onClose={onClose}>
      {error ? <div className="error-banner">{error}</div> : null}
      <div className="field">
        <label className="field__label">Название</label>
        <input
          className="field__input"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label">Цена, ₽</label>
        <input
          className="field__input"
          inputMode="numeric"
          value={price}
          onChange={(e) => setPrice(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label">Длительность, минут</label>
        <input
          className="field__input"
          inputMode="numeric"
          value={duration}
          onChange={(e) => setDuration(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label">
          <input
            type="checkbox"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
            style={{ marginRight: 8 }}
          />
          Активна (видна клиентам)
        </label>
      </div>
      <button className="btn btn--block" disabled={saving} onClick={submit}>
        {saving ? "Сохраняем..." : "Сохранить"}
      </button>
      {onDelete ? (
        <>
          <div style={{ height: 8 }} />
          <button
            className="btn btn--ghost btn--block"
            style={{ color: "var(--danger)" }}
            onClick={onDelete}
          >
            Скрыть услугу
          </button>
        </>
      ) : null}
    </Modal>
  );
}
