import { Link, NavLink, Route, Routes } from "react-router-dom";
import Overview from "./pages/Overview";
import { ResourceList, type ResourceListConfig } from "./pages/ResourceList";
import { api } from "./api";
import MemoryPage from "./pages/Memory";
import ConnectorsPage from "./pages/Connectors";
import ChannelsPage from "./pages/Channels";
import CommandsPage from "./pages/Commands";
import ChatPage from "./pages/Chat";
import DeliveriesPage from "./pages/Deliveries";
import TracePage from "./pages/Trace";
import ProactivePage from "./pages/Proactive";
import CapabilitiesPage from "./pages/Capabilities";
import SystemPage from "./pages/System";
import TasksPage from "./pages/Tasks";
import { MockBanner } from "./components";

// ── 图标（内联 SVG，无外部依赖） ─────────────────────────────
type IconName =
  | "overview"
  | "chat"
  | "runs"
  | "tasks"
  | "proactive"
  | "deliveries"
  | "connectors"
  | "memory"
  | "capabilities"
  | "trace"
  | "system";

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
    case "proactive":
      return (
        <svg {...common}>
          <path d="M12 2L2 7l10 5 10-5-10-5z" />
          <path d="M2 17l10 5 10-5" />
          <path d="M2 12l10 5 10-5" />
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
    case "connectors":
      return (
        <svg {...common}>
          <path d="M9 2v6M15 16v6" />
          <path d="M6 8h12v8H6z" />
          <path d="M12 8V2M12 16v6" />
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
    case "capabilities":
      return (
        <svg {...common}>
          <path d="M12 2v8M12 14v8M2 12h8M14 12h8" />
          <circle cx="12" cy="12" r="3" />
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
    case "system":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="3" />
          <path d="M12 1v3M12 21v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" />
        </svg>
      );
  }
}

const NAV: { to: string; label: string; end?: boolean; icon: IconName }[] = [
  { to: "/", label: "总览", end: true, icon: "overview" },
  { to: "/chat", label: "对话", icon: "chat" },
  { to: "/runs", label: "运行", icon: "runs" },
  { to: "/tasks", label: "任务", icon: "tasks" },
  { to: "/proactive", label: "主动系统", icon: "proactive" },
  { to: "/deliveries", label: "投递", icon: "deliveries" },
  { to: "/connectors", label: "数据源", icon: "connectors" },
  { to: "/memory", label: "记忆", icon: "memory" },
  { to: "/capabilities", label: "能力", icon: "capabilities" },
  { to: "/trace", label: "Trace & 审计", icon: "trace" },
  { to: "/system", label: "系统", icon: "system" },
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
        <div className="text-[10px] font-medium text-muted">Dashboard · 控制面</div>
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
        暖色调可观测面板 · 所有写操作经 Command API 并审计。
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
    { key: "status", label: "状态", render: (r) => <span className={`pill ${r.status === "completed" ? "bg-ok/15 text-ok" : r.status === "failed" ? "bg-danger/15 text-danger" : "bg-info/15 text-info"}`}>{String(r.status)}</span> },
    { key: "channel", label: "渠道", render: (r) => <span className="text-[11px] text-muted">{String(r.channel ?? "-")}</span> },
    { key: "session_id", label: "会话", render: (r) => (
      <Link to={`/trace/${r.session_id}`} className="text-[11px] font-medium text-primary hover:text-primary-strong" title="查看该会话 Trace">
        {String(r.session_id).slice(0, 8)} 🔍
      </Link>
    ) },
    { key: "created_at", label: "创建时间" },
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
              <Route path="/tasks" element={<TasksPage />} />
              <Route path="/proactive" element={<ProactivePage />} />
              <Route path="/deliveries" element={<DeliveriesPage />} />
              <Route path="/connectors" element={<ConnectorsPage />} />
              <Route path="/channels" element={<ChannelsPage />} />
              <Route path="/memory" element={<MemoryPage />} />
              <Route path="/capabilities" element={<CapabilitiesPage />} />
              <Route path="/trace" element={<TracePage />} />
              <Route path="/trace/:id" element={<TracePage />} />
              <Route path="/trace/:id/:mid" element={<TracePage />} />
              <Route path="/system" element={<SystemPage />} />
              <Route path="/commands" element={<CommandsPage />} />
            </Routes>
          </div>
        </main>
      </div>
    </div>
  );
}
