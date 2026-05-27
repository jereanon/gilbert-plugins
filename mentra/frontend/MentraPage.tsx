// MentraPage — admin SPA for the Mentra smart-glasses plugin.
//
// Two sections, both gated behind the admin role server-side:
//   1. User mappings — CRUD on the email → Gilbert user_id table
//      ``MentraService`` consults before opening a session.
//   2. Active sessions — read-only, auto-refreshes every 10s so the
//      operator can watch glasses connecting in real time.
//
// Style follows the messaging plugin's MessagingPage (PageHeader,
// shadcn-ui primitives, ``cn`` for class composition). Deliberately
// minimal — the plugin's main UX lives on the glasses; this page is
// for the operator.

import { useCallback, useEffect, useState } from "react";
import { PlusIcon, RefreshCwIcon, Trash2Icon, XIcon } from "lucide-react";

import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { LoadingSpinner } from "@/components/ui/LoadingSpinner";
import { cn } from "@/lib/utils";

import { useMentraApi } from "./api";
import type {
  MentraSessionSummary,
  MentraUserMapping,
} from "./types";

/** Poll cadence for the live-session table. The session set turns
 *  over slowly (one row per pair of glasses currently on a user's
 *  face) so 10s is generous without being annoying for the operator. */
const SESSIONS_POLL_MS = 10_000;

