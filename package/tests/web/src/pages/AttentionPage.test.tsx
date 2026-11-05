// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getAttentionOverview: vi.fn(),
  getAttentionTicket: vi.fn(),
  getAttentionTickets: vi.fn(),
  getAttentionThreads: vi.fn(),
  getAttentionThread: vi.fn(),
  getAttentionDecisions: vi.fn(),
  getAttentionSettings: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
  setManagementProfile: vi.fn(),
  getManagementProfile: vi.fn(() => ""),
}));

let container: HTMLDivElement;
let root: Root;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

async function waitFor(cond: () => boolean, timeoutMs = 5000) {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error("waitFor: condition never became true");
    }
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
}

function click(el: Element | null | undefined) {
  if (!el) throw new Error("element not rendered");
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

async function renderAttentionPage() {
  const [{ default: AttentionPage }, { I18nProvider }, { PageHeaderProvider }] =
    await Promise.all([
      import("./AttentionPage"),
      import("@/i18n"),
      import("@/contexts/PageHeaderProvider"),
    ]);

  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);

  await act(async () => {
    root.render(
      <I18nProvider>
        <MemoryRouter>
          <PageHeaderProvider pluginTabs={[]}>
            <AttentionPage />
          </PageHeaderProvider>
        </MemoryRouter>
      </I18nProvider>,
    );
  });
}

beforeEach(() => {
  for (const fn of Object.values(apiMocks)) fn.mockReset();
});

afterEach(async () => {
  if (root) {
    await act(async () => {
      root.unmount();
    });
  }
  container?.remove();
});

