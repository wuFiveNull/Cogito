import { Link, NavLink, Route, Routes } from "react-router-dom";
import Overview from "./pages/Overview";
import { ResourceList, type ResourceListConfig } from "./pages/ResourceList";
import { api } from "./api";
import MemoryPage from "./pages/Memory";
import ConnectorsPage from "./pages/Connectors";
import ChannelsPage from "./pages/Channels";
import CommandsPage from "./pages/Commands";
import PluginsPage from "./pages/Plugins";
import ChatPage from "./pages/Chat";
import DeliveriesPage from "./pages/Deliveries";
import TracePage from "./pages/Trace";
import { MockBanner, StatusPill } from "./components";

// ── 图标（内联 SVG，无外部依赖） ─────────────────────────────
type IconName =
  | "overview"
  | "chat"
  | "runs"
  | "tasks"
  | "memory"
  | "connectors"
  | "channels"
  | "trace"
  | "deliveries"
  | "commands"
  | "plugins";

function NavIcon({ name }: { name: IconName }) {
  const common = {
    width: 18,
    height: 18,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  switch (name) {
    case "overview":
      return (
        <svg {...common}>
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="7" height="7" rx="1.5" />
        </svg>
      );
    case "chat":
      return (
        <svg {...common}>
          <path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.6-.8L3 21l1.9-5.4A8.5 8.5 0 1 1 21 11.5z" />
        </svg>
      );
    case "runs":
      return (
        <svg {...common}>
          <path d="M3 12h4l3 8 4-16 3 8h4" />
        </svg>
      );
    case "tasks":
      return (
        <svg {...common}>
          <path d="M9 11l3 3L22 4" />
          <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
        </svg>
      );
    case "memory":
      return (
        <svg {...common}>
          <path d="M9 18h6" />
          <path d="M10 22h4" />
          <path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1V18h6v-1.2c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2z" />
        </svg>
      );
    case "connectors":
      return (
        <svg {...common}>
          <path d="M9 2v6M15 16v6" />
          <path d="M6 8h12v8H6z" />
          <path d="M12 8V2M12 16v6" />
        </svg>
      );
    case "channels":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="2" />
          <path d="M16.2 7.8a6 6 0 0 1 0 8.4M7.8 16.2a6 6 0 0 1 0-8.4" />
          <path d="M19 5a9 9 0 0 1 0 14M5 19a9 9 0 0 1 0-14" />
        </svg>
      );
    case "deliveries":
      return (
        <svg {...common}>
          <path d="M1 3h13v13H1z" />
          <path d="M14 8h4l3 3v5h-7z" />
          <circle cx="5.5" cy="18.5" r="1.8" />
          <circle cx="17.5" cy="18.5" r="1.8" />
        </svg>
      );
    case "commands":
      return (
        <svg {...common}>
          <path d="M4 17l6-6-6-6M12 19h8" />
        </svg>
      );
    case "trace":
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="2.2" />
          <circle cx="18" cy="6" r="2.2" />
          <circle cx="6" cy="18" r="2.2" />
          <circle cx="18" cy="18" r="2.2" />
          <circle cx="12" cy="12" r="2.2" />
          <path d="M6 8v8M18 8v8M8 6h8M8 18h8" />
        </svg>
      );
    case "plugins":
      return (
        <svg {...common}>
          <path d="M12 2v8M12 14v8M2 12h8M14 12h8" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      );
  }
}

const NAV: { to: string; label: string; end?: boolean; icon: IconName }[] = [
  { to: "/", label: "概览", end: true, icon: "overview" },
  { to: "/chat", label: "对话", icon: "chat" },
  { to: "/runs", label: "运行", icon: "runs" },
  { to: "/tasks", label: "任务", icon: "tasks" },
  { to: "/memory", label: "记忆", icon: "memory" },
  { to: "/connectors", label: "连接器", icon: "connectors" },
  { to: "/channels", label: "渠道", icon: "channels" },
  { to: "/trace", label: "Trace", icon: "trace" },
  { to: "/deliveries", label: "投递", icon: "deliveries" },
  { to: "/commands", label: "命令", icon: "commands" },
  { to: "/plugins", label: "插件", icon: "plugins" },
];

function BrandMark() {
  return (
    <div className="flex items-center gap-2.5 px-2">
      <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-primary text-white shadow-warm">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v3M12 19v3M2 12h3M19 12h3" strokeLinecap="round" />
        </svg>
      </div>
      <div className="leading-tight">
        <div className="text-sm font-extrabold tracking-wide text-ink">COGITO</div>
        <div className="text-[10px] font-medium text-muted">Agent 运行时</div>
      </div>
    </div>
  );
}

