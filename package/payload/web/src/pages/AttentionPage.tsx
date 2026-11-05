import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  AlertTriangle,
  RefreshCw,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  AttentionHealth,
  AttentionFlowCounts,
  AttentionOverviewResponse,
  AttentionTicket,
  AttentionTicketDetailResponse,
} from "@/lib/api";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useI18n } from "@/i18n";
import { cn, themedBody, timeAgo } from "@/lib/utils";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

const NEEDS_PERSON_STAGES = [
  "person/delivery",
  "person/answer",
  "person/review",
  "person/blocked",
] as const;

const OTHER_STAGES = ["working", "queued", "closed", "idle"] as const;

interface MandateBasisData {
  via?: string;
  rules?: string[] | string;
  amendments?: Array<{ summary?: string; description?: string; at?: number | string }>;
  [key: string]: unknown;
}

function parseBasisJson(jsonStr?: string): MandateBasisData {
  if (!jsonStr) return {};
  try {
    const parsed = JSON.parse(jsonStr);
    return typeof parsed === "object" && parsed !== null ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * Ticket detail drawer displaying task summary, mandate basis in plain words,
 * amendment chain, inputs, and outbox rows.
 */
function TicketDetailDrawer({
  ticketId,
  onClose,
}: {
  ticketId: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [detail, setDetail] = useState<AttentionTicketDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadDetail = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .getAttentionTicket(ticketId)
      .then((res) => {
        setDetail(res);
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load ticket details");
        setLoading(false);
      });
  }, [ticketId]);

  useEffect(() => {
    let cancelled = false;
    api
      .getAttentionTicket(ticketId)
      .then((res) => {
        if (!cancelled) {
          setDetail(res);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load ticket details");
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [ticketId]);

  // Handle escape key to close drawer
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const mandate = detail?.mandate;
  const basis = useMemo(() => parseBasisJson(mandate?.basis_json), [mandate?.basis_json]);

  const rulesList: string[] = useMemo(() => {
    if (!basis.rules) return [];
    if (Array.isArray(basis.rules)) return basis.rules.map(String);
    return [String(basis.rules)];
  }, [basis.rules]);

  const amendmentChain = useMemo(() => {
    const items: Array<{ title: string; subtitle?: string }> = [];
    if (mandate?.amended_from != null) {
      items.push({
        title: `Amended from mandate #${mandate.amended_from}`,
      });
    }
    if (Array.isArray(basis.amendments)) {
      for (const am of basis.amendments) {
        items.push({
          title: am.summary || am.description || "Amendment",
          subtitle: am.at ? String(am.at) : undefined,
        });
      }
    }
    return items;
  }, [mandate, basis.amendments]);

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Ticket ${ticketId} details`}
      className="fixed inset-0 z-50 flex justify-end bg-background/80 backdrop-blur-xs"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className={cn(
          themedBody,
          "relative flex h-full w-full max-w-2xl flex-col border-l border-border bg-card shadow-2xl",
        )}
      >
        {/* Drawer Header */}
        <header className="flex items-center justify-between border-b border-border p-4">
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm font-semibold">{ticketId}</span>
            {detail?.status && (
              <Badge tone={detail.status === "done" ? "success" : "secondary"}>
                {detail.status}
              </Badge>
            )}
          </div>
          <Button
            size="sm"
            ghost
            onClick={onClose}
            aria-label="Close drawer"
          >
            <X className="h-4 w-4" />
          </Button>
        </header>

        {/* Drawer Body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {loading ? (
            <div className="flex min-h-[16rem] items-center justify-center">
              <Spinner />
            </div>
          ) : error ? (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
              <div className="flex items-center gap-2 font-medium">
                <AlertCircle className="h-4 w-4" />
                <span>Error loading ticket</span>
              </div>
              <p className="mt-1">{error}</p>
              <Button size="sm" outlined className="mt-3" onClick={loadDetail}>
                Retry
              </Button>
            </div>
          ) : detail ? (
            <>
              {/* Task Summary */}
              <section className="space-y-3">
                <h3 className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
                  {t.attention?.ticketDetail?.taskSummary ?? "Task Summary"}
                </h3>
                <div className="rounded-lg border border-border bg-muted/20 p-4 space-y-2">
                  <h4 className="text-base font-semibold leading-snug">{detail.title}</h4>
                  <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3 pt-2">
                    <div>
                      <span className="text-muted-foreground block">Status</span>
                      <span className="font-medium font-mono">{detail.status}</span>
                    </div>
                    {detail.block_kind && (
                      <div>
                        <span className="text-muted-foreground block">Block Kind</span>
                        <span className="font-medium font-mono text-warning">
                          {detail.block_kind}
                        </span>
                      </div>
                    )}
                    {detail.assignee && (
                      <div>
                        <span className="text-muted-foreground block">Assignee</span>
                        <span className="font-medium font-mono">{detail.assignee}</span>
                      </div>
                    )}
                    {detail.priority != null && detail.priority !== 0 && (
                      <div>
                        <span className="text-muted-foreground block">Priority</span>
                        <span className="font-medium font-mono">{detail.priority}</span>
                      </div>
                    )}
                    {detail.ticket_kind && (
                      <div>
                        <span className="text-muted-foreground block">Ticket Kind</span>
                        <span className="font-medium font-mono">{detail.ticket_kind}</span>
                      </div>
                    )}
                    {detail.created_at ? (
                      <div>
                        <span className="text-muted-foreground block">Created</span>
                        <span className="font-medium font-mono">
                          {timeAgo(detail.created_at)}
                        </span>
                      </div>
                    ) : null}
                  </div>
                  {detail.description && (
                    <div className="pt-2 border-t border-border/50 text-xs text-muted-foreground whitespace-pre-wrap">
                      {detail.description}
                    </div>
                  )}
                  {detail.result && (
                    <div className="pt-2 border-t border-border/50 text-xs text-foreground whitespace-pre-wrap">
                      <span className="text-muted-foreground block mb-1">Result:</span>
                      {detail.result}
                    </div>
                  )}
                </div>
              </section>

              {/* Mandate Basis in PLAIN WORDS */}
              <section className="space-y-3">
                <h3 className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
                  {t.attention?.ticketDetail?.mandateBasis ?? "Mandate Basis"}
                </h3>
                {mandate ? (
                  <div className="rounded-lg border border-border bg-muted/20 p-4 space-y-3">
                    <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
                      <div>
                        <span className="text-muted-foreground block">
                          {t.attention?.ticketDetail?.strength ?? "Strength"}
                        </span>
                        <span className="font-medium font-mono uppercase">
                          {mandate.strength || "standard"}
                        </span>
                      </div>
                      <div>
                        <span className="text-muted-foreground block">
                          {t.attention?.ticketDetail?.via ?? "Via"}
                        </span>
                        <span className="font-medium font-mono">
                          {basis.via || mandate.scope || "direct"}
                        </span>
                      </div>
                      <div>
                        <span className="text-muted-foreground block">
                          {t.attention?.ticketDetail?.policyVersion ?? "Policy Version"}
                        </span>
                        <span className="font-medium font-mono">
                          {mandate.policy_version || "default"}
                        </span>
                      </div>
                      <div>
                        <span className="text-muted-foreground block">
                          {t.attention?.ticketDetail?.scope ?? "Scope"}
                        </span>
                        <span className="font-medium font-mono">{mandate.scope || "general"}</span>
                      </div>
                      <div>
                        <span className="text-muted-foreground block">
                          {t.attention?.ticketDetail?.status ?? "Status"}
                        </span>
                        <span className="font-medium font-mono">{mandate.status || "active"}</span>
                      </div>
                    </div>

                    {/* Rules rendered in plain words */}
                    <div className="pt-2 border-t border-border/50">
                      <span className="text-xs text-muted-foreground block mb-1.5">
                        {t.attention?.ticketDetail?.rules ?? "Rules"}
                      </span>
                      {rulesList.length > 0 ? (
                        <ul className="space-y-1 text-xs">
                          {rulesList.map((rule, idx) => (
                            <li key={idx} className="flex items-start gap-1.5 font-mono">
                              <span className="text-muted-foreground">•</span>
                              <span>{rule}</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="text-xs text-muted-foreground italic">
                          No specific rules attached.
                        </p>
                      )}
                    </div>
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground italic">
                    {t.attention?.ticketDetail?.noMandate ??
                      "No mandate basis recorded for this ticket."}
                  </p>
                )}
              </section>

              {/* Amendment Chain */}
              <section className="space-y-3">
                <h3 className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
                  {t.attention?.ticketDetail?.amendmentChain ?? "Amendment Chain"}
                </h3>
                {amendmentChain.length > 0 ? (
                  <ul className="rounded-lg border border-border divide-y divide-border/60 bg-muted/20 text-xs">
                    {amendmentChain.map((am, idx) => (
                      <li key={idx} className="p-3">
                        <span className="font-medium font-mono">{am.title}</span>
                        {am.subtitle && (
                          <span className="text-muted-foreground ml-2 text-xs">
                            ({am.subtitle})
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-xs text-muted-foreground italic">
                    {t.attention?.ticketDetail?.noAmendments ?? "No amendments recorded."}
                  </p>
                )}
              </section>

              {/* Inputs */}
              <section className="space-y-3">
                <h3 className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
                  {t.attention?.ticketDetail?.inputs ?? "Inputs"} ({detail.inputs?.length ?? 0})
                </h3>
                {detail.inputs && detail.inputs.length > 0 ? (
                  <div className="space-y-2">
                    {detail.inputs.map((inp) => (
                      <div
                        key={inp.id}
                        className="rounded-lg border border-border bg-muted/20 p-3 text-xs space-y-1.5"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5">
                            <Badge tone="secondary" className="font-mono text-[10px]">
                              {inp.kind}
                            </Badge>
                            {inp.is_person ? (
                              <Badge tone="warning" className="text-[10px]">
                                Person
                              </Badge>
                            ) : null}
                            {inp.context_only ? (
                              <Badge tone="outline" className="text-[10px]">
                                Context only
                              </Badge>
                            ) : null}
                          </div>
                          {inp.created_at ? (
                            <span className="text-muted-foreground font-mono">
                              {timeAgo(inp.created_at)}
                            </span>
                          ) : null}
                        </div>
                        {inp.body && (
                          <p className="font-mono text-muted-foreground whitespace-pre-wrap break-words">
                            {inp.body}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground italic">
                    {t.attention?.ticketDetail?.noInputs ?? "No inputs recorded."}
                  </p>
                )}
              </section>

              {/* Outbox Rows */}
              <section className="space-y-3">
                <h3 className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
                  {t.attention?.ticketDetail?.outbox ?? "Outbox"} ({detail.outbox?.length ?? 0})
                </h3>
                {detail.outbox && detail.outbox.length > 0 ? (
                  <div className="space-y-2">
                    {detail.outbox.map((out) => {
                      const tone =
                        out.status === "sent"
                          ? "success"
                          : out.status === "failed"
                            ? "destructive"
                            : out.status === "dropped"
                              ? "warning"
                              : "secondary";
                      return (
                        <div
                          key={out.id}
                          className="rounded-lg border border-border bg-muted/20 p-3 text-xs space-y-1.5"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <div className="flex items-center gap-2">
                              <span className="font-semibold font-mono">{out.source}</span>
                              <Badge tone={tone} className="text-[10px] font-mono">
                                {out.status}
                              </Badge>
                              <span className="text-muted-foreground font-mono">
                                ({out.purpose})
                              </span>
                            </div>
                            {out.created_at ? (
                              <span className="text-muted-foreground font-mono">
                                {timeAgo(out.created_at)}
                              </span>
                            ) : null}
                          </div>
                          {out.body && (
                            <p className="font-mono text-muted-foreground whitespace-pre-wrap break-words">
                              {out.body}
                            </p>
                          )}
                          {out.last_error && (
                            <p className="text-destructive font-mono text-[11px]">
                              Error: {out.last_error}
                            </p>
                          )}
                          <div className="flex items-center gap-3 text-[11px] text-muted-foreground font-mono pt-1">
                            <span>Attempts: {out.attempts}</span>
                            {out.sent_at ? <span>Sent: {timeAgo(out.sent_at)}</span> : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground italic">
                    {t.attention?.ticketDetail?.noOutbox ?? "No outbox records."}
                  </p>
                )}
              </section>
            </>
          ) : null}
        </div>
      </div>
    </div>,
    document.body,
  );
}

/**
 * Ticket card item in Attention lists.
 */
function TicketCard({
  ticket,
  onSelect,
}: {
  ticket: AttentionTicket;
  onSelect: (id: string) => void;
}) {
  const isPerson = ticket.stage.startsWith("person/");
  const stageTone = isPerson
    ? ticket.stage === "person/delivery"
      ? "destructive"
      : ticket.stage === "person/answer"
        ? "warning"
        : ticket.stage === "person/review"
          ? "secondary"
          : "outline"
    : ticket.stage === "closed"
      ? "success"
      : "secondary";

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onSelect(ticket.id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(ticket.id);
        }
      }}
      className="group flex flex-col justify-between gap-2 rounded-lg border border-border bg-card p-3.5 shadow-xs transition hover:border-foreground/30 hover:bg-muted/10 cursor-pointer"
    >
      <div className="flex items-start justify-between gap-3">
        <h4 className="text-sm font-semibold leading-snug group-hover:text-foreground">
          {ticket.title}
        </h4>
        <Badge tone={stageTone} className="shrink-0 text-[11px] font-mono">
          {ticket.stage}
        </Badge>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground font-mono">
        <span>#{ticket.id}</span>
        <span>•</span>
        <span>status: {ticket.status}</span>
        {ticket.block_kind && (
          <>
            <span>•</span>
            <span className="text-warning font-semibold">
              blocked: {ticket.block_kind}
            </span>
          </>
        )}
        {ticket.assignee && (
          <>
            <span>•</span>
            <span>@{ticket.assignee}</span>
          </>
        )}
        {ticket.priority != null && ticket.priority !== 0 && (
          <>
            <span>•</span>
            <span>p{ticket.priority}</span>
          </>
        )}
        {ticket.created_at ? (
          <>
            <span>•</span>
            <span>{timeAgo(ticket.created_at)}</span>
          </>
        ) : null}
      </div>
    </div>
  );
}

export default function AttentionPage() {
  const { t } = useI18n();
  const { setTitle, setEnd } = usePageHeader();
  const [overview, setOverview] = useState<AttentionOverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedTicketId, setSelectedTicketId] = useState<string | null>(null);

  const loadData = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .getAttentionOverview()
      .then((data) => {
        setOverview(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(
          err instanceof Error ? err.message : "Failed to load Attention overview",
        );
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    setTitle(t.attention?.title ?? "Attention");
    let cancelled = false;
    api
      .getAttentionOverview()
      .then((data) => {
        if (!cancelled) {
          setOverview(data);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to load Attention overview",
          );
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [setTitle, t.attention?.title]);

  useEffect(() => {
    setEnd(
      <Button
        size="sm"
        outlined
        onClick={() => void loadData()}
        disabled={loading}
        aria-label="Refresh Attention view"
        className="gap-1.5"
      >
        <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
        <span>{t.common?.refresh ?? "Refresh"}</span>
      </Button>,
    );
    return () => setEnd(null);
  }, [setEnd, loadData, loading, t.common?.refresh]);

  // Derived tickets grouped by stage
  const ticketsByStage = useMemo(() => {
    if (!overview) return {};
    return overview.tickets_by_stage || overview.stages || {};
  }, [overview]);

  const needsPersonTickets = useMemo(() => {
    if (!overview) return [];
    if (overview.needs_person) return overview.needs_person;
    // Fallback: derive from ticketsByStage
    const out: AttentionTicket[] = [];
    for (const st of NEEDS_PERSON_STAGES) {
      if (ticketsByStage[st]) {
        out.push(...ticketsByStage[st]);
      }
    }
    return out;
  }, [overview, ticketsByStage]);

  const stageLabels: Record<string, { label: string; description: string }> = useMemo(
    () => ({
      "person/delivery": {
        label: t.attention?.stages?.delivery?.label ?? "Delivery Failure",
        description:
          t.attention?.stages?.delivery?.description ??
          "Outbox failed to deliver message to destination",
      },
      "person/answer": {
        label: t.attention?.stages?.answer?.label ?? "Awaiting Answer",
        description:
          t.attention?.stages?.answer?.description ??
          "Waiting for human answer or input",
      },
      "person/review": {
        label: t.attention?.stages?.review?.label ?? "Awaiting Review",
        description:
          t.attention?.stages?.review?.description ??
          "Waiting for human review or approval",
      },
      "person/blocked": {
        label: t.attention?.stages?.blocked?.label ?? "Blocked",
        description:
          t.attention?.stages?.blocked?.description ??
          "Task blocked or execution failed",
      },
      working: {
        label: t.attention?.stages?.working?.label ?? "Working",
        description:
          t.attention?.stages?.working?.description ??
          "Active execution running or claimed by worker",
      },
      queued: {
        label: t.attention?.stages?.queued?.label ?? "Queued",
        description:
          t.attention?.stages?.queued?.description ??
          "Ready or dispatched for execution",
      },
      closed: {
        label: t.attention?.stages?.closed?.label ?? "Closed",
        description:
          t.attention?.stages?.closed?.description ??
          "Completed and resolved tasks",
      },
      idle: {
        label: t.attention?.stages?.idle?.label ?? "Idle",
        description:
          t.attention?.stages?.idle?.description ??
          "Tasks without active dispatch or waiting state",
      },
    }),
    [t.attention?.stages],
  );

  if (loading && !overview) {
    return (
      <div
        className="flex min-h-[20rem] flex-1 items-center justify-center"
        aria-busy="true"
        aria-live="polite"
      >
        <Spinner />
      </div>
    );
  }

  if (error && !overview) {
    const isSuperagentDisabled =
      error.toLowerCase().includes("superagent is disabled") ||
      error.toLowerCase().includes("404");

    return (
      <div className="p-6">
        <div className="rounded-lg border border-border bg-card p-6 shadow-xs max-w-xl">
          <div className="flex items-center gap-2 text-warning font-semibold">
            <AlertTriangle className="h-5 w-5" />
            <span>
              {isSuperagentDisabled
                ? "Superagent Disabled"
                : "Unable to load Attention View"}
            </span>
          </div>
          <p className="mt-2 text-sm text-muted-foreground">
            {isSuperagentDisabled
              ? "Superagent is currently disabled in your configuration. Enable it in config.yaml (`superagent.enabled: true`) to activate orchestrator attention queues."
              : error}
          </p>
          <Button size="sm" outlined className="mt-4" onClick={() => void loadData()}>
            {t.common?.retry ?? "Retry"}
          </Button>
        </div>
      </div>
    );
  }

  const health: AttentionHealth = overview?.health ?? {
    paused: false,
    outbox_due: 0,
    outbox_failed: 0,
    eval_queued: 0,
    unavailable_24h: 0,
  };

  const flow: AttentionFlowCounts = overview?.flow_counts_24h ?? {
    triggers_received: 0,
    decisions_by_outcome: {},
    tickets_created: 0,
    replies_sent: 0,
    replies_failed: 0,
    replies_dropped: 0,
    replies: { sent: 0, failed: 0, dropped: 0 },
  };

  return (
    <div className="p-6 space-y-8 max-w-6xl">
      {/* ── (1) Needs-me section FIRST ───────────────────────────────────── */}
      {/* EMPTY-STATE RULE: when needs-me is empty, the section renders exactly one line
          (the empty-state text) and no extra chrome/cards. */}
      <section data-testid="needs-me-section" aria-label="Needs attention">
        {needsPersonTickets.length === 0 ? (
          <div
            data-testid="needs-me-empty"
            className="text-sm text-muted-foreground py-1"
          >
            {t.attention?.needsMeEmpty ?? "No tickets currently need your attention."}
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold tracking-tight">
                {t.attention?.needsMeTitle ?? "Needs Attention"}
              </h2>
              <Badge tone="warning" className="text-xs">
                {needsPersonTickets.length}
              </Badge>
            </div>

            {/* Grouped by stage in the exact derived-stage order:
                delivery, answer, review, blocked */}
            <div className="space-y-6">
              {NEEDS_PERSON_STAGES.map((stageKey) => {
                const stageTickets = ticketsByStage[stageKey] || [];
                if (stageTickets.length === 0) return null;
                const info = stageLabels[stageKey];
                return (
                  <div
                    key={stageKey}
                    data-testid={`needs-me-stage-${stageKey}`}
                    className="space-y-2.5"
                  >
                    <div className="flex items-baseline justify-between gap-2 border-b border-border/60 pb-1.5">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-semibold">{info.label}</span>
                        <Badge tone="secondary" className="text-xs">
                          {stageTickets.length}
                        </Badge>
                      </div>
                      <span className="text-xs text-muted-foreground">
                        {info.description}
                      </span>
                    </div>

                    <div className="grid gap-2.5 sm:grid-cols-2">
                      {stageTickets.map((tkt) => (
                        <TicketCard
                          key={tkt.id}
                          ticket={tkt}
                          onSelect={setSelectedTicketId}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </section>

      {/* ── (2) Health Strip ────────────────────────────────────────────── */}
      <section data-testid="health-strip" aria-label="System Health">
        <Card>
          <CardHeader className="py-3 px-4">
            <div className="flex items-center justify-between">
              <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
                {t.attention?.healthTitle ?? "System Health"}
              </CardTitle>
              <Badge tone={health.paused ? "destructive" : "success"} className="text-xs">
                {health.paused
                  ? t.attention?.paused ?? "Paused"
                  : t.attention?.active ?? "Active"}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="px-4 pb-3 pt-0">
            <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.outboxDue ?? "Outbox Due"}
                </span>
                <span className="font-mono text-base font-semibold">
                  {health.outbox_due}
                </span>
              </div>
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.outboxFailed ?? "Outbox Failed"}
                </span>
                <span
                  className={cn(
                    "font-mono text-base font-semibold",
                    health.outbox_failed > 0 && "text-destructive",
                  )}
                >
                  {health.outbox_failed}
                </span>
              </div>
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.evalQueued ?? "Eval Queued"}
                </span>
                <span className="font-mono text-base font-semibold">
                  {health.eval_queued}
                </span>
              </div>
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.unavailable24h ?? "Unavailable (24h)"}
                </span>
                <span className="font-mono text-base font-semibold">
                  {health.unavailable_24h}
                </span>
              </div>
            </div>
          </CardContent>
        </Card>
      </section>

      {/* ── (3) 24h Flow Counts ─────────────────────────────────────────── */}
      <section data-testid="flow-counts" aria-label="Activity Flow">
        <Card>
          <CardHeader className="py-3 px-4">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
              {t.attention?.flowCountsTitle ?? "24h Activity Flow"}
            </CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-3 pt-0 space-y-3">
            <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.triggersReceived ?? "Triggers Received"}
                </span>
                <span className="font-mono text-base font-semibold">
                  {flow.triggers_received}
                </span>
              </div>
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.ticketsCreated ?? "Tickets Created"}
                </span>
                <span className="font-mono text-base font-semibold">
                  {flow.tickets_created}
                </span>
              </div>
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.repliesSent ?? "Replies Sent"}
                </span>
                <span className="font-mono text-base font-semibold">
                  {flow.replies_sent}
                </span>
              </div>
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.repliesFailed ?? "Replies Failed"}
                </span>
                <span
                  className={cn(
                    "font-mono text-base font-semibold",
                    flow.replies_failed > 0 && "text-destructive",
                  )}
                >
                  {flow.replies_failed}
                </span>
              </div>
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.repliesDropped ?? "Replies Dropped"}
                </span>
                <span
                  className={cn(
                    "font-mono text-base font-semibold",
                    flow.replies_dropped > 0 && "text-warning",
                  )}
                >
                  {flow.replies_dropped}
                </span>
              </div>
            </div>

            {/* Decisions breakdown */}
            {flow.decisions_by_outcome &&
              Object.keys(flow.decisions_by_outcome).length > 0 && (
                <div className="flex flex-wrap items-center gap-2 border-t border-border/60 pt-2 text-xs">
                  <span className="text-muted-foreground">Decisions:</span>
                  {Object.entries(flow.decisions_by_outcome).map(([outcome, cnt]) => (
                    <Badge key={outcome} tone="outline" className="font-mono text-xs">
                      {outcome}: {cnt}
                    </Badge>
                  ))}
                </div>
              )}
          </CardContent>
        </Card>
      </section>

      {/* ── (4) Executor Load ───────────────────────────────────────────── */}
      {/* slots in use — from the overview payload if present; if the 5.3 overview shape
          lacks it, render the section from the fields that exist and note the gap */}
      <section data-testid="executor-load" aria-label="Executor Load">
        <Card>
          <CardHeader className="py-3 px-4">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
              {t.attention?.executorLoadTitle ?? "Executor Load"}
            </CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-3 pt-0">
            <div className="flex flex-wrap items-center gap-6 text-sm">
              <div>
                <span className="text-xs text-muted-foreground block">
                  {t.attention?.executorLoadSlots ?? "Slots in Use"}
                </span>
                <span className="font-mono text-base font-semibold">
                  {overview?.executor_load?.slots_in_use != null
                    ? `${overview.executor_load.slots_in_use}${
                        overview.executor_load.max_slots
                          ? ` / ${overview.executor_load.max_slots}`
                          : ""
                      }`
                    : "Not reported"}
                </span>
              </div>
              <div>
                <span className="text-xs text-muted-foreground block">
                  Active Working Tickets
                </span>
                <span className="font-mono text-base font-semibold">
                  {ticketsByStage.working?.length ?? 0}
                </span>
              </div>
              {overview?.executor_load?.slots_in_use == null && (
                <span className="text-xs text-muted-foreground italic">
                  {t.attention?.executorLoadUnavailable ??
                    "Executor slot metrics are not reported by the backend."}
                </span>
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      {/* ── (5) Stage-grouped ticket lists for working/queued/closed/idle BELOW needs-me ── */}
      <section data-testid="other-stages-section" aria-label="Other stages">
        <div className="space-y-6">
          <h2 className="text-base font-semibold tracking-tight text-foreground">
            {t.attention?.otherStagesTitle ?? "Lifecycle Stages"}
          </h2>

          <div className="space-y-6">
            {OTHER_STAGES.map((stageKey) => {
              const stageTickets = ticketsByStage[stageKey] || [];
              const info = stageLabels[stageKey];
              return (
                <div
                  key={stageKey}
                  data-testid={`stage-section-${stageKey}`}
                  className="space-y-2.5"
                >
                  <div className="flex items-baseline justify-between gap-2 border-b border-border/60 pb-1.5">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold">{info.label}</span>
                      <Badge tone="secondary" className="text-xs">
                        {stageTickets.length}
                      </Badge>
                    </div>
                    <span className="text-xs text-muted-foreground">
                      {info.description}
                    </span>
                  </div>

                  {stageTickets.length > 0 ? (
                    <div className="grid gap-2.5 sm:grid-cols-2">
                      {stageTickets.map((tkt) => (
                        <TicketCard
                          key={tkt.id}
                          ticket={tkt}
                          onSelect={setSelectedTicketId}
                        />
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-muted-foreground italic py-1">
                      No tickets currently in {info.label.toLowerCase()}.
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </section>

      {/* ── Ticket Detail Drawer ────────────────────────────────────────── */}
      {selectedTicketId && (
        <TicketDetailDrawer
          ticketId={selectedTicketId}
          onClose={() => setSelectedTicketId(null)}
        />
      )}
    </div>
  );
}