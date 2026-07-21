"use client";

import {
  AlertCircleIcon,
  ArrowRightIcon,
  BadgeCheckIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  ClipboardCheckIcon,
  ExternalLinkIcon,
  FileCheck2Icon,
  FileTextIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  SparklesIcon,
  TargetIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";

interface FormOption {
  value: string;
  label: string;
}

interface FormField {
  path: string;
  label: string;
  reason: string;
  control: "text" | "number" | "select" | "multi_select";
  unit?: string | null;
  required: boolean;
  value?: unknown;
  options: FormOption[];
}

interface WorkflowProgressStep {
  key: string;
  label: string;
  status: "pending" | "running" | "completed" | "waiting" | "failed";
}

interface CoverageReviewEnvelope {
  kind: "insurance_coverage_review";
  status:
    | "intake_required"
    | "waiting_input"
    | "waiting_confirmation"
    | "completed"
    | "suspended"
    | "cancelled"
    | "failed"
    | "running";
  message: string;
  task_id?: string | null;
  customer_id?: string | null;
  thread_id?: string | null;
  intake?: Record<string, unknown> | null;
  form_fields: FormField[];
  internal_report_url?: string | null;
  customer_report_url?: string | null;
  review_revision?: number | null;
  kernel_hash?: string | null;
  profile_version?: number | null;
  review_packet?: ReviewPacket | null;
  progress_steps?: WorkflowProgressStep[];
  can_retry?: boolean;
}

interface ReviewDimensionFact {
  dimension_code: string;
  dimension_name: string;
  state: string;
  existing_value?: string | number | null;
  ideal_value?: string | number | null;
  gap_value?: string | number | null;
  existing_display: string;
  ideal_display: string;
  gap_display: string;
  unit: string;
  priority_rank?: number | null;
}

interface ReviewPacket {
  customer_identity: string[];
  trigger_context: string[];
  dimension_facts: ReviewDimensionFact[];
  auxiliary_observations: string[];
  preserve_items: string[];
  correction_items: string[];
  warnings: string[];
  diff_summary: string[];
}

type ReviewFeedbackAction = "revise_facts" | "revise_narrative" | "reject";

type FormValues = Record<string, string | string[]>;

const AUTO_ADVANCE_MAX_ATTEMPTS = 12;
const AUTO_ADVANCE_RETRY_MS = 1500;
const taskAdvanceRequests = new Map<string, Promise<CoverageReviewEnvelope>>();

const FORM_GROUPS = [
  {
    key: "identity",
    title: "客户身份",
    description: "用于匹配客户中心、客户档案与保单报告。",
    fields: ["customer_name", "age", "gender"],
  },
  {
    key: "cashflow",
    title: "家庭现金流",
    description: "确认收入、支出、负债与可承受预算。",
    fields: [
      "annual_income_wan",
      "spouse_annual_income_wan",
      "family_expense_yuan_month",
      "large_loan_wan",
      "annual_premium_budget_yuan",
    ],
  },
  {
    key: "protection",
    title: "已有保障",
    description: "补齐五类风险保障现状，已有安排会进入保留项判断。",
    fields: [
      "existing_disease_coverage_wan",
      "existing_medical_responsibility_tier",
      "existing_disability_coverage_wan",
      "existing_care_coverage_wan",
      "existing_death_coverage_wan",
    ],
  },
  {
    key: "long_term",
    title: "长期安排",
    description: "用于财富、养老与传承三个长期维度的测算。",
    fields: [
      "existing_wealth_reserve_wan",
      "existing_retirement_cashflow_yuan_year",
      "existing_legacy_reserve_wan",
    ],
  },
  {
    key: "context",
    title: "本次检视",
    description: "确定检视范围、触发场景与表达边界。",
    fields: ["investment_risk_tolerance", "review_scope", "trigger_ids"],
  },
] as const;

const JOURNEY_CHAPTERS = [
  {
    key: "evidence",
    label: "资料汇集",
    caption: "客户、档案与保单证据",
    stepKeys: ["customer_data", "policy_data", "evidence_gate"],
  },
  {
    key: "diagnosis",
    label: "保障诊断",
    caption: "八维测算与诊断内核",
    stepKeys: ["calculation", "kernel", "diagnosis"],
  },
  {
    key: "review",
    label: "顾问复核",
    caption: "内部报告与人工闸门",
    stepKeys: ["internal_report", "agent_review"],
  },
  {
    key: "delivery",
    label: "客户交付",
    caption: "客户版报告生成与校验",
    stepKeys: ["customer_report"],
  },
] as const;

function normalizeError(cause: unknown, fallback: string) {
  if (!(cause instanceof Error)) return new Error(fallback);
  if (!cause.message.trim() || cause.message === "Internal Server Error") {
    return new Error(fallback);
  }
  return cause;
}

function errorFromResponseBody(body: string, fallback: string) {
  if (!body || body === "Internal Server Error") return new Error(fallback);
  try {
    const parsed = JSON.parse(body) as {
      detail?: unknown;
      message?: unknown;
    };
    const detail =
      typeof parsed.detail === "string"
        ? parsed.detail
        : typeof parsed.message === "string"
          ? parsed.message
          : null;
    return new Error(detail ?? body);
  } catch {
    return new Error(body);
  }
}

async function responseError(response: Response, fallback: string) {
  return errorFromResponseBody((await response.text()).trim(), fallback);
}

async function fetchTaskEnvelope(taskId: string) {
  const response = await fetchWithAuth(`/api/insurance/tasks/${taskId}/ui`);
  if (!response.ok) {
    throw await responseError(response, "读取保障检视任务状态失败");
  }
  return (await response.json()) as CoverageReviewEnvelope;
}

async function mutateTaskAndRefresh({
  taskId,
  url,
  body,
  fallbackError,
}: {
  taskId: string;
  url: string;
  body: Record<string, unknown>;
  fallbackError: string;
}) {
  let recoverableError: Error | null = null;
  let response: Response | null = null;

  try {
    response = await fetchWithAuth(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    recoverableError = normalizeError(cause, fallbackError);
  }

  if (response && !response.ok) {
    const responseBody = (await response.text()).trim();
    const error = errorFromResponseBody(responseBody, fallbackError);
    const isGenericProxyFailure =
      response.status >= 500 &&
      (!responseBody || responseBody === "Internal Server Error");
    if (!isGenericProxyFailure) throw error;
    recoverableError = error;
  }

  try {
    // Workflow state is persisted wave by wave. If the proxy disconnects after
    // persistence, this read recovers the authoritative state instead of
    // showing a false failure to the agent.
    return await fetchTaskEnvelope(taskId);
  } catch (cause) {
    throw recoverableError ?? normalizeError(cause, fallbackError);
  }
}

async function advanceTaskAndRefresh(
  taskId: string,
  body: Record<string, unknown> = {},
) {
  return mutateTaskAndRefresh({
    taskId,
    url: `/api/insurance/tasks/${taskId}/advance`,
    body,
    fallbackError: "保障检视推进连接中断，请点击“继续推进”重试。",
  });
}

async function runTaskUntilStable(
  taskId: string,
  onProgress?: (envelope: CoverageReviewEnvelope) => void,
) {
  for (let attempt = 0; attempt < AUTO_ADVANCE_MAX_ATTEMPTS; attempt += 1) {
    const next = await advanceTaskAndRefresh(taskId);
    onProgress?.(next);
    if (next.status !== "running") return next;
    await new Promise((resolve) =>
      window.setTimeout(resolve, AUTO_ADVANCE_RETRY_MS),
    );
  }
  throw new Error("保障检视推进超时，请点击“继续推进”重试。");
}

function advanceTaskUntilStable(
  taskId: string,
  onProgress?: (envelope: CoverageReviewEnvelope) => void,
) {
  const existing = taskAdvanceRequests.get(taskId);
  if (existing) {
    return existing.then((next) => {
      onProgress?.(next);
      return next;
    });
  }
  const request = runTaskUntilStable(taskId, onProgress).finally(() => {
    if (taskAdvanceRequests.get(taskId) === request) {
      taskAdvanceRequests.delete(taskId);
    }
  });
  taskAdvanceRequests.set(taskId, request);
  return request;
}

function markCurrentProgressRunning(steps: WorkflowProgressStep[] = []) {
  const activeIndex = steps.findIndex((step) =>
    ["waiting", "pending"].includes(step.status),
  );
  if (activeIndex < 0) return steps;
  return steps.map((step, index) =>
    index === activeIndex ? { ...step, status: "running" as const } : step,
  );
}

function progressAfterReviewAction(
  steps: WorkflowProgressStep[] = [],
  action: "approve" | ReviewFeedbackAction,
) {
  const startKey =
    action === "revise_facts"
      ? "calculation"
      : action === "revise_narrative"
        ? "internal_report"
        : "customer_report";
  const startIndex = steps.findIndex((step) => step.key === startKey);
  if (startIndex < 0) return markCurrentProgressRunning(steps);
  return steps.map((step, index) => ({
    ...step,
    status:
      index < startIndex
        ? ("completed" as const)
        : index === startIndex
          ? ("running" as const)
          : ("pending" as const),
  }));
}

function reviewProgressMessage(action: "approve" | ReviewFeedbackAction) {
  if (action === "revise_facts") {
    return "事实修正已提交，正在重新测算八维保障并更新对内诊断。";
  }
  if (action === "revise_narrative") {
    return "表达意见已提交，正在保持诊断数字不变并重新生成对内报告。";
  }
  if (action === "approve") {
    return "代理人复核已确认，正在生成并校验客户版 HTML 报告。";
  }
  return "正在记录本次复核结论。";
}

export function isCoverageReviewEnvelope(
  value: unknown,
): value is CoverageReviewEnvelope {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { kind?: unknown }).kind === "insurance_coverage_review"
  );
}

