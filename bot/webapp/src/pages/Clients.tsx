import { useEffect, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import type { Client, ClientDetail } from "../types";
import { formatDateTime, formatPrice, statusLabel } from "../utils/format";

export function ClientsPage() {
  const [clients, setClients] = useState<Client[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Client | null>(null);
  const [detail, setDetail] = useState<ClientDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const reload = async (q?: string) => {
    try {
      const list = await api.listClients(q);
      setClients(list);
    } catch (err) {
      setError(String(err));
    }
  };

  useEffect(() => {
    void reload();
  }, []);

  const openDetail = async (c: Client) => {
    setDetailLoading(true);
    try {
      const d = await api.getClient(c.id);
      setDetail(d);
    } catch (err) {
      setError(String(err));
    } finally {
      setDetailLoading(false);
    }
  };

  const closeDetail = () => {
    setDetail(null);
  };

  return (
    <div>
      <PageHeader
        title="Клиенты"
        subtitle={clients.length ? `Всего: ${clients.length}` : undefined}
        action={
          <button className="btn btn--small" onClick={() => setCreating(true)}>
            + Клиент
          </button>
        }
      />

      {error ? <div className="error-banner">{error}</div> : null}

      <div className="field">
        <input
          className="field__input"
          placeholder="Поиск по имени или телефону"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            void reload(e.target.value);
          }}
        />
      </div>

      {!detailLoading && clients.length === 0 ? (
        <EmptyState
          icon="👥"
          title="Список пуст"
          description="Добавьте первого клиента или дайте ссылку — клиенты появятся сами."
        />
      ) : null}

      <div className="list">
        {clients.map((c) => (
          <div
            className="list-item"
            key={c.id}
            onClick={() => openDetail(c)}
            style={{
              textAlign: "left",
              cursor: "pointer",
              background: c.is_blocked ? "rgba(255,0,0,0.05)" : undefined,
            }}
          >
            <div className="list-item__row">
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="list-item__title">
                  {c.name}
                  {c.is_blocked ? " 🚫" : ""}
                  {c.notes ? " 📝" : ""}
                </div>
                <div className="list-item__meta">
                  {c.phone ? c.phone : "—"}
                  {c.tg_username ? (
                    <>
                      {" · "}
                      <a
                        href={`https://t.me/${c.tg_username}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        style={{ color: "var(--link, #2481cc)" }}
                      >
                        @{c.tg_username}
                      </a>
                    </>
                  ) : null}
                </div>
              </div>
              <div className="list-item__meta" style={{ textAlign: "right", flexShrink: 0 }}>
                {c.last_visit_at ? formatDateTime(c.last_visit_at) : "ни разу"}
              </div>
            </div>
          </div>
        ))}
      </div>

      {detailLoading ? (
        <div style={{ textAlign: "center", padding: 24, color: "var(--hint)" }}>
          Загрузка...
        </div>
      ) : null}

      <ClientDetailModal
        detail={detail}
        onClose={closeDetail}
        onEdit={(c) => {
          closeDetail();
          setEditing(c);
        }}
        onBlock={async (tgUserId) => {
          await api.blockClient(tgUserId);
          await reload(query);
          if (detail) {
            const refreshed = await api.getClient(detail.id);
            setDetail(refreshed);
          }
        }}
        onUnblock={async (tgUserId) => {
          await api.unblockClient(tgUserId);
          await reload(query);
          if (detail) {
            const refreshed = await api.getClient(detail.id);
            setDetail(refreshed);
          }
        }}
        onDelete={async (id) => {
          const ok = window.confirm(
            "Удалить клиента и все его записи? Это действие нельзя отменить.",
          );
          if (!ok) return;
          await api.deleteClient(id);
          closeDetail();
          await reload(query);
        }}
      />

      <ClientFormModal
        open={creating}
        title="Новый клиент"
        onClose={() => setCreating(false)}
        onSubmit={async (payload) => {
          await api.createClient(payload);
          setCreating(false);
          await reload(query);
        }}
      />

      <ClientFormModal
        open={!!editing}
        title="Редактирование клиента"
        client={editing ?? undefined}
        onClose={() => setEditing(null)}
        onSubmit={async (payload) => {
          if (!editing) return;
          await api.updateClient(editing.id, payload);
          setEditing(null);
          await reload(query);
        }}
      />
    </div>
  );
}

function ClientDetailModal({
  detail,
  onClose,
  onEdit,
  onBlock,
  onUnblock,
  onDelete,
}: {
  detail: ClientDetail | null;
  onClose: () => void;
  onEdit: (c: Client) => void;
  onBlock: (tgUserId: number) => Promise<void>;
  onUnblock: (tgUserId: number) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
}) {
  if (!detail) return null;

  const came = detail.bookings.filter((b) => b.status === "came");
  const cancelled = detail.bookings.filter(
    (b) => b.status === "cancelled" || b.status === "no_show",
  );
  const revenue = came.reduce((sum, b) => sum + b.price_snapshot, 0);

  return (
    <Modal open title={detail.name} onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {/* Contact info */}
        <div className="card" style={{ margin: 0 }}>
          {detail.phone ? (
            <div className="list-item__meta">📱 {detail.phone}</div>
          ) : null}
          {detail.tg_username ? (
            <div className="list-item__meta">
              💬{" "}
              <a
                href={`https://t.me/${detail.tg_username}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                @{detail.tg_username}
              </a>
            </div>
          ) : null}
          {detail.tg_user_id ? (
            <div className="list-item__meta">
              🆔{" "}
              <span
                onClick={() =>
                  navigator.clipboard.writeText(String(detail.tg_user_id))
                }
                style={{
                  cursor: "pointer",
                  textDecoration: "underline dotted",
                  fontFamily: "monospace",
                }}
                title="Нажмите чтобы скопировать"
              >
                {detail.tg_user_id}
              </span>
            </div>
          ) : null}
          {detail.is_blocked ? (
            <div
              style={{
                color: "var(--danger)",
                fontWeight: 600,
                fontSize: 13,
                marginTop: 4,
              }}
            >
              🚫 Заблокирован
            </div>
          ) : null}
        </div>

        {/* Notes */}
        {detail.notes ? (
          <div className="card" style={{ margin: 0 }}>
            <div
              style={{
                fontWeight: 600,
                fontSize: 13,
                color: "var(--hint)",
                marginBottom: 4,
              }}
            >
              📝 Заметки
            </div>
            <div style={{ fontSize: 14, whiteSpace: "pre-wrap" }}>
              {detail.notes}
            </div>
          </div>
        ) : null}

        {/* Stats */}
        <div className="kpi-grid" style={{ margin: 0 }}>
          <div className="kpi">
            <div className="kpi__label">Всего записей</div>
            <div className="kpi__value">{detail.bookings.length}</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Пришёл</div>
            <div className="kpi__value">{came.length}</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Отмены</div>
            <div className="kpi__value">{cancelled.length}</div>
          </div>
          <div className="kpi">
            <div className="kpi__label">Доход</div>
            <div className="kpi__value">{formatPrice(revenue)}</div>
          </div>
        </div>

        {/* Booking history */}
        {detail.bookings.length > 0 ? (
          <div>
            <div
              style={{
                fontWeight: 600,
                fontSize: 14,
                marginBottom: 8,
              }}
            >
              📋 История записей
            </div>
            <div className="list" style={{ gap: 6 }}>
              {detail.bookings.slice(0, 10).map((b) => (
                <div
                  className="list-item"
                  key={b.id}
                  style={{ padding: "8px 12px" }}
                >
                  <div className="list-item__row">
                    <div style={{ minWidth: 0 }}>
                      <div
                        className="list-item__title"
                        style={{ fontSize: 14 }}
                      >
                        {b.service_name}
                      </div>
                      <div className="list-item__meta">
                        {formatDateTime(b.starts_at)}
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
                      <span className={`pill pill--${b.status}`}>
                        {statusLabel(b.status)}
                      </span>
                      {b.price_snapshot > 0 ? (
                        <span
                          className="list-item__meta"
                          style={{ whiteSpace: "nowrap" }}
                        >
                          {formatPrice(b.price_snapshot)}
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
              ))}
              {detail.bookings.length > 10 ? (
                <div
                  className="list-item__meta"
                  style={{ textAlign: "center", padding: 4 }}
                >
                  ...и ещё {detail.bookings.length - 10}
                </div>
              ) : null}
            </div>
          </div>
        ) : (
          <div
            style={{
              textAlign: "center",
              color: "var(--hint)",
              padding: 12,
              fontSize: 14,
            }}
          >
            Записей пока нет
          </div>
        )}

        {/* Actions */}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            className="btn btn--ghost btn--small"
            onClick={() => onEdit(detail)}
            style={{ flex: 1 }}
          >
            ✏️ Редактировать
          </button>
          {detail.tg_user_id ? (
            detail.is_blocked ? (
              <button
                className="btn btn--small"
                onClick={() => onUnblock(detail.tg_user_id!)}
                style={{ flex: 1 }}
              >
                ✅ Разблокировать
              </button>
            ) : (
              <button
                className="btn btn--ghost btn--small"
                onClick={() => onBlock(detail.tg_user_id!)}
                style={{ flex: 1, color: "var(--danger)" }}
              >
                🚫 Заблокировать
              </button>
            )
          ) : null}
        </div>
        <button
          className="btn btn--ghost btn--block btn--small"
          onClick={() => onDelete(detail.id)}
          style={{ color: "var(--danger)" }}
        >
          Удалить клиента
        </button>
      </div>
    </Modal>
  );
}

function ClientFormModal({
  open,
  title,
  client,
  onClose,
  onSubmit,
}: {
  open: boolean;
  title: string;
  client?: Client;
  onClose: () => void;
  onSubmit: (payload: {
    name: string;
    phone?: string | null;
    tg_username?: string | null;
    notes?: string | null;
  }) => Promise<void>;
}) {
  const [name, setName] = useState(client?.name ?? "");
  const [phone, setPhone] = useState(client?.phone ?? "");
  const [tgUsername, setTgUsername] = useState(client?.tg_username ?? "");
  const [notes, setNotes] = useState(client?.notes ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setName(client?.name ?? "");
    setPhone(client?.phone ?? "");
    setTgUsername(client?.tg_username ?? "");
    setNotes(client?.notes ?? "");
    setError(null);
  }, [client, open]);

  const submit = async () => {
    if (!name.trim()) {
      setError("Имя обязательно");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSubmit({
        name: name.trim(),
        phone: phone.trim() || null,
        tg_username: tgUsername.trim() || null,
        notes: notes.trim() || null,
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
        <label className="field__label">Имя</label>
        <input
          className="field__input"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label">Телефон</label>
        <input
          className="field__input"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label">Telegram username</label>
        <input
          className="field__input"
          value={tgUsername}
          onChange={(e) => setTgUsername(e.target.value)}
          placeholder="без @"
        />
      </div>
      <div className="field">
        <label className="field__label">Заметки</label>
        <textarea
          className="field__textarea"
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </div>
      <button className="btn btn--block" disabled={saving} onClick={submit}>
        {saving ? "Сохраняем..." : "Сохранить"}
      </button>
    </Modal>
  );
}