function timeAgo(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function MentraPage() {
  const {
    mappings,
    mappingsLoading,
    mappingsError,
    reloadMappings,
    createMapping,
    updateMapping,
    deleteMapping,
    sessions,
    sessionsLoading,
    sessionsError,
    reloadSessions,
  } = useMentraApi();

  // Auto-poll active sessions. Setup runs once; the hook captures
  // the latest ``reloadSessions`` via the dep array.
  useEffect(() => {
    const handle = window.setInterval(() => {
      void reloadSessions();
    }, SESSIONS_POLL_MS);
    return () => window.clearInterval(handle);
  }, [reloadSessions]);

  return (
    <div className="flex h-[100svh] flex-col">
      <PageHeader
        eyebrow="Mentra"
        title="Smart glasses"
        description="Manage Mentra user mappings and watch live glasses sessions."
      />

      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-10">
        <MappingsSection
          mappings={mappings}
          loading={mappingsLoading}
          error={mappingsError}
          onReload={reloadMappings}
          onCreate={(input) => createMapping(input)}
          onUpdate={(input) => updateMapping(input)}
          onDelete={(id) => deleteMapping(id)}
        />

        <SessionsSection
          sessions={sessions}
          loading={sessionsLoading}
          error={sessionsError}
          onReload={reloadSessions}
        />
      </div>
    </div>
  );
}

// ── User mappings section ───────────────────────────────────────────

interface MappingsSectionProps {
  mappings: MentraUserMapping[];
  loading: boolean;
  error: string | null;
  onReload: () => Promise<void>;
  onCreate: (input: {
    mentra_user_id: string;
    gilbert_user_id: string;
    display_name?: string;
    roles?: string[];
  }) => Promise<MentraUserMapping>;
  onUpdate: (input: {
    mapping_id: string;
    mentra_user_id?: string;
    gilbert_user_id?: string;
    display_name?: string;
    roles?: string[];
  }) => Promise<MentraUserMapping>;
  onDelete: (id: string) => Promise<void>;
}

function MappingsSection({
  mappings,
  loading,
  error,
  onReload,
  onCreate,
  onUpdate,
  onDelete,
}: MappingsSectionProps) {
  const [addOpen, setAddOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [editId, setEditId] = useState<string | null>(null);

  return (
    <section>
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <h2 className="text-base font-semibold">User mappings</h2>
          <p className="mt-0.5 text-xs text-muted-foreground max-w-prose">
            Mentra dispatches glasses sessions to Gilbert keyed by the
            user&apos;s Mentra-side email. Each row maps one Mentra
            account to a Gilbert user — without a row the session
            request is refused.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => void onReload()}
            disabled={loading}
          >
            <RefreshCwIcon
              className={cn("size-3.5 mr-1.5", loading && "animate-spin")}
            />
            Refresh
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={() => setAddOpen((v) => !v)}
          >
            {addOpen ? (
              <>
                <XIcon className="size-3.5 mr-1.5" />
                Cancel
              </>
            ) : (
              <>
                <PlusIcon className="size-3.5 mr-1.5" />
                Add mapping
              </>
            )}
          </Button>
        </div>
      </div>

      {addOpen && (
        <NewMappingForm
          onCancel={() => setAddOpen(false)}
          onSubmit={async (input) => {
            await onCreate(input);
            setAddOpen(false);
          }}
        />
      )}

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive mb-3">
          {error}
        </div>
      )}

      <div className="rounded-md border border-border overflow-hidden">
        {loading && mappings.length === 0 ? (
          <div className="flex items-center justify-center p-8 text-muted-foreground">
            <LoadingSpinner />
          </div>
        ) : mappings.length === 0 ? (
          <div className="p-6 text-sm text-muted-foreground">
            No mappings yet. Click <strong>Add mapping</strong> to
            create one — every Mentra account needs a row here before
            its glasses can talk to Gilbert.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
              <tr>
                <th className="text-left font-medium px-3 py-2">
                  Mentra user
                </th>
                <th className="text-left font-medium px-3 py-2">
                  Gilbert user
                </th>
                <th className="text-left font-medium px-3 py-2">
                  Display name
                </th>
                <th className="text-left font-medium px-3 py-2">
                  Roles
                </th>
                <th className="text-left font-medium px-3 py-2 w-32">
                  Created
                </th>
                <th className="text-right font-medium px-3 py-2 w-40">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {mappings.map((m) =>
                m.id === editId ? (
                  <EditMappingRow
                    key={m.id}
                    mapping={m}
                    onCancel={() => setEditId(null)}
                    onSubmit={async (patch) => {
                      await onUpdate({ mapping_id: m.id, ...patch });
                      setEditId(null);
                    }}
                  />
                ) : (
                  <tr key={m.id} className="hover:bg-accent/30">
                    <td className="px-3 py-2 font-mono text-xs">
                      {m.mentra_user_id}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {m.gilbert_user_id}
                    </td>
                    <td className="px-3 py-2">{m.display_name}</td>
                    <td className="px-3 py-2">
                      <div className="flex gap-1 flex-wrap">
                        {m.roles.map((r) => (
                          <Badge
                            key={r}
                            variant="outline"
                            className="text-[10px] py-0"
                          >
                            {r}
                          </Badge>
                        ))}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {timeAgo(m.created_at) || "—"}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <div className="flex justify-end gap-1.5">
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => setEditId(m.id)}
                        >
                          Edit
                        </Button>
                        {pendingDelete === m.id ? (
                          <>
                            <Button
                              type="button"
                              size="sm"
                              variant="destructive"
                              onClick={async () => {
                                await onDelete(m.id);
                                setPendingDelete(null);
                              }}
                            >
                              Confirm
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              onClick={() => setPendingDelete(null)}
                            >
                              Cancel
                            </Button>
                          </>
                        ) : (
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            onClick={() => setPendingDelete(m.id)}
                          >
                            <Trash2Icon className="size-3.5" />
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

// ── Mapping forms ───────────────────────────────────────────────────

interface MappingFormValues {
  mentra_user_id: string;
  gilbert_user_id: string;
  display_name: string;
  roles: string;
}

function parseRoles(raw: string): string[] {
  const items = raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  return items.length > 0 ? items : ["user"];
}

function NewMappingForm({
  onCancel,
  onSubmit,
}: {
  onCancel: () => void;
  onSubmit: (input: {
    mentra_user_id: string;
    gilbert_user_id: string;
    display_name?: string;
    roles?: string[];
  }) => Promise<void>;
}) {
  const [values, setValues] = useState<MappingFormValues>({
    mentra_user_id: "",
    gilbert_user_id: "",
    display_name: "",
    roles: "user",
  });
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const canSubmit =
    values.mentra_user_id.trim().length > 0 &&
    values.gilbert_user_id.trim().length > 0 &&
    !submitting;

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setErr(null);
    try {
      await onSubmit({
        mentra_user_id: values.mentra_user_id.trim(),
        gilbert_user_id: values.gilbert_user_id.trim(),
        display_name: values.display_name.trim() || undefined,
        roles: parseRoles(values.roles),
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }, [canSubmit, onSubmit, values]);

  return (
    <div className="rounded-md border border-border bg-muted/20 p-4 mb-3">
      <div className="grid grid-cols-2 gap-3">
        <LabelledInput
          label="Mentra email"
          value={values.mentra_user_id}
          onChange={(v) =>
            setValues((prev) => ({ ...prev, mentra_user_id: v }))
          }
          placeholder="alice@example.com"
        />
        <LabelledInput
          label="Gilbert user id"
          value={values.gilbert_user_id}
          onChange={(v) =>
            setValues((prev) => ({ ...prev, gilbert_user_id: v }))
          }
          placeholder="usr_alice"
        />
        <LabelledInput
          label="Display name (optional)"
          value={values.display_name}
          onChange={(v) =>
            setValues((prev) => ({ ...prev, display_name: v }))
          }
          placeholder="Alice"
        />
        <LabelledInput
          label="Roles (comma-separated)"
          value={values.roles}
          onChange={(v) => setValues((prev) => ({ ...prev, roles: v }))}
          placeholder="user"
        />
      </div>
      {err && (
        <p className="mt-2 text-xs text-destructive">{err}</p>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={onCancel}
          disabled={submitting}
        >
          Cancel
        </Button>
        <Button
          type="button"
          size="sm"
          onClick={() => void handleSubmit()}
          disabled={!canSubmit}
        >
          Create mapping
        </Button>
      </div>
    </div>
  );
}

function EditMappingRow({
  mapping,
  onCancel,
  onSubmit,
}: {
  mapping: MentraUserMapping;
  onCancel: () => void;
  onSubmit: (patch: {
    mentra_user_id?: string;
    gilbert_user_id?: string;
    display_name?: string;
    roles?: string[];
  }) => Promise<void>;
}) {
  const [values, setValues] = useState<MappingFormValues>({
    mentra_user_id: mapping.mentra_user_id,
    gilbert_user_id: mapping.gilbert_user_id,
    display_name: mapping.display_name,
    roles: mapping.roles.join(", "),
  });
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleSubmit = useCallback(async () => {
    setSubmitting(true);
    setErr(null);
    try {
      await onSubmit({
        mentra_user_id: values.mentra_user_id.trim(),
        gilbert_user_id: values.gilbert_user_id.trim(),
        display_name: values.display_name.trim(),
        roles: parseRoles(values.roles),
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  }, [onSubmit, values]);

  return (
    <tr className="bg-accent/40 align-top">
      <td className="px-3 py-2">
        <Input
          value={values.mentra_user_id}
          onChange={(e) =>
            setValues((prev) => ({
              ...prev,
              mentra_user_id: e.target.value,
            }))
          }
          className="h-8 text-xs font-mono"
        />
      </td>
      <td className="px-3 py-2">
        <Input
          value={values.gilbert_user_id}
          onChange={(e) =>
            setValues((prev) => ({
              ...prev,
              gilbert_user_id: e.target.value,
            }))
          }
          className="h-8 text-xs font-mono"
        />
      </td>
      <td className="px-3 py-2">
        <Input
          value={values.display_name}
          onChange={(e) =>
            setValues((prev) => ({
              ...prev,
              display_name: e.target.value,
            }))
          }
          className="h-8 text-xs"
        />
      </td>
      <td className="px-3 py-2" colSpan={2}>
        <Input
          value={values.roles}
          onChange={(e) =>
            setValues((prev) => ({ ...prev, roles: e.target.value }))
          }
          className="h-8 text-xs"
          placeholder="user, admin"
        />
        {err && (
          <p className="mt-1 text-[10px] text-destructive">{err}</p>
        )}
      </td>
      <td className="px-3 py-2 text-right">
        <div className="flex justify-end gap-1.5">
          <Button
            type="button"
            size="sm"
            onClick={() => void handleSubmit()}
            disabled={submitting}
          >
            Save
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={onCancel}
            disabled={submitting}
          >
            Cancel
          </Button>
        </div>
      </td>
    </tr>
  );
}

function LabelledInput({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="h-9 text-sm"
      />
    </label>
  );
}

// ── Active sessions section ─────────────────────────────────────────

function SessionsSection({
  sessions,
  loading,
  error,
  onReload,
}: {
  sessions: MentraSessionSummary[];
  loading: boolean;
  error: string | null;
  onReload: () => Promise<void>;
}) {
  return (
    <section>
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <h2 className="text-base font-semibold">Active sessions</h2>
          <p className="mt-0.5 text-xs text-muted-foreground max-w-prose">
            Live glasses sessions currently connected to Gilbert.
            Refreshes every {Math.round(SESSIONS_POLL_MS / 1000)}{" "}
            seconds.
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => void onReload()}
          disabled={loading}
        >
          <RefreshCwIcon
            className={cn("size-3.5 mr-1.5", loading && "animate-spin")}
          />
          Refresh
        </Button>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive mb-3">
          {error}
        </div>
      )}

      <div className="rounded-md border border-border overflow-hidden">
        {loading && sessions.length === 0 ? (
          <div className="flex items-center justify-center p-8 text-muted-foreground">
            <LoadingSpinner />
          </div>
        ) : sessions.length === 0 ? (
          <div className="p-6 text-sm text-muted-foreground">
            No active sessions. When a Mentra user activates Gilbert
            on their glasses, a row will appear here.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
              <tr>
                <th className="text-left font-medium px-3 py-2">
                  Session
                </th>
                <th className="text-left font-medium px-3 py-2">
                  Mentra user
                </th>
                <th className="text-left font-medium px-3 py-2">
                  Gilbert user
                </th>
                <th className="text-left font-medium px-3 py-2">
                  Device
                </th>
                <th className="text-left font-medium px-3 py-2 w-32">
                  Connected
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {sessions.map((s) => (
                <tr key={s.session_id} className="hover:bg-accent/30">
                  <td
                    className="px-3 py-2 font-mono text-[10px] text-muted-foreground truncate max-w-[180px]"
                    title={s.session_id}
                  >
                    {s.session_id}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {s.mentra_user_id || "—"}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {s.gilbert_user_id || "—"}
                  </td>
                  <td className="px-3 py-2 text-xs">
                    {s.capabilities.modelName || (
                      <span className="text-muted-foreground italic">
                        unknown
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {timeAgo(s.connected_at) || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