export function CoverageReviewTask({
  initialEnvelope,
  autoAdvance = false,
}: {
  initialEnvelope: CoverageReviewEnvelope;
  autoAdvance?: boolean;
}) {
  const [envelope, setEnvelope] = useState(initialEnvelope);
  const [dialogOpen, setDialogOpen] = useState(
    !initialEnvelope.thread_id &&
      ["intake_required", "waiting_input"].includes(initialEnvelope.status),
  );
  const [values, setValues] = useState<FormValues>(() =>
    initialValues(initialEnvelope.form_fields),
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reviewDialogOpen, setReviewDialogOpen] = useState(false);
  const [reviewAction, setReviewAction] =
    useState<ReviewFeedbackAction>("revise_facts");
  const [reviewNote, setReviewNote] = useState("");
  const [autoAdvancing, setAutoAdvancing] = useState(false);
  const [statusHydrated, setStatusHydrated] = useState(false);
  const advanceInFlight = useRef(false);
  const phase = phaseContent(envelope.status);
  const progress = progressSummary(envelope);
  const canSubmit = useMemo(
    () =>
      envelope.form_fields.every((field) => {
        if (!field.required) return true;
        const value = values[field.path];
        return Array.isArray(value) ? value.length > 0 : Boolean(value?.trim());
      }),
    [envelope.form_fields, values],
  );

  const applyEnvelope = useCallback(
    (next: CoverageReviewEnvelope, openInputDialog = true) => {
      setEnvelope(next);
      setValues(initialValues(next.form_fields));
      setDialogOpen(
        openInputDialog &&
          ["intake_required", "waiting_input"].includes(next.status),
      );
    },
    [],
  );

  useEffect(() => {
    setStatusHydrated(false);
    const threadId = initialEnvelope.thread_id;
    const taskId = initialEnvelope.task_id;
    const shouldResolveLatest =
      !taskId &&
      Boolean(threadId) &&
      autoAdvance &&
      initialEnvelope.status === "running";
    if (!taskId && !shouldResolveLatest) {
      setStatusHydrated(true);
      return;
    }
    let active = true;

    void (async () => {
      try {
        const statusUrl = taskId
          ? `/api/insurance/tasks/${taskId}/ui`
          : `/api/insurance/coverage-reviews/latest?thread_id=${encodeURIComponent(threadId ?? "")}`;
        const response = await fetchWithAuth(statusUrl);
        if (response.status === 404 || !response.ok) {
          if (active) {
            setDialogOpen(
              ["intake_required", "waiting_input"].includes(
                initialEnvelope.status,
              ),
            );
          }
          return;
        }
        const next = (await response.json()) as CoverageReviewEnvelope;
        if (!active) return;
        applyEnvelope(next);
      } catch {
        // 初次补录尚未创建任务时，继续使用 Tool 返回的本地任务卡。
        if (active) {
          setDialogOpen(
            ["intake_required", "waiting_input"].includes(
              initialEnvelope.status,
            ),
          );
        }
      } finally {
        if (active) setStatusHydrated(true);
      }
    })();

    return () => {
      active = false;
    };
  }, [
    applyEnvelope,
    autoAdvance,
    initialEnvelope.status,
    initialEnvelope.task_id,
    initialEnvelope.thread_id,
  ]);

  const continueRunningTask = useCallback(async () => {
    const taskId = envelope.task_id;
    if (!taskId || advanceInFlight.current) return;
    const previousEnvelope = envelope;
    advanceInFlight.current = true;
    setAutoAdvancing(true);
    setError(null);
    setDialogOpen(false);
    if (envelope.status === "failed") {
      setEnvelope((current) => ({
        ...current,
        status: "running",
        message: "正在重新生成受约束报告内容。",
      }));
    }
    try {
      const next = await advanceTaskUntilStable(taskId, (progressEnvelope) =>
        applyEnvelope(progressEnvelope, false),
      );
      applyEnvelope(next);
    } catch (cause) {
      setEnvelope(previousEnvelope);
      setError(cause instanceof Error ? cause.message : "推进失败，请稍后重试");
    } finally {
      advanceInFlight.current = false;
      setAutoAdvancing(false);
    }
  }, [applyEnvelope, envelope]);

  useEffect(() => {
    if (!statusHydrated || !autoAdvance || envelope.status !== "running") {
      return;
    }
    void continueRunningTask();
  }, [autoAdvance, continueRunningTask, envelope.status, statusHydrated]);

  const submitForm = async () => {
    const submittedEnvelope = envelope;
    setSubmitting(true);
    setError(null);
    setDialogOpen(false);
    try {
      if (envelope.status === "intake_required") {
        setEnvelope((current) => ({
          ...current,
          status: "running",
          message: "正在查询客户中心、客户档案和保单信息。",
          form_fields: [],
          progress_steps: markCurrentProgressRunning(current.progress_steps),
        }));
        const body: Record<string, unknown> = {
          ...(envelope.intake ?? {}),
          thread_id: envelope.thread_id,
        };
        for (const field of envelope.form_fields) {
          body[field.path.slice(1)] = values[field.path];
        }
        const response = await fetchWithAuth(
          "/api/insurance/coverage-reviews/intake",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        if (!response.ok) throw new Error(await response.text());
        const next = (await response.json()) as CoverageReviewEnvelope;
        applyEnvelope(next);
        return;
      }
      if (!envelope.task_id) throw new Error("保障检视任务 ID 缺失");
      advanceInFlight.current = true;
      setAutoAdvancing(true);
      setEnvelope((current) => ({
        ...current,
        status: "running",
        message: "补录信息已提交，正在执行八维测算并生成对内诊断。",
        form_fields: [],
        progress_steps: markCurrentProgressRunning(current.progress_steps),
      }));
      const answers: Record<string, string | string[]> = {};
      let triggerIds: string[] | undefined;
      for (const field of envelope.form_fields) {
        const key = field.path.split("/").at(-1);
        if (!key) continue;
        const selected = values[field.path] ?? "";
        if (key === "trigger_ids") {
          triggerIds = Array.isArray(selected)
            ? selected
            : selected
              ? [selected]
              : [];
        } else {
          answers[key] = selected;
        }
      }
      let next = await advanceTaskAndRefresh(envelope.task_id, {
        answers,
        trigger_ids: triggerIds,
      });
      applyEnvelope(next, false);
      if (next.status === "running") {
        next = await advanceTaskUntilStable(
          envelope.task_id,
          (progressEnvelope) => applyEnvelope(progressEnvelope, false),
        );
        applyEnvelope(next, false);
      }
    } catch (cause) {
      setEnvelope(submittedEnvelope);
      setError(normalizeError(cause, "提交失败，请稍后重试").message);
      setDialogOpen(true);
    } finally {
      advanceInFlight.current = false;
      setAutoAdvancing(false);
      setSubmitting(false);
    }
  };

  const submitReview = async (
    action: "approve" | ReviewFeedbackAction,
    note: string,
  ) => {
    if (!envelope.task_id || !envelope.kernel_hash) return;
    const submittedEnvelope = envelope;
    setSubmitting(true);
    setError(null);
    if (action !== "reject") {
      setReviewDialogOpen(false);
      setEnvelope((current) => ({
        ...current,
        status: "running",
        message: reviewProgressMessage(action),
        review_packet: null,
        progress_steps: progressAfterReviewAction(
          current.progress_steps,
          action,
        ),
      }));
    }
    try {
      let next = await mutateTaskAndRefresh({
        taskId: envelope.task_id,
        url: `/api/insurance/tasks/${envelope.task_id}/coverage-review-feedback`,
        body: {
          action,
          expected_review_revision: envelope.review_revision,
          expected_kernel_hash: envelope.kernel_hash,
          expected_profile_version: envelope.profile_version,
          note,
        },
        fallbackError: "复核提交连接中断，请刷新任务状态后重试。",
      });
      applyEnvelope(next, false);
      if (next.status === "running") {
        next = await advanceTaskUntilStable(
          envelope.task_id,
          (progressEnvelope) => applyEnvelope(progressEnvelope, false),
        );
      }
      applyEnvelope(next);
      setReviewDialogOpen(false);
      setReviewNote("");
    } catch (cause) {
      setEnvelope(submittedEnvelope);
      if (action !== "approve" && action !== "reject") {
        setReviewDialogOpen(true);
      }
      setError(normalizeError(cause, "复核失败，请稍后重试").message);
    } finally {
      setSubmitting(false);
    }
  };

  const openReviewDialog = (action: ReviewFeedbackAction) => {
    setReviewAction(action);
    setReviewNote("");
    setReviewDialogOpen(true);
  };

  const groupedFormFields = useMemo(
    () => groupFormFields(envelope.form_fields),
    [envelope.form_fields],
  );

  return (
    <div className="bg-background my-1 w-full overflow-hidden">
      <div className="bg-muted/20 border-b px-5 py-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 gap-3.5">
            <div className="bg-foreground text-background flex size-10 shrink-0 items-center justify-center rounded-md">
              <ClipboardCheckIcon className="size-5" />
            </div>
            <div className="min-w-0">
              <div className="text-muted-foreground flex items-center gap-1.5 text-[11px] font-medium uppercase">
                <SparklesIcon className="size-3.5" />
                保障顾问工作台
              </div>
              <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h3 className="text-base font-semibold">家庭保障检视</h3>
                <span className="text-primary text-xs font-medium">
                  {progress}
                </span>
              </div>
              <p className="text-muted-foreground mt-1.5 max-w-3xl text-sm leading-6">
                {envelope.message}
              </p>
            </div>
          </div>
          <StatusLabel status={envelope.status} />
        </div>
      </div>

      <div className="px-5 pb-5">
        {error && (
          <div className="border-destructive/30 bg-destructive/5 text-destructive mt-4 flex items-start gap-2 rounded-md border px-3 py-2.5 text-sm">
            <AlertCircleIcon className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {(envelope.progress_steps?.length ?? 0) > 0 && (
          <WorkflowJourney steps={envelope.progress_steps ?? []} />
        )}

        {envelope.status === "waiting_confirmation" &&
          envelope.review_packet && (
            <ReviewSummary packet={envelope.review_packet} />
          )}

        {Boolean(
          envelope.internal_report_url ?? envelope.customer_report_url,
        ) && (
          <ReportDelivery
            status={envelope.status}
            internalUrl={envelope.internal_report_url}
            customerUrl={envelope.customer_report_url}
          />
        )}

        <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t pt-4">
          <div className="flex flex-wrap gap-2">
            {["intake_required", "waiting_input"].includes(envelope.status) && (
              <Button onClick={() => setDialogOpen(true)}>
                补齐关键信息
                <ArrowRightIcon />
              </Button>
            )}
            {envelope.status === "running" && (
              <Button
                variant="outline"
                disabled={autoAdvancing}
                onClick={() => void continueRunningTask()}
              >
                {autoAdvancing ? (
                  <LoaderCircleIcon className="animate-spin" />
                ) : (
                  <RefreshCwIcon />
                )}
                {autoAdvancing ? "正在协调各环节" : "同步最新进度"}
              </Button>
            )}
            {envelope.status === "failed" && envelope.can_retry && (
              <Button
                disabled={autoAdvancing}
                onClick={() => void continueRunningTask()}
              >
                {autoAdvancing ? (
                  <LoaderCircleIcon className="animate-spin" />
                ) : (
                  <RefreshCwIcon />
                )}
                {autoAdvancing ? "正在继续生成" : "从中断处继续生成"}
              </Button>
            )}
            {envelope.status === "waiting_confirmation" && (
              <>
                <Button
                  variant="outline"
                  disabled={submitting}
                  onClick={() => openReviewDialog("revise_facts")}
                >
                  修正客户事实
                </Button>
                <Button
                  variant="outline"
                  disabled={submitting}
                  onClick={() => openReviewDialog("revise_narrative")}
                >
                  调整沟通表达
                </Button>
                <Button
                  variant="ghost"
                  disabled={submitting}
                  onClick={() => openReviewDialog("reject")}
                >
                  退回本次检视
                </Button>
              </>
            )}
          </div>

          {envelope.status === "waiting_confirmation" && (
            <Button
              disabled={submitting}
              onClick={() =>
                submitReview(
                  "approve",
                  "代理人已复核客户事实、八维数字、保留项、行动次序和对内报告。",
                )
              }
            >
              <BadgeCheckIcon />
              {submitting ? "正在生成客户版" : "确认诊断并生成客户版"}
            </Button>
          )}
        </div>
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="flex max-h-[90vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-4xl">
          <DialogHeader className="bg-muted/20 shrink-0 border-b px-6 py-5 text-left">
            <div className="text-muted-foreground flex items-center gap-1.5 text-xs font-medium">
              <ShieldCheckIcon className="size-4" />
              信息仅用于本次保障检视
            </div>
            <DialogTitle className="mt-1.5 text-xl">
              {phase.dialogTitle}
            </DialogTitle>
            <DialogDescription className="max-w-2xl leading-6">
              {phase.dialogDescription}
            </DialogDescription>
          </DialogHeader>

          <div className="min-h-0 flex-1 overflow-y-auto px-6">
            {groupedFormFields.map((group) => (
              <section key={group.key} className="border-b py-5 last:border-0">
                <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
                  <div>
                    <h4 className="text-sm font-semibold">{group.title}</h4>
                    <p className="text-muted-foreground mt-1 text-xs leading-5">
                      {group.description}
                    </p>
                  </div>
                  <span className="text-muted-foreground text-xs">
                    {group.fields.length} 项
                  </span>
                </div>
                <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2">
                  {group.fields.map((field) => (
                    <FormFieldControl
                      key={field.path}
                      field={field}
                      value={values[field.path] ?? ""}
                      onChange={(value) =>
                        setValues((current) => ({
                          ...current,
                          [field.path]: value,
                        }))
                      }
                    />
                  ))}
                </div>
              </section>
            ))}
          </div>

          {error && (
            <p className="text-destructive shrink-0 border-t px-6 pt-3 text-sm">
              {error}
            </p>
          )}
          <DialogFooter className="bg-background shrink-0 border-t px-6 py-4">
            <Button variant="ghost" onClick={() => setDialogOpen(false)}>
              稍后处理
            </Button>
            <Button
              data-testid="coverage-profile-submit"
              disabled={!canSubmit || submitting}
              onClick={submitForm}
            >
              {submitting ? phase.submittingLabel : phase.submitLabel}
              {!submitting && <ArrowRightIcon />}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={reviewDialogOpen} onOpenChange={setReviewDialogOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{reviewDialogTitle(reviewAction)}</DialogTitle>
            <DialogDescription>
              {reviewDialogDescription(reviewAction)}
            </DialogDescription>
          </DialogHeader>
          <Textarea
            value={reviewNote}
            onChange={(event) => setReviewNote(event.target.value)}
            placeholder={reviewDialogPlaceholder(reviewAction)}
            className="min-h-32"
          />
          {error && <p className="text-destructive text-sm">{error}</p>}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setReviewDialogOpen(false)}
            >
              取消
            </Button>
            <Button
              variant={reviewAction === "reject" ? "destructive" : "default"}
              disabled={!reviewNote.trim() || submitting}
              onClick={() => submitReview(reviewAction, reviewNote)}
            >
              {submitting ? "正在提交..." : reviewSubmitLabel(reviewAction)}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function WorkflowJourney({ steps }: { steps: WorkflowProgressStep[] }) {
  const chapters = JOURNEY_CHAPTERS.map((chapter) => {
    const chapterSteps = steps.filter((step) =>
      chapter.stepKeys.some((key) => key === step.key),
    );
    return {
      ...chapter,
      status: aggregateProgressStatus(chapterSteps.map((step) => step.status)),
    };
  });
  const activeStep =
    steps.find((step) => step.status === "failed") ??
    steps.find((step) => step.status === "running") ??
    steps.find((step) => step.status === "waiting") ??
    steps.find((step) => step.status === "pending");
  const completed = steps.filter((step) => step.status === "completed").length;
  const activeIndex = activeStep ? steps.indexOf(activeStep) : -1;
  const lastCompleted = steps
    .slice(0, activeIndex >= 0 ? activeIndex : steps.length)
    .reverse()
    .find((step) => step.status === "completed");
  const nextStep =
    activeIndex >= 0
      ? steps.slice(activeIndex + 1).find((step) => step.status !== "completed")
      : undefined;
  const currentHint = journeyStatusHint(activeStep?.status);

  return (
    <section className="mt-5 border-y py-4">
      <div
        className="flex flex-wrap items-center justify-between gap-2"
        aria-live="polite"
      >
        <div className="flex items-center gap-2 text-sm font-medium">
          {activeStep?.status === "failed" ? (
            <AlertCircleIcon className="text-destructive size-4" />
          ) : activeStep ? (
            <LoaderCircleIcon className="text-primary size-4 animate-spin" />
          ) : (
            <CheckCircle2Icon className="text-primary size-4" />
          )}
          <span>{activeStep?.label ?? "全部业务环节已完成"}</span>
        </div>
        <span className="text-muted-foreground text-xs tabular-nums">
          {completed}/{steps.length} 个环节完成
        </span>
      </div>

      <div className="mt-4 grid divide-y border-y sm:grid-cols-3 sm:divide-x sm:divide-y-0">
        <JourneyMoment
          label="刚刚完成"
          value={lastCompleted?.label ?? "客户信息已进入检视流程"}
          detail={
            lastCompleted ? "结果已写入本次任务状态" : "正在建立任务上下文"
          }
        />
        <JourneyMoment
          label="当前环节"
          value={activeStep?.label ?? "全部业务环节已完成"}
          detail={currentHint}
          emphasis
        />
        <JourneyMoment
          label="接下来"
          value={nextStep?.label ?? "查看并交付两份报告"}
          detail={
            nextStep ? "当前环节完成后自动进入" : "内外报告均已通过交付校验"
          }
        />
      </div>

      <ol className="mt-4 grid grid-cols-2 gap-x-4 gap-y-4 lg:grid-cols-4">
        {chapters.map((chapter, index) => (
          <li key={chapter.key} className="relative min-w-0">
            <div className="flex items-center gap-2">
              <span
                className={`flex size-6 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold ${chapterTone(
                  chapter.status,
                )}`}
              >
                {chapter.status === "completed" ? (
                  <CheckCircle2Icon className="size-3.5" />
                ) : (
                  index + 1
                )}
              </span>
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">
                  {chapter.label}
                </div>
                <div className="text-muted-foreground mt-0.5 truncate text-[11px]">
                  {chapter.caption}
                </div>
              </div>
              {index < chapters.length - 1 && (
                <ChevronRightIcon className="text-muted-foreground/40 ml-auto hidden size-4 lg:block" />
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function JourneyMoment({
  label,
  value,
  detail,
  emphasis = false,
}: {
  label: string;
  value: string;
  detail: string;
  emphasis?: boolean;
}) {
  return (
    <div className="min-w-0 py-3 sm:px-4 sm:first:pl-0 sm:last:pr-0">
      <div className="text-muted-foreground text-[11px] font-medium">
        {label}
      </div>
      <div
        className={`mt-1 truncate text-sm font-medium ${emphasis ? "text-foreground" : "text-muted-foreground"}`}
      >
        {value}
      </div>
      <div className="text-muted-foreground mt-1 text-xs leading-5">
        {detail}
      </div>
    </div>
  );
}

function journeyStatusHint(status?: WorkflowProgressStep["status"]) {
  if (status === "failed") return "已完成事实均保留，可从中断处继续";
  if (status === "waiting") return "需要代理人确认后继续";
  if (status === "running") return "系统正在自动处理，无需重复点击";
  if (status === "pending") return "即将自动开始";
  return "报告已经完成并通过交付校验";
}

function aggregateProgressStatus(
  statuses: WorkflowProgressStep["status"][],
): WorkflowProgressStep["status"] {
  if (statuses.some((status) => status === "failed")) return "failed";
  if (statuses.some((status) => status === "waiting")) return "waiting";
  if (statuses.some((status) => status === "running")) return "running";
  if (
    statuses.length > 0 &&
    statuses.every((status) => status === "completed")
  ) {
    return "completed";
  }
  if (statuses.some((status) => status === "completed")) return "running";
  return "pending";
}

function chapterTone(status: WorkflowProgressStep["status"]) {
  if (status === "completed") {
    return "border-primary bg-primary text-primary-foreground";
  }
  if (status === "failed") {
    return "border-destructive bg-destructive/10 text-destructive";
  }
  if (status === "running" || status === "waiting") {
    return "border-foreground bg-foreground text-background";
  }
  return "border-border bg-background text-muted-foreground";
}

function ReportDelivery({
  status,
  internalUrl,
  customerUrl,
}: {
  status: CoverageReviewEnvelope["status"];
  internalUrl?: string | null;
  customerUrl?: string | null;
}) {
  return (
    <section className="mt-6">
      <div className="flex items-center gap-2">
        <FileCheck2Icon className="text-primary size-4" />
        <h4 className="text-sm font-semibold">报告交付</h4>
        <span className="text-muted-foreground text-xs">
          两份报告共用同一诊断内核
        </span>
      </div>
      <div className="mt-3 grid border-y sm:grid-cols-2 sm:divide-x">
        <div className="flex items-center gap-3 py-4 pr-4">
          <FileTextIcon className="text-muted-foreground size-5 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium">代理人内部诊断</div>
            <div className="text-muted-foreground mt-0.5 text-xs">
              测算依据、沟通策略与复核要点
            </div>
          </div>
          {internalUrl ? (
            <Button variant="outline" size="sm" asChild>
              <a href={internalUrl} target="_blank" rel="noreferrer">
                查看
                <ExternalLinkIcon />
              </a>
            </Button>
          ) : (
            <span className="text-muted-foreground text-xs">生成中</span>
          )}
        </div>
        <div className="flex items-center gap-3 py-4 sm:pl-4">
          <ShieldCheckIcon className="text-muted-foreground size-5 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium">客户家庭保障报告</div>
            <div className="text-muted-foreground mt-0.5 text-xs">
              {status === "waiting_confirmation"
                ? "完成代理人复核后解锁"
                : "客户可读的 HTML 交付版本"}
            </div>
          </div>
          {customerUrl ? (
            <Button size="sm" asChild>
              <a href={customerUrl} target="_blank" rel="noreferrer">
                打开
                <ExternalLinkIcon />
              </a>
            </Button>
          ) : (
            <span className="text-muted-foreground text-xs">待复核</span>
          )}
        </div>
      </div>
    </section>
  );
}

function ReviewSummary({ packet }: { packet: ReviewPacket }) {
  const priorityFacts = [...packet.dimension_facts]
    .filter(
      (fact) => fact.priority_rank !== null && fact.priority_rank !== undefined,
    )
    .sort(
      (left, right) =>
        Number(left.priority_rank ?? 99) - Number(right.priority_rank ?? 99),
    )
    .slice(0, 3);
  const gapCount = packet.dimension_facts.filter((fact) =>
    ["mild_gap", "significant_gap", "severe_gap"].includes(fact.state),
  ).length;
  const noNeedCount = packet.dimension_facts.filter(
    (fact) => fact.state === "no_need",
  ).length;

  return (
    <section className="mt-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <div className="text-muted-foreground text-[11px] font-medium uppercase">
            Advisor review
          </div>
          <h4 className="mt-1 text-base font-semibold">代理人复核摘要</h4>
        </div>
        <span className="text-muted-foreground text-xs">
          请确认事实、数字、保留项与行动次序
        </span>
      </div>

      <div className="mt-4 grid grid-cols-2 border-y sm:grid-cols-4 sm:divide-x">
        <ReviewMetric label="八维缺口" value={`${gapCount} 项`} />
        <ReviewMetric label="优先行动" value={`${priorityFacts.length} 项`} />
        <ReviewMetric
          label="明确保留"
          value={`${packet.preserve_items.length} 项`}
        />
        <ReviewMetric label="当前无需" value={`${noNeedCount} 项`} />
      </div>

      <div className="mt-5 grid gap-6 lg:grid-cols-2">
        <div>
          <div className="flex items-center gap-2 text-sm font-medium">
            <TargetIcon className="text-primary size-4" />
            本次优先关注
          </div>
          <ol className="mt-3 space-y-3">
            {priorityFacts.map((fact, index) => (
              <li key={fact.dimension_code} className="flex items-start gap-3">
                <span className="bg-foreground text-background flex size-6 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold">
                  {index + 1}
                </span>
                <div className="min-w-0">
                  <div className="text-sm font-medium">
                    {fact.dimension_name}
                    <span className="text-muted-foreground ml-2 text-xs font-normal">
                      {dimensionStateLabel(fact.state)}
                    </span>
                  </div>
                  <div className="text-muted-foreground mt-0.5 text-xs">
                    缺口 {fact.gap_display}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </div>

        <div className="border-t pt-4 lg:border-t-0 lg:border-l lg:pt-0 lg:pl-6">
          <div className="text-sm font-medium">客户与检视场景</div>
          <p className="text-muted-foreground mt-1.5 text-sm leading-6">
            {[...packet.customer_identity, ...packet.trigger_context].join(
              " · ",
            )}
          </p>
        </div>
      </div>

      <div className="mt-5 overflow-x-auto border-t pt-4">
        <table className="w-full min-w-[620px] table-fixed border-collapse text-sm">
          <thead className="text-muted-foreground border-b text-left text-xs">
            <tr>
              <th className="w-[17%] pb-2 font-medium">维度</th>
              <th className="w-[17%] pb-2 font-medium">状态</th>
              <th className="w-[17%] pb-2 font-medium">已有</th>
              <th className="w-[17%] pb-2 font-medium">目标</th>
              <th className="w-[20%] pb-2 font-medium">缺口</th>
              <th className="w-[12%] pb-2 text-right font-medium">次序</th>
            </tr>
          </thead>
          <tbody>
            {packet.dimension_facts.map((fact) => (
              <tr key={fact.dimension_code} className="border-t">
                <td className="py-2.5 font-medium">{fact.dimension_name}</td>
                <td className="py-2.5">{dimensionStateLabel(fact.state)}</td>
                <td className="py-2.5">{fact.existing_display}</td>
                <td className="py-2.5">{fact.ideal_display}</td>
                <td className="py-2.5">{fact.gap_display}</td>
                <td className="py-2.5 text-right tabular-nums">
                  {fact.priority_rank ?? "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-5 grid gap-4 border-t pt-4 text-sm lg:grid-cols-2">
        <div>
          <div className="flex items-center gap-2 font-medium">
            <BadgeCheckIcon className="text-primary size-4" />
            保留与修正
          </div>
          <ul className="text-muted-foreground mt-2 space-y-1.5 leading-5">
            {[...packet.preserve_items, ...packet.correction_items].map(
              (item) => (
                <li key={item}>{item}</li>
              ),
            )}
            {packet.preserve_items.length === 0 &&
              packet.correction_items.length === 0 && <li>暂无特别事项</li>}
          </ul>
        </div>
        <div>
          <div className="font-medium">辅助观察与待确认</div>
          <ul className="text-muted-foreground mt-2 space-y-1.5 leading-5">
            {[...packet.auxiliary_observations, ...packet.warnings].map(
              (item) => (
                <li key={item}>{item}</li>
              ),
            )}
            {packet.auxiliary_observations.length === 0 &&
              packet.warnings.length === 0 && <li>暂无待确认事项</li>}
          </ul>
        </div>
      </div>
    </section>
  );
}

function ReviewMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-3 py-3 first:pl-0 sm:px-5">
      <div className="text-muted-foreground text-xs">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function reviewDialogTitle(action: ReviewFeedbackAction) {
  if (action === "revise_facts") return "修改客户事实";
  if (action === "revise_narrative") return "调整报告表达";
  return "驳回本次检视";
}

function reviewDialogDescription(action: ReviewFeedbackAction) {
  if (action === "revise_facts") {
    return "请用自然语言写明字段、数值和单位。系统会先提取成结构化变更，再重跑测算和报告。";
  }
  if (action === "revise_narrative") {
    return "请说明称呼、解释密度或话术方向。这类修改不会重算八维数字。";
  }
  return "驳回后不会生成对客报告，驳回原因会进入审计记录。";
}

function reviewDialogPlaceholder(action: ReviewFeedbackAction) {
  if (action === "revise_facts") {
    return "例如：她的家庭月支出不是 2 万，是 3 万；配偶年收入是 25 万。";
  }
  if (action === "revise_narrative") {
    return "例如：称呼改为王总，解释简洁一些，优先从家庭责任切入。";
  }
  return "请写明驳回原因。";
}

function reviewSubmitLabel(action: ReviewFeedbackAction) {
  if (action === "revise_facts") return "提交并重新测算";
  if (action === "revise_narrative") return "提交并重新生成报告";
  return "确认驳回";
}

function dimensionStateLabel(state: string) {
  return (
    {
      sufficient: "充足",
      mild_gap: "轻度不足",
      significant_gap: "显著不足",
      severe_gap: "严重缺失",
      no_need: "当前无需",
      unknown: "待确认",
    }[state] ?? state
  );
}

function phaseContent(status: CoverageReviewEnvelope["status"]) {
  if (status === "intake_required") {
    return {
      progress: "步骤 1/4 · 客户基础信息",
      dialogTitle: "确认客户身份",
      dialogDescription:
        "这些信息用于准确匹配客户中心、客户档案与保单报告。确认后，系统会自动完成资料汇集。",
      submitLabel: "确认并开始资料汇集",
      submittingLabel: "正在汇集客户资料...",
    };
  }
  if (status === "waiting_input") {
    return {
      progress: "步骤 2/4 · 八维测算信息",
      dialogTitle: "完善保障画像",
      dialogDescription:
        "系统已尽可能合并客户中心、档案与保单报告。请确认仍缺少的关键事实，提交后将进入八维测算与内部诊断。",
      submitLabel: "确认画像并开始诊断",
      submittingLabel: "正在固化保障画像...",
    };
  }
  if (status === "waiting_confirmation") {
    return {
      progress: "步骤 3/4 · 代理人复核",
      dialogTitle: "代理人复核",
      dialogDescription: "请复核对内诊断报告后再生成对客报告。",
      submitLabel: "确认并继续",
      submittingLabel: "正在提交...",
    };
  }
  return {
    progress:
      status === "completed"
        ? "步骤 4/4 · 报告已生成"
        : status === "running"
          ? "步骤 2/4 · 正在生成对内诊断"
          : "保障检视流程",
    dialogTitle: "保障检视任务",
    dialogDescription: "请按任务卡状态继续处理。",
    submitLabel: "确认并继续",
    submittingLabel: "正在提交...",
  };
}

function progressSummary(envelope: CoverageReviewEnvelope) {
  const steps = envelope.progress_steps ?? [];
  if (steps.length === 0) return phaseContent(envelope.status).progress;
  const completed = steps.filter((step) => step.status === "completed").length;
  const current =
    steps.find((step) => step.status === "failed") ??
    steps.find((step) => step.status === "running") ??
    steps.find((step) => step.status === "waiting") ??
    steps.find((step) => step.status === "pending");
  if (!current) return `${steps.length}/${steps.length} · 全部步骤已完成`;
  const prefix =
    current.status === "failed" ? "执行中断" : `${completed}/${steps.length}`;
  return `${prefix} · ${current.label}`;
}

function groupFormFields(fields: FormField[]) {
  const assignedPaths = new Set<string>();
  const groups: Array<{
    key: string;
    title: string;
    description: string;
    fields: FormField[];
  }> = FORM_GROUPS.map((group) => {
    const groupFieldKeys: readonly string[] = group.fields;
    const matched = fields.filter((field) => {
      const key = field.path.split("/").at(-1) ?? "";
      const included = groupFieldKeys.includes(key);
      if (included) assignedPaths.add(field.path);
      return included;
    });
    return { ...group, fields: matched };
  }).filter((group) => group.fields.length > 0);
  const remaining = fields.filter((field) => !assignedPaths.has(field.path));
  if (remaining.length > 0) {
    groups.push({
      key: "other",
      title: "其他确认",
      description: "用于完善本次检视所需的补充信息。",
      fields: remaining,
    });
  }
  return groups;
}

function FormFieldControl({
  field,
  value,
  onChange,
}: {
  field: FormField;
  value: string | string[];
  onChange: (value: string | string[]) => void;
}) {
  return (
    <div className={field.control === "multi_select" ? "sm:col-span-2" : ""}>
      <div className="mb-1.5 flex items-center gap-1.5 text-sm font-medium">
        <span>{field.label}</span>
        {field.unit && (
          <span className="text-muted-foreground text-xs font-normal">
            {field.unit}
          </span>
        )}
        {field.required && (
          <span className="text-primary text-[10px] font-medium">必填</span>
        )}
      </div>

      {field.control === "select" ? (
        <Select value={String(value)} onValueChange={onChange}>
          <SelectTrigger className="w-full">
            <SelectValue placeholder="请选择" />
          </SelectTrigger>
          <SelectContent>
            {field.options.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : field.control === "multi_select" ? (
        <div>
          <div className="text-muted-foreground mb-2 text-xs">
            已选择 {Array.isArray(value) ? value.length : 0} 个场景，可多选
          </div>
          <div className="grid max-h-56 gap-2 overflow-y-auto pr-1 sm:grid-cols-2">
            {field.options.map((option) => {
              const selected = Array.isArray(value) ? value : [];
              const checked = selected.includes(option.value);
              return (
                <label
                  key={option.value}
                  className={`flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2.5 text-sm transition-colors ${
                    checked
                      ? "border-primary/50 bg-primary/5 text-foreground"
                      : "border-border hover:bg-muted/40"
                  }`}
                >
                  <input
                    type="checkbox"
                    className="accent-primary mt-0.5"
                    checked={checked}
                    onChange={(event) =>
                      onChange(
                        event.target.checked
                          ? [...selected, option.value]
                          : selected.filter((item) => item !== option.value),
                      )
                    }
                  />
                  <span className="leading-5">{option.label}</span>
                </label>
              );
            })}
          </div>
        </div>
      ) : (
        <Input
          type={field.control === "number" ? "number" : "text"}
          min={field.control === "number" ? 0 : undefined}
          value={String(value)}
          placeholder="请输入"
          onChange={(event) => onChange(event.target.value)}
        />
      )}

      {field.reason && (
        <p className="text-muted-foreground mt-1.5 text-xs leading-5">
          {field.reason}
        </p>
      )}
    </div>
  );
}

function StatusLabel({ status }: { status: CoverageReviewEnvelope["status"] }) {
  const labels: Record<CoverageReviewEnvelope["status"], string> = {
    intake_required: "待补基础信息",
    waiting_input: "待补测算信息",
    waiting_confirmation: "待代理人复核",
    completed: "已完成",
    suspended: "已暂停",
    cancelled: "已取消",
    failed: "执行失败",
    running: "执行中",
  };
  const tones: Record<CoverageReviewEnvelope["status"], string> = {
    intake_required: "border-amber-500/30 bg-amber-500/10 text-amber-700",
    waiting_input: "border-amber-500/30 bg-amber-500/10 text-amber-700",
    waiting_confirmation: "border-primary/30 bg-primary/10 text-primary",
    completed: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700",
    suspended: "border-border bg-muted text-muted-foreground",
    cancelled: "border-border bg-muted text-muted-foreground",
    failed: "border-destructive/30 bg-destructive/10 text-destructive",
    running: "border-primary/30 bg-primary/10 text-primary",
  };
  return (
    <span
      className={`flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${tones[status]}`}
    >
      <span
        className={`size-1.5 rounded-full bg-current ${status === "running" ? "animate-pulse" : ""}`}
      />
      {labels[status]}
    </span>
  );
}

function initialValues(fields: FormField[]): FormValues {
  return Object.fromEntries(
    fields.map((field) => {
      let value: string | string[] = "";
      if (field.control === "multi_select") {
        value = Array.isArray(field.value) ? field.value.map(String) : [];
      } else if (
        typeof field.value === "string" ||
        typeof field.value === "number"
      ) {
        value = String(field.value);
      }
      return [field.path, value];
    }),
  );
}