describe("AttentionPage", () => {
  it("enforces EMPTY-STATE RULE: exactly one line and no extra chrome/cards when needs-me is empty", async () => {
    apiMocks.getAttentionOverview.mockResolvedValue({
      health: {
        paused: false,
        outbox_due: 0,
        outbox_failed: 0,
        eval_queued: 0,
        unavailable_24h: 0,
      },
      flow_counts_24h: {
        triggers_received: 12,
        decisions_by_outcome: { accept: 8 },
        tickets_created: 4,
        replies_sent: 5,
        replies_failed: 0,
        replies_dropped: 0,
        replies: { sent: 5, failed: 0, dropped: 0 },
      },
      needs_person: [],
      needs_person_count: 0,
      stages: {
        "person/delivery": [],
        "person/answer": [],
        "person/review": [],
        "person/blocked": [],
        working: [{ id: "t_work1", stage: "working", title: "Working Task", status: "running" }],
        queued: [],
        closed: [],
        idle: [],
      },
      tickets_by_stage: {
        "person/delivery": [],
        "person/answer": [],
        "person/review": [],
        "person/blocked": [],
        working: [{ id: "t_work1", stage: "working", title: "Working Task", status: "running" }],
        queued: [],
        closed: [],
        idle: [],
      },
      tickets: [{ id: "t_work1", stage: "working", title: "Working Task", status: "running" }],
    });

    await renderAttentionPage();

    await waitFor(() => Boolean(document.querySelector("[data-testid='needs-me-section']")));

    const needsMeSection = document.querySelector("[data-testid='needs-me-section']");
    expect(needsMeSection).not.toBeNull();

    // Check empty-state element exists
    const emptyEl = document.querySelector("[data-testid='needs-me-empty']");
    expect(emptyEl).not.toBeNull();
    expect(emptyEl?.textContent?.trim()).toBe("No tickets currently need your attention.");

    // Check no extra chrome/cards inside needs-me-section
    // Children count must be exactly 1 (the single empty-state text line)
    expect(needsMeSection?.children.length).toBe(1);
    expect(needsMeSection?.querySelector("h2")).toBeNull();
    expect(needsMeSection?.querySelector(".card, [class*='rounded-lg border']")).toBeNull();
  });

  it("orders needs-me tickets strictly in derived-stage order (delivery, answer, review, blocked)", async () => {
    // Provide tickets in arbitrary/reverse order to prove sorting / grouping precedence
    const tickets = [
      { id: "t_blk", stage: "person/blocked", title: "Blocked Task", status: "blocked" },
      { id: "t_ans", stage: "person/answer", title: "Answer Needed", status: "blocked", block_kind: "needs_input" },
      { id: "t_del", stage: "person/delivery", title: "Delivery Failed", status: "running" },
      { id: "t_rev", stage: "person/review", title: "Review Required", status: "blocked", block_kind: "review" },
    ];

    apiMocks.getAttentionOverview.mockResolvedValue({
      health: {
        paused: true,
        outbox_due: 2,
        outbox_failed: 1,
        eval_queued: 0,
        unavailable_24h: 3,
      },
      flow_counts_24h: {
        triggers_received: 20,
        decisions_by_outcome: { accept: 15, reject: 5 },
        tickets_created: 10,
        replies_sent: 8,
        replies_failed: 1,
        replies_dropped: 0,
        replies: { sent: 8, failed: 1, dropped: 0 },
      },
      needs_person: tickets,
      needs_person_count: 4,
      stages: {
        "person/delivery": [tickets[2]],
        "person/answer": [tickets[1]],
        "person/review": [tickets[3]],
        "person/blocked": [tickets[0]],
        working: [],
        queued: [],
        closed: [],
        idle: [],
      },
      tickets_by_stage: {
        "person/delivery": [tickets[2]],
        "person/answer": [tickets[1]],
        "person/review": [tickets[3]],
        "person/blocked": [tickets[0]],
        working: [],
        queued: [],
        closed: [],
        idle: [],
      },
      tickets,
    });

    await renderAttentionPage();

    await waitFor(() => Boolean(document.querySelector("[data-testid='needs-me-section'] h2")));

    const needsMeSection = document.querySelector("[data-testid='needs-me-section']");
    expect(needsMeSection).not.toBeNull();

    // Verify stages rendered in exact order: delivery, answer, review, blocked
    const stageElements = needsMeSection?.querySelectorAll("[data-testid^='needs-me-stage-']");
    expect(stageElements).not.toBeNull();
    expect(stageElements?.length).toBe(4);

    const renderedStageKeys = Array.from(stageElements || []).map((el) =>
      el.getAttribute("data-testid")?.replace("needs-me-stage-", ""),
    );

    expect(renderedStageKeys).toEqual([
      "person/delivery",
      "person/answer",
      "person/review",
      "person/blocked",
    ]);

    // Verify titles within stages
    expect(stageElements?.[0].textContent).toContain("Delivery Failed");
    expect(stageElements?.[1].textContent).toContain("Answer Needed");
    expect(stageElements?.[2].textContent).toContain("Review Required");
    expect(stageElements?.[3].textContent).toContain("Blocked Task");
  });

  it("renders health strip, 24h flow counts, executor load, and other stages", async () => {
    apiMocks.getAttentionOverview.mockResolvedValue({
      health: {
        paused: false,
        outbox_due: 5,
        outbox_failed: 2,
        eval_queued: 1,
        unavailable_24h: 0,
      },
      flow_counts_24h: {
        triggers_received: 42,
        decisions_by_outcome: { accept: 30, reject: 12 },
        tickets_created: 18,
        replies_sent: 25,
        replies_failed: 2,
        replies_dropped: 1,
        replies: { sent: 25, failed: 2, dropped: 1 },
      },
      needs_person: [],
      needs_person_count: 0,
      stages: {
        "person/delivery": [],
        "person/answer": [],
        "person/review": [],
        "person/blocked": [],
        working: [{ id: "t_work", stage: "working", title: "Working Job", status: "running" }],
        queued: [{ id: "t_q", stage: "queued", title: "Queued Job", status: "todo" }],
        closed: [{ id: "t_done", stage: "closed", title: "Done Job", status: "done" }],
        idle: [],
      },
      tickets_by_stage: {
        "person/delivery": [],
        "person/answer": [],
        "person/review": [],
        "person/blocked": [],
        working: [{ id: "t_work", stage: "working", title: "Working Job", status: "running" }],
        queued: [{ id: "t_q", stage: "queued", title: "Queued Job", status: "todo" }],
        closed: [{ id: "t_done", stage: "closed", title: "Done Job", status: "done" }],
        idle: [],
      },
      tickets: [],
    });

    await renderAttentionPage();

    await waitFor(() => Boolean(document.querySelector("[data-testid='health-strip']")));

    // Health strip assertions
    const healthStrip = document.querySelector("[data-testid='health-strip']");
    expect(healthStrip?.textContent).toContain("Active");
    expect(healthStrip?.textContent).toContain("Outbox Due");
    expect(healthStrip?.textContent).toContain("5");
    expect(healthStrip?.textContent).toContain("Outbox Failed");
    expect(healthStrip?.textContent).toContain("2");

    // Flow counts assertions
    const flowCounts = document.querySelector("[data-testid='flow-counts']");
    expect(flowCounts?.textContent).toContain("42"); // triggers
    expect(flowCounts?.textContent).toContain("18"); // tickets
    expect(flowCounts?.textContent).toContain("25"); // sent

    // Executor load assertions
    const executorLoad = document.querySelector("[data-testid='executor-load']");
    expect(executorLoad?.textContent).toContain("Executor Load");
    expect(executorLoad?.textContent).toContain("Active Working Tickets");

    // Other stages assertions
    const otherStages = document.querySelector("[data-testid='other-stages-section']");
    expect(otherStages?.textContent).toContain("Working");
    expect(otherStages?.textContent).toContain("Working Job");
    expect(otherStages?.textContent).toContain("Queued");
    expect(otherStages?.textContent).toContain("Queued Job");
    expect(otherStages?.textContent).toContain("Closed");
    expect(otherStages?.textContent).toContain("Done Job");
  });

  it("opens ticket detail drawer with plain words mandate basis, amendment chain, inputs and outbox", async () => {
    const tkt = {
      id: "t_inspect",
      stage: "person/answer",
      title: "Ticket To Inspect",
      status: "blocked",
    };

    apiMocks.getAttentionOverview.mockResolvedValue({
      health: { paused: false, outbox_due: 0, outbox_failed: 0, eval_queued: 0, unavailable_24h: 0 },
      flow_counts_24h: {
        triggers_received: 0,
        decisions_by_outcome: {},
        tickets_created: 0,
        replies_sent: 0,
        replies_failed: 0,
        replies_dropped: 0,
        replies: { sent: 0, failed: 0, dropped: 0 },
      },
      needs_person: [tkt],
      needs_person_count: 1,
      stages: {
        "person/delivery": [],
        "person/answer": [tkt],
        "person/review": [],
        "person/blocked": [],
        working: [],
        queued: [],
        closed: [],
        idle: [],
      },
      tickets_by_stage: {
        "person/delivery": [],
        "person/answer": [tkt],
        "person/review": [],
        "person/blocked": [],
        working: [],
        queued: [],
        closed: [],
        idle: [],
      },
      tickets: [tkt],
    });

    apiMocks.getAttentionTicket.mockResolvedValue({
      id: "t_inspect",
      title: "Ticket To Inspect",
      status: "blocked",
      block_kind: "needs_input",
      mandate: {
        id: 42,
        ticket_id: "t_inspect",
        policy_version: "v2.1",
        scope: "project-omega",
        strength: "enforced",
        basis_json: JSON.stringify({
          via: "slack_incident_channel",
          rules: ["rule_auto_escalate", "rule_require_lead_signoff"],
          amendments: [{ summary: "Policy upgraded from v1 to v2.1", at: 1700000000 }],
        }),
        status: "active",
        amended_from: 10,
        created_at: 1699999000,
      },
      inputs: [
        {
          id: 1,
          ticket_id: "t_inspect",
          kind: "user_query",
          body: "What is the status of the database migration?",
          is_person: 1,
          context_only: 0,
          created_at: 1699999500,
        },
      ],
      outbox: [
        {
          id: 101,
          ticket_id: "t_inspect",
          source: "slack",
          purpose: "incident_update",
          body: "Database migration in progress",
          status: "sent",
          attempts: 1,
          created_at: 1699999600,
          sent_at: 1699999650,
        },
      ],
      task: { id: "t_inspect" },
    });

    await renderAttentionPage();

    await waitFor(() => Boolean(document.querySelector("[data-testid='needs-me-stage-person/answer']")));

    // Click on the ticket card to open drawer
    const ticketCard = document.querySelector("[data-testid='needs-me-stage-person/answer'] [role='button']");
    expect(ticketCard).not.toBeNull();
    await act(async () => {
      click(ticketCard);
    });

    // Wait for drawer dialog to mount and load
    await waitFor(() => Boolean(document.querySelector("[role='dialog']")));

    expect(apiMocks.getAttentionTicket).toHaveBeenCalledWith("t_inspect");

    const drawer = document.querySelector("[role='dialog']");
    expect(drawer).not.toBeNull();

    // Plain words mandate basis verification (NOT raw JSON)
    expect(drawer?.textContent).toContain("Task Summary");
    expect(drawer?.textContent).toContain("Mandate Basis");
    expect(drawer?.textContent?.toLowerCase()).toContain("enforced"); // strength
    expect(drawer?.textContent).toContain("slack_incident_channel"); // via
    expect(drawer?.textContent).toContain("v2.1"); // policy version
    expect(drawer?.textContent).toContain("rule_auto_escalate"); // rules
    expect(drawer?.textContent).toContain("rule_require_lead_signoff");

    // Amendment chain verification
    expect(drawer?.textContent).toContain("Amendment Chain");
    expect(drawer?.textContent).toContain("Amended from mandate #10");
    expect(drawer?.textContent).toContain("Policy upgraded from v1 to v2.1");

    // Inputs verification
    expect(drawer?.textContent).toContain("What is the status of the database migration?");
    expect(drawer?.textContent).toContain("user_query");

    // Outbox verification
    expect(drawer?.textContent).toContain("Database migration in progress");
    expect(drawer?.textContent).toContain("slack");
    expect(drawer?.textContent).toContain("sent");

    // Test closing drawer
    const closeBtn = drawer?.querySelector("button[aria-label='Close drawer']");
    expect(closeBtn).not.toBeNull();
    await act(async () => {
      click(closeBtn);
    });

    await waitFor(() => document.querySelector("[role='dialog']") === null);
  });
});
