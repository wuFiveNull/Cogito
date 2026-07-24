import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type EventRecord } from "../api";
import { Badge, Empty, ErrorBox, Loading, PageTitle, Section, StatusPill, useAsync } from "../components";

function fmtTime(value: number): string {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function outcomeTone(outcome: string): "ok" | "warn" | "danger" | "info" | "muted" {
  if (outcome === "failed" || outcome === "cancelled" || outcome === "unknown") return "danger";
  if (outcome === "completed" || outcome === "success") return "ok";
  if (outcome === "pending" || outcome === "running") return "warn";
  return "info";
}

function EventRow({ event }: { event: EventRecord }) {
  const attrs = event.attributes;
  const tokens = Number(attrs.input_tokens ?? 0) + Number(attrs.output_tokens ?? 0);
  const latency = attrs.latency_ms;
  return (
    <article className="rounded-xl border border-borderc bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={outcomeTone(event.outcome)}>{event.outcome || event.event_class}</Badge>
        <span className="font-semibold text-primary">{event.event_type}</span>
        <span className="font-mono text-[11px] text-muted">
          {event.stream_type}/{event.stream_id.slice(0, 16)} #{event.stream_version}
        </span>
        <span className="ml-auto text-xs text-muted">{fmtTime(event.occurred_at)}</span>
      </div>
      {event.summary && <p className="mt-2 text-sm text-ink">{event.summary}</p>}
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-muted">
        {event.parent_span_id && <span>parent span: {event.parent_span_id}</span>}
        {event.causation_id && <span>caused by: {event.causation_id}</span>}
        {tokens > 0 && <span>tokens: {tokens}</span>}
        {latency !== undefined && <span>latency: {String(latency)}ms</span>}
        {event.error_category && <span className="text-danger">{event.error_category}</span>}
      </div>
    </article>
  );
}

function SessionList({ onPick }: { onPick: (id: string) => void }) {
  const state = useAsync(() => api.sessions(), []);
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorBox msg={state.error} />;
  const sessions = state.data?.items ?? [];
  if (!sessions.length) return <Empty msg="暂无会话" />;
  return (
    <div className="space-y-2">
      {sessions.map((session) => {
        const id = String(session.session_id);
        return (
          <button
            key={id}
            className="flex w-full items-center gap-3 rounded-xl border border-borderc bg-surface-2 p-4 text-left hover:border-primary/40"
            onClick={() => onPick(id)}
          >
            <StatusPill status={String(session.status ?? "unknown")} />
            <div className="min-w-0 flex-1">
              <div className="font-mono text-sm text-ink">{id}</div>
              <div className="text-xs text-muted">conversation: {String(session.conversation_id ?? "-")}</div>
            </div>
            <div className="text-xs text-muted">{Number(session.turn_count ?? 0)} turns</div>
          </button>
        );
      })}
    </div>
  );
}

function EventTimeline({ sessionId }: { sessionId: string }) {
  const timeline = useAsync(() => api.eventTimeline(sessionId), [sessionId]);
  if (timeline.loading) return <Loading label="加载事件时间线…" />;
  if (timeline.error) return <ErrorBox msg={timeline.error} />;
  const events = timeline.data?.events ?? [];
  const traces = [...new Set(events.map((event) => event.trace_id).filter(Boolean))];
  return (
    <div className="space-y-5">
      <Link to="/trace" className="btn-ghost">← 返回会话</Link>
      <PageTitle title="Event Timeline" desc={`session: ${sessionId}`} />
      <Section title="因果链" subtitle="所有可见状态均由不可变 Event 重放；不读取 Trace/Span 表。">
        {traces.length ? (
          <div className="flex flex-wrap gap-2">
            {traces.map((traceId) => <span key={traceId} className="pill bg-accent/20 font-mono text-primary-strong">trace: {traceId}</span>)}
          </div>
        ) : <Empty msg="该会话暂无 Event；旧历史须先完成快照导入。" />}
      </Section>
      {events.length > 0 && (
        <Section title="事件时间线" subtitle={`${events.length} 个事件，按发生时间排序`}>
          <div className="space-y-2">{events.map((event) => <EventRow key={event.event_id} event={event} />)}</div>
        </Section>
      )}
    </div>
  );
}

export default function TracePage() {
  const { id } = useParams<{ id?: string }>();
  const [picked, setPicked] = useState<string | null>(null);
  const sessionId = id ?? picked;
  const pick = useCallback((value: string) => setPicked(value), []);
  if (sessionId) return <EventTimeline sessionId={sessionId} />;
  return (
    <div className="space-y-5">
      <PageTitle title="Event Explorer" desc="选择会话，查看统一事件时间线与因果链。" />
      <SessionList onPick={pick} />
    </div>
  );
}
