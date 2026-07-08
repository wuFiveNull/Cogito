import { NavLink, Route, Routes } from "react-router-dom";
import Overview from "./pages/Overview";
import { ResourceList, type ResourceListConfig } from "./pages/ResourceList";
import { api } from "./api";
import MemoryPage from "./pages/Memory";
import ConnectorsPage from "./pages/Connectors";
import ChannelsPage from "./pages/Channels";
import CommandsPage from "./pages/Commands";
import PluginsPage from "./pages/Plugins";
import ChatPage from "./pages/Chat";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/chat", label: "Chat" },
  { to: "/runs", label: "Runs" },
  { to: "/tasks", label: "Tasks" },
  { to: "/memory", label: "Memory" },
  { to: "/connectors", label: "Connectors" },
  { to: "/channels", label: "Channels" },
  { to: "/deliveries", label: "Deliveries" },
  { to: "/commands", label: "Commands" },
  { to: "/plugins", label: "Plugins" },
];

function Sidebar() {
  return (
    <aside className="hidden w-56 shrink-0 border-r border-[#232c45] bg-[#0d1220] p-4 lg:block">
      <div className="mb-6 px-2 text-sm font-semibold tracking-wide text-[#e6e9f2]">COGITO</div>
      <nav className="flex flex-col gap-1">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) =>
              `rounded-lg px-3 py-2 text-sm transition ${isActive ? "bg-[#7c5cff]/15 text-[#e6e9f2]" : "text-[#8b93ad] hover:bg-[#1b2236] hover:text-[#e6e9f2]"}`
            }
          >
            {n.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}

const turnsCfg: ResourceListConfig = {
  title: "Runs (Turns)",
  statusOptions: ["queued", "running", "completed", "failed", "cancelled"],
  fetchList: (status?: string) => api.turns(status ? { status } : {}),
  fetchDetail: async (id: string) => {
    const d = await api.turn(id);
    return { item: d.turn, attempts: d.attempts };
  },
  rowKey: "turn_id",
  detailAttemptsLabel: "RunAttempts",
  cols: [
    { key: "turn_id", label: "ID", render: (r: Record<string, unknown>) => <span className="text-[11px]">{String(r.turn_id).slice(0, 10)}</span> },
    { key: "status", label: "Status", render: (r: Record<string, unknown>) => <span className={`pill ${r.status === "completed" ? "bg-ok/15 text-ok" : r.status === "failed" ? "bg-warn/15 text-warn" : "bg-[#1b2236] text-[#8b93ad]"}`}>{String(r.status)}</span> },
    { key: "session_id", label: "Session", render: (r: Record<string, unknown>) => <span className="text-[11px]">{String(r.session_id).slice(0, 8)}</span> },
    { key: "created_at", label: "Created" },
  ],
};

const tasksCfg: ResourceListConfig = {
  title: "Tasks",
  statusOptions: ["queued", "running", "failed", "completed", "cancelled"],
  fetchList: (status?: string) => api.tasks(status ? { status } : {}),
  fetchDetail: async (id: string) => {
    const d = await api.task(id);
    return { item: d.task, attempts: d.attempts };
  },
  rowKey: "task_id",
  detailAttemptsLabel: "TaskAttempts",
  cols: [
    { key: "task_id", label: "ID", render: (r: Record<string, unknown>) => <span className="text-[11px]">{String(r.task_id).slice(0, 10)}</span> },
    { key: "task_type", label: "Type" },
    { key: "status", label: "Status", render: (r: Record<string, unknown>) => <span className={`pill ${r.status === "completed" ? "bg-ok/15 text-ok" : r.status === "failed" ? "bg-warn/15 text-warn" : "bg-[#1b2236] text-[#8b93ad]"}`}>{String(r.status)}</span> },
    { key: "priority", label: "Prio" },
    { key: "origin", label: "Origin" },
  ],
};

export default function App() {
  return (
    <div className="flex h-full min-h-screen">
      <Sidebar />
      <main className="flex-1 overflow-y-auto p-6">
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
          <Route path="/deliveries" element={<DeliveriesPage />} />
          <Route path="/commands" element={<CommandsPage />} />
          <Route path="/plugins" element={<PluginsPage />} />
        </Routes>
      </main>
    </div>
  );
}

import DeliveriesPage from "./pages/Deliveries";