function Sidebar() {
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-borderc bg-surface/70 p-4 backdrop-blur lg:flex">
      <BrandMark />
      <nav className="mt-6 flex flex-col gap-1">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) => `nav-link ${isActive ? "nav-link-active" : ""}`}
          >
            <NavIcon name={n.icon} />
            {n.label}
          </NavLink>
        ))}
      </nav>
      <div className="mt-auto rounded-xl bg-surface-2 p-3 text-[11px] leading-relaxed text-muted">
        暖色调可观测面板 · 与 QQ / Terminal 共享同一条 Core 主链路。
      </div>
    </aside>
  );
}

function TopNav() {
  return (
    <nav className="flex gap-1 overflow-x-auto border-b border-borderc bg-surface/70 px-3 py-2 lg:hidden">
      {NAV.map((n) => (
        <NavLink
          key={n.to}
          to={n.to}
          end={n.end}
          className={({ isActive }) =>
            `nav-link whitespace-nowrap ${isActive ? "nav-link-active" : ""}`
          }
        >
          <NavIcon name={n.icon} />
          {n.label}
        </NavLink>
      ))}
    </nav>
  );
}

const turnsCfg: ResourceListConfig = {
  title: "运行 (Runs / Turns)",
  statusOptions: ["queued", "running", "completed", "failed", "cancelled"],
  fetchList: (status?: string) => api.turns(status ? { status } : {}),
  fetchDetail: async (id: string) => {
    const d = await api.turn(id);
    return { item: d.turn, attempts: d.attempts };
  },
  rowKey: "turn_id",
  detailAttemptsLabel: "RunAttempts",
  cols: [
    { key: "turn_id", label: "ID", render: (r) => <span className="text-[11px]">{String(r.turn_id).slice(0, 10)}</span> },
    { key: "status", label: "状态", render: (r) => <StatusPill status={String(r.status)} /> },
    { key: "channel", label: "渠道", render: (r) => <span className="text-[11px] text-muted">{String(r.channel ?? "-")}</span> },
    { key: "session_id", label: "会话", render: (r) => (
      <Link to={`/trace/${r.session_id}`} className="text-[11px] font-medium text-primary hover:text-primary-strong" title="查看该会话 Trace">
        {String(r.session_id).slice(0, 8)} 🔍
      </Link>
    ) },
    { key: "created_at", label: "创建时间" },
  ],
};

const tasksCfg: ResourceListConfig = {
  title: "任务 (Tasks)",
  statusOptions: ["queued", "running", "failed", "completed", "cancelled"],
  fetchList: (status?: string) => api.tasks(status ? { status } : {}),
  fetchDetail: async (id: string) => {
    const d = await api.task(id);
    return { item: d.task, attempts: d.attempts };
  },
  rowKey: "task_id",
  detailAttemptsLabel: "TaskAttempts",
  cols: [
    { key: "task_id", label: "ID", render: (r) => <span className="text-[11px]">{String(r.task_id).slice(0, 10)}</span> },
    { key: "task_type", label: "类型" },
    { key: "status", label: "状态", render: (r) => <StatusPill status={String(r.status)} /> },
    { key: "priority", label: "优先级" },
    { key: "origin", label: "来源" },
  ],
};

export default function App() {
  return (
    <div className="flex h-full min-h-screen bg-cream text-ink">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopNav />
        <main className="flex-1 overflow-y-auto px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto max-w-6xl">
            <MockBanner />
            <Routes>
              <Route path="/" element={<Overview />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/runs" element={<ResourceList cfg={turnsCfg} />} />
              <Route path="/runs/:id" element={<ResourceList cfg={turnsCfg} />} />
              <Route path="/tasks" element={<ResourceList cfg={tasksCfg} />} />
              <Route path="/tasks/:id" element={<ResourceList cfg={tasksCfg} />} />
              <Route path="/memory" element={<MemoryPage />} />
              <Route path="/connectors" element={<ConnectorsPage />} />
              <Route path="/channels" element={<ChannelsPage />} />
              <Route path="/trace" element={<TracePage />} />
              <Route path="/trace/:id" element={<TracePage />} />
              <Route path="/deliveries" element={<DeliveriesPage />} />
              <Route path="/commands" element={<CommandsPage />} />
              <Route path="/plugins" element={<PluginsPage />} />
            </Routes>
          </div>
        </main>
      </div>
    </div>
  );
}
