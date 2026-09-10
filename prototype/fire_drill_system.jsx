import { useState, useEffect, useRef, useCallback } from "react";

const ZONES = [
  { id: "Z1", name: "Floor 1 — Lobby & Reception", capacity: 45, color: "#2563EB" },
  { id: "Z2", name: "Floor 2 — Engineering", capacity: 82, color: "#7C3AED" },
  { id: "Z3", name: "Floor 3 — Finance & HR", capacity: 60, color: "#059669" },
  { id: "Z4", name: "Floor 4 — Executive Suite", capacity: 28, color: "#D97706" },
  { id: "Z5", name: "Floor 5 — Operations", capacity: 71, color: "#DC2626" },
  { id: "B1", name: "Basement — Server Room", capacity: 12, color: "#0891B2" },
];

const ASSEMBLY_POINTS = [
  { id: "AP1", name: "North Car Park", maxCapacity: 200 },
  { id: "AP2", name: "South Garden", maxCapacity: 150 },
  { id: "AP3", name: "East Plaza", maxCapacity: 180 },
];

const ROLES = ["Warden", "Deputy Warden", "First Aider", "Staff", "Visitor", "Contractor"];
const DEPARTMENTS = ["Engineering", "Finance", "HR", "Operations", "Executive", "IT", "Facilities"];
const STATUSES = ["safe", "missing", "injured", "evacuating", "unaccounted"];

function initPersonnel() {
  const names = [
    "Ahmed Al-Rashid","Sarah Chen","James Okonkwo","Fatima Malik","Carlos Rivera",
    "Priya Sharma","Michael Torres","Aisha Diallo","Liam O'Brien","Yuki Tanaka",
    "Omar Hassan","Elena Vasquez","Noah Williams","Meera Patel","David Kim",
    "Zara Ahmed","Tyler Johnson","Amara Nwosu","Jake Anderson","Sofia Rossi",
    "Hassan Al-Farsi","Isabella Clark","Kwame Mensah","Natasha Ivanova","Ravi Gupta",
    "Emma Wright","Tariq Hussain","Chioma Eze","Lucas Pereira","Mia Lindqvist",
    "Yusuf Omar","Grace Nakamura","Ethan Brown","Layla Karimi","Felix Schmidt",
    "Adaeze Obi","Ryan McCarthy","Hana Watanabe","Malik Jefferson","Zoe Andersen",
    "Sami Khalil","Chloe Dubois","Leon Muller","Nadia Osei","Connor Walsh",
    "Amelia Zhang","Hamid Rahimi","Victoria Santos","Jaden Brooks","Lyra Kowalski"
  ];
  return names.map((name, i) => {
    const zone = ZONES[Math.floor(Math.random() * ZONES.length)];
    return {
      id: `P${String(i + 1).padStart(3, "0")}`,
      name,
      role: ROLES[Math.floor(Math.random() * ROLES.length)],
      department: DEPARTMENTS[Math.floor(Math.random() * DEPARTMENTS.length)],
      zone: zone.id,
      status: "unaccounted",
      assemblyPoint: null,
      checkedInAt: null,
      badgeNumber: `B${Math.floor(Math.random() * 90000) + 10000}`,
      phone: `+44 ${Math.floor(Math.random() * 9000) + 1000} ${Math.floor(Math.random() * 900000) + 100000}`,
      medicalNeeds: Math.random() < 0.08,
    };
  });
}

function initDrill() {
  return {
    id: `DRILL-${Date.now()}`,
    name: "Q2 2026 Emergency Evacuation Drill",
    site: "Nexus Corporate HQ — Lahore",
    startTime: null,
    endTime: null,
    status: "standby",
    drillCommander: "Col. Tariq Hussain (Safety Director)",
    incidentType: "Fire — Server Room B1",
    notes: "",
  };
}

const statusColors = {
  safe: { bg: "#DCFCE7", text: "#166534", border: "#4ADE80" },
  missing: { bg: "#FEE2E2", text: "#991B1B", border: "#F87171" },
  injured: { bg: "#FEF3C7", text: "#92400E", border: "#FBBF24" },
  evacuating: { bg: "#DBEAFE", text: "#1E40AF", border: "#60A5FA" },
  unaccounted: { bg: "#F3F4F6", text: "#4B5563", border: "#9CA3AF" },
};

const statusIcons = { safe: "✓", missing: "!", injured: "+", evacuating: ">", unaccounted: "?" };

export default function FireDrillSystem() {
  const [view, setView] = useState("dashboard");
  const [drill, setDrill] = useState(initDrill());
  const [personnel, setPersonnel] = useState(initPersonnel());
  const [elapsed, setElapsed] = useState(0);
  const [alerts, setAlerts] = useState([]);
  const [searchQ, setSearchQ] = useState("");
  const [filterZone, setFilterZone] = useState("all");
  const [filterStatus, setFilterStatus] = useState("all");
  const [filterDept, setFilterDept] = useState("all");
  const [selectedPerson, setSelectedPerson] = useState(null);
  const [autoSim, setAutoSim] = useState(false);
  const [simSpeed, setSimSpeed] = useState(1);
  const timerRef = useRef(null);
  const simRef = useRef(null);

  useEffect(() => {
    if (drill.status === "active" && drill.startTime) {
      timerRef.current = setInterval(() => {
        setElapsed(Math.floor((Date.now() - drill.startTime) / 1000));
      }, 1000);
    }
    return () => clearInterval(timerRef.current);
  }, [drill.status, drill.startTime]);

  useEffect(() => {
    if (autoSim && drill.status === "active") {
      simRef.current = setInterval(() => {
        setPersonnel(prev => {
          const unaccounted = prev.filter(p => p.status === "unaccounted");
          if (unaccounted.length === 0) { setAutoSim(false); return prev; }
          const count = Math.max(1, Math.floor(Math.random() * 3 * simSpeed));
          const toUpdate = unaccounted.slice(0, count);
          return prev.map(p => {
            if (!toUpdate.find(u => u.id === p.id)) return p;
            const r = Math.random();
            const ap = ASSEMBLY_POINTS[Math.floor(Math.random() * ASSEMBLY_POINTS.length)];
            const newStatus = r < 0.85 ? "safe" : r < 0.93 ? "evacuating" : r < 0.97 ? "injured" : "missing";
            return { ...p, status: newStatus, assemblyPoint: ap.id, checkedInAt: new Date().toLocaleTimeString() };
          });
        });
      }, 1200 / simSpeed);
    }
    return () => clearInterval(simRef.current);
  }, [autoSim, drill.status, simSpeed]);

  const pushAlert = useCallback((msg, type = "info") => {
    const id = Date.now() + Math.random();
    setAlerts(a => [{ id, msg, type, time: new Date().toLocaleTimeString() }, ...a.slice(0, 19)]);
  }, []);

  const startDrill = () => {
    const now = Date.now();
    setDrill(d => ({ ...d, status: "active", startTime: now }));
    setElapsed(0);
    pushAlert("DRILL INITIATED — All floor wardens notified", "danger");
    pushAlert("Evacuation alarms triggered across all zones", "warning");
    pushAlert("MQTT tracking system online", "info");
  };

  const endDrill = () => {
    clearInterval(timerRef.current);
    clearInterval(simRef.current);
    setAutoSim(false);
    setDrill(d => ({ ...d, status: "completed", endTime: Date.now() }));
    pushAlert("DRILL COMPLETED — Final report generated", "success");
  };

  const resetDrill = () => {
    clearInterval(timerRef.current);
    clearInterval(simRef.current);
    setAutoSim(false);
    setDrill(initDrill());
    setPersonnel(initPersonnel());
    setElapsed(0);
    setAlerts([]);
  };

  const updatePersonStatus = (personId, newStatus) => {
    setPersonnel(prev => prev.map(p => {
      if (p.id !== personId) return p;
      const ap = ASSEMBLY_POINTS[Math.floor(Math.random() * ASSEMBLY_POINTS.length)];
      pushAlert(`${p.name} marked as ${newStatus}`, newStatus === "safe" ? "success" : newStatus === "missing" ? "danger" : "warning");
      return { ...p, status: newStatus, assemblyPoint: p.assemblyPoint || ap.id, checkedInAt: new Date().toLocaleTimeString() };
    }));
  };

  const markAllInZone = (zoneId, status) => {
    setPersonnel(prev => prev.map(p => {
      if (p.zone !== zoneId || p.status !== "unaccounted") return p;
      const ap = ASSEMBLY_POINTS[Math.floor(Math.random() * ASSEMBLY_POINTS.length)];
      return { ...p, status, assemblyPoint: ap.id, checkedInAt: new Date().toLocaleTimeString() };
    }));
    pushAlert(`Bulk update: Zone ${zoneId} all unaccounted -> ${status}`, "info");
  };

  const stats = {
    total: personnel.length,
    safe: personnel.filter(p => p.status === "safe").length,
    missing: personnel.filter(p => p.status === "missing").length,
    injured: personnel.filter(p => p.status === "injured").length,
    evacuating: personnel.filter(p => p.status === "evacuating").length,
    unaccounted: personnel.filter(p => p.status === "unaccounted").length,
    accounted: personnel.filter(p => ["safe","injured","missing"].includes(p.status)).length,
  };

  const pct = n => Math.round((n / Math.max(stats.total, 1)) * 100);

  const formatElapsed = s => {
    const m = Math.floor(s / 60), sec = s % 60;
    return `${String(m).padStart(2,"0")}:${String(sec).padStart(2,"0")}`;
  };

  const filteredPersonnel = personnel.filter(p => {
    const q = searchQ.toLowerCase();
    const matchQ = !q || p.name.toLowerCase().includes(q) || p.id.toLowerCase().includes(q) || p.badgeNumber.toLowerCase().includes(q);
    const matchZ = filterZone === "all" || p.zone === filterZone;
    const matchS = filterStatus === "all" || p.status === filterStatus;
    const matchD = filterDept === "all" || p.department === filterDept;
    return matchQ && matchZ && matchS && matchD;
  });

  const zoneStats = ZONES.map(z => {
    const zp = personnel.filter(p => p.zone === z.id);
    return {
      ...z,
      total: zp.length,
      safe: zp.filter(p => p.status === "safe").length,
      missing: zp.filter(p => p.status === "missing").length,
      injured: zp.filter(p => p.status === "injured").length,
      evacuating: zp.filter(p => p.status === "evacuating").length,
      unaccounted: zp.filter(p => p.status === "unaccounted").length,
    };
  });

  const apStats = ASSEMBLY_POINTS.map(ap => ({
    ...ap,
    count: personnel.filter(p => p.assemblyPoint === ap.id && p.status === "safe").length,
    injured: personnel.filter(p => p.assemblyPoint === ap.id && p.status === "injured").length,
    total: personnel.filter(p => p.assemblyPoint === ap.id).length,
  }));

  const drillDuration = drill.endTime && drill.startTime
    ? Math.floor((drill.endTime - drill.startTime) / 1000) : elapsed;

  const navItems = [
    { id: "dashboard", label: "Command Center", icon: "ti-layout-dashboard" },
    { id: "personnel", label: "Personnel Tracker", icon: "ti-users" },
    { id: "zones", label: "Zone Status", icon: "ti-building" },
    { id: "assembly", label: "Assembly Points", icon: "ti-map-pin" },
    { id: "alerts", label: "Alert Log", icon: "ti-bell" },
    { id: "report", label: "Drill Report", icon: "ti-file-text" },
  ];

  const mono = "'IBM Plex Mono','Courier New',monospace";

  const s = {
    app: { fontFamily: mono, minHeight:"100vh", background:"#080C18", color:"#CBD5E1", fontSize:"12px", display:"flex" },
    sidebar: { width:"210px", minWidth:"210px", background:"#0B0F1E", borderRight:"1px solid rgba(255,255,255,0.06)", display:"flex", flexDirection:"column" },
    logo: { padding:"18px 16px 14px", borderBottom:"1px solid rgba(255,255,255,0.06)" },
    logoMark: { display:"flex", alignItems:"center", gap:"8px", marginBottom:"4px" },
    flameIcon: { width:"20px", height:"20px", background:"#EF4444", borderRadius:"3px", display:"flex", alignItems:"center", justifyContent:"center", fontSize:"11px", color:"#fff", fontWeight:"700" },
    logoText: { fontSize:"12px", fontWeight:"700", letterSpacing:"0.12em", color:"#F1F5F9" },
    logoSub: { fontSize:"9px", color:"#334155", letterSpacing:"0.1em", marginTop:"1px" },
    nav: { flex:1, paddingTop:"6px" },
    navItem: (a) => ({ display:"flex", alignItems:"center", gap:"8px", padding:"8px 16px", cursor:"pointer", fontSize:"11px", letterSpacing:"0.04em", color: a ? "#F1F5F9" : "#475569", background: a ? "rgba(59,130,246,0.1)" : "transparent", borderLeft: a ? "2px solid #3B82F6" : "2px solid transparent", transition:"all .12s" }),
    sidebarFooter: { padding:"12px 16px", borderTop:"1px solid rgba(255,255,255,0.06)" },
    main: { flex:1, display:"flex", flexDirection:"column", overflow:"hidden" },
    topbar: { padding:"10px 20px", borderBottom:"1px solid rgba(255,255,255,0.06)", display:"flex", alignItems:"center", justifyContent:"space-between", background:"#080C18", gap:"12px" },
    content: { flex:1, padding:"18px 20px", overflowY:"auto" },
    card: { background:"#0B0F1E", border:"1px solid rgba(255,255,255,0.07)", borderRadius:"4px", padding:"14px" },
    cardTitle: { fontSize:"9px", fontWeight:"700", letterSpacing:"0.14em", color:"#334155", textTransform:"uppercase", marginBottom:"10px" },
    btn: (v) => {
      const variants = {
        danger: { background:"#450a0a", borderColor:"#7f1d1d", color:"#fca5a5" },
        success: { background:"#052e16", borderColor:"#14532d", color:"#86efac" },
        primary: { background:"#172554", borderColor:"#1e3a8a", color:"#bfdbfe" },
        ghost: { background:"transparent", borderColor:"rgba(255,255,255,0.1)", color:"#64748b" },
        warning: { background:"#451a03", borderColor:"#78350f", color:"#fde68a" },
      }[v] || { background:"transparent", borderColor:"rgba(255,255,255,0.1)", color:"#64748b" };
      return { padding:"6px 14px", borderRadius:"3px", border:`1px solid ${variants.borderColor}`, background:variants.background, color:variants.color, cursor:"pointer", fontSize:"10px", fontWeight:"700", letterSpacing:"0.08em", fontFamily:mono, transition:"opacity .15s" };
    },
    statCard: { background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.06)", borderRadius:"4px", padding:"12px 14px" },
    statNum: (c) => ({ fontSize:"26px", fontWeight:"700", color: c, letterSpacing:"-0.02em", lineHeight:1 }),
    statLbl: { fontSize:"9px", color:"#334155", letterSpacing:"0.1em", marginTop:"4px" },
    badge: (status) => {
      const sc = { active:"#EF4444", completed:"#3B82F6", standby:"#475569" }[status] || "#475569";
      return { padding:"3px 10px", borderRadius:"2px", fontSize:"9px", fontWeight:"700", letterSpacing:"0.12em", background:`${sc}18`, color:sc, border:`1px solid ${sc}40` };
    },
    statusTag: (st) => {
      const c = { safe:"#22c55e", missing:"#ef4444", injured:"#f59e0b", evacuating:"#3b82f6", unaccounted:"#475569" }[st] || "#475569";
      return { display:"inline-flex", alignItems:"center", gap:"3px", padding:"2px 7px", borderRadius:"2px", fontSize:"9px", fontWeight:"700", letterSpacing:"0.08em", background:`${c}18`, color:c, border:`1px solid ${c}40` };
    },
    bar: (pctVal, color) => (
      <div style={{ height:"3px", borderRadius:"2px", background:"rgba(255,255,255,0.06)", overflow:"hidden" }}>
        <div style={{ height:"100%", width:`${Math.min(100,Math.max(0,pctVal))}%`, background:color, transition:"width .5s" }}></div>
      </div>
    ),
    input: { background:"rgba(255,255,255,0.04)", border:"1px solid rgba(255,255,255,0.08)", borderRadius:"3px", color:"#CBD5E1", padding:"7px 10px", fontSize:"11px", fontFamily:mono, outline:"none" },
    select: { background:"#0B0F1E", border:"1px solid rgba(255,255,255,0.08)", borderRadius:"3px", color:"#94A3B8", padding:"7px 10px", fontSize:"11px", fontFamily:mono, cursor:"pointer" },
    tableHead: { display:"grid", padding:"7px 12px", borderBottom:"1px solid rgba(255,255,255,0.06)", background:"#070A14" },
    tableRow: (alt) => ({ display:"grid", padding:"8px 12px", borderBottom:"1px solid rgba(255,255,255,0.04)", background: alt ? "#09102A" : "#0B0F1E", cursor:"pointer" }),
  };

  const AlertDot = ({ type }) => {
    const c = { danger:"#EF4444", success:"#22C55E", warning:"#F59E0B", info:"#3B82F6" }[type] || "#3B82F6";
    return <span style={{ display:"inline-block", width:"5px", height:"5px", borderRadius:"50%", background:c, flexShrink:0, marginTop:"5px" }}></span>;
  };

  return (
    <div style={s.app}>
      <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;700&display=swap" rel="stylesheet"/>

      <div style={s.sidebar}>
        <div style={s.logo}>
          <div style={s.logoMark}>
            <div style={s.flameIcon}>F</div>
            <span style={s.logoText}>SafeEgress</span>
          </div>
          <div style={s.logoSub}>FIRE DRILL COMMAND v2.6</div>
        </div>
        <div style={s.nav}>
          {navItems.map(n => (
            <div key={n.id} style={s.navItem(view === n.id)} onClick={() => setView(n.id)}>
              <i className={`ti ${n.icon}`} style={{ fontSize:"14px", width:"16px" }} aria-hidden="true"></i>
              {n.label}
              {n.id === "alerts" && alerts.length > 0 && (
                <span style={{ marginLeft:"auto", background:"#1d4ed8", color:"#bfdbfe", borderRadius:"10px", padding:"1px 6px", fontSize:"9px", fontWeight:"700" }}>{alerts.length}</span>
              )}
            </div>
          ))}
        </div>
        <div style={s.sidebarFooter}>
          <div style={{ fontSize:"9px", color:"#1E293B", letterSpacing:"0.08em", marginBottom:"2px" }}>DRILL ID</div>
          <div style={{ fontSize:"9px", color:"#334155", wordBreak:"break-all" }}>{drill.id.replace("DRILL-","")}</div>
          <div style={{ fontSize:"9px", color:"#1E293B", letterSpacing:"0.08em", marginTop:"8px", marginBottom:"2px" }}>SITE</div>
          <div style={{ fontSize:"9px", color:"#334155" }}>{drill.site}</div>
        </div>
      </div>

      <div style={s.main}>
        <div style={s.topbar}>
          <div style={{ display:"flex", alignItems:"center", gap:"12px" }}>
            <span style={s.badge(drill.status)}>{drill.status.toUpperCase()}</span>
            {drill.status === "active" && (
              <span style={{ fontSize:"9px", color:"#EF4444", letterSpacing:"0.1em", display:"flex", alignItems:"center", gap:"4px" }}>
                <span style={{ width:"6px", height:"6px", background:"#EF4444", borderRadius:"50%", display:"inline-block", animation:"pulse 1s ease-in-out infinite" }}></span>
                LIVE
              </span>
            )}
            <span style={{ fontSize:"10px", color:"#334155" }}>{drill.incidentType}</span>
          </div>
          <div style={{ display:"flex", alignItems:"center", gap:"10px" }}>
            <span style={{ fontSize:"20px", fontWeight:"700", color: drill.status === "active" ? "#F87171" : "#334155", letterSpacing:"0.05em", fontFamily:mono }}>
              {formatElapsed(elapsed)}
            </span>
            {drill.status === "standby" && <button style={s.btn("danger")} onClick={startDrill}>INITIATE DRILL</button>}
            {drill.status === "active" && <>
              <label style={{ display:"flex", alignItems:"center", gap:"5px", fontSize:"10px", color:"#475569", cursor:"pointer" }}>
                <input type="checkbox" checked={autoSim} onChange={e => setAutoSim(e.target.checked)} />
                AUTO-SIM
              </label>
              <select style={{ ...s.select, padding:"4px 8px", fontSize:"10px" }} value={simSpeed} onChange={e => setSimSpeed(Number(e.target.value))}>
                <option value={1}>1x</option><option value={2}>2x</option><option value={5}>5x</option>
              </select>
              <button style={s.btn("success")} onClick={endDrill}>END DRILL</button>
            </>}
            {drill.status === "completed" && <button style={s.btn("ghost")} onClick={resetDrill}>RESET SYSTEM</button>}
          </div>
        </div>

        <div style={s.content}>

          {view === "dashboard" && (
            <div>
              <div style={{ marginBottom:"14px" }}>
                <div style={{ fontSize:"10px", color:"#1E293B", letterSpacing:"0.12em", textTransform:"uppercase", marginBottom:"2px" }}>
                  {drill.name}
                </div>
                <div style={{ fontSize:"11px", color:"#334155" }}>
                  Commander: {drill.drillCommander}
                </div>
              </div>

              <div style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:"10px", marginBottom:"12px" }}>
                {[
                  { lbl:"Total Personnel", val:stats.total, color:"#94A3B8" },
                  { lbl:"Accounted", val:stats.accounted, color:"#60A5FA", sub:`${pct(stats.accounted)}%` },
                  { lbl:"Confirmed Safe", val:stats.safe, color:"#4ADE80", sub:`${pct(stats.safe)}%` },
                  { lbl:"Unaccounted", val:stats.unaccounted, color:"#F87171", sub:`${pct(stats.unaccounted)}%` },
                ].map(m => (
                  <div key={m.lbl} style={s.statCard}>
                    <div style={s.cardTitle}>{m.lbl}</div>
                    <div style={s.statNum(m.color)}>{m.val}</div>
                    {m.sub && <div style={{ fontSize:"11px", color:m.color, marginTop:"3px" }}>{m.sub}</div>}
                  </div>
                ))}
              </div>

              <div style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:"10px", marginBottom:"12px" }}>
                {[
                  { lbl:"Missing", val:stats.missing, color:"#F87171" },
                  { lbl:"Injured", val:stats.injured, color:"#FBBF24" },
                  { lbl:"Evacuating", val:stats.evacuating, color:"#60A5FA" },
                  { lbl:"Drill Time", val:formatElapsed(elapsed), color: drill.status === "active" ? "#F87171" : "#334155" },
                ].map(m => (
                  <div key={m.lbl} style={s.statCard}>
                    <div style={s.cardTitle}>{m.lbl}</div>
                    <div style={{ ...s.statNum(m.color), fontSize:"22px" }}>{m.val}</div>
                  </div>
                ))}
              </div>

              <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"12px", marginBottom:"12px" }}>
                <div style={s.card}>
                  <div style={s.cardTitle}>Evacuation Progress</div>
                  {[
                    { st:"safe", label:"Safe", count:stats.safe, color:"#22C55E" },
                    { st:"evacuating", label:"Evacuating", count:stats.evacuating, color:"#3B82F6" },
                    { st:"injured", label:"Injured", count:stats.injured, color:"#F59E0B" },
                    { st:"missing", label:"Missing", count:stats.missing, color:"#EF4444" },
                    { st:"unaccounted", label:"Unaccounted", count:stats.unaccounted, color:"#334155" },
                  ].map(row => (
                    <div key={row.st} style={{ marginBottom:"9px" }}>
                      <div style={{ display:"flex", justifyContent:"space-between", marginBottom:"4px" }}>
                        <span style={{ fontSize:"10px", color:row.color }}>{row.label}</span>
                        <span style={{ fontSize:"10px", color:row.color }}>{row.count} ({pct(row.count)}%)</span>
                      </div>
                      {s.bar(pct(row.count), row.color)}
                    </div>
                  ))}
                </div>

                <div style={s.card}>
                  <div style={s.cardTitle}>Zone Accountability</div>
                  {zoneStats.map(z => (
                    <div key={z.id} style={{ display:"flex", alignItems:"center", gap:"8px", marginBottom:"9px" }}>
                      <div style={{ width:"3px", height:"28px", background:z.color, borderRadius:"2px", flexShrink:0 }}></div>
                      <div style={{ flex:1 }}>
                        <div style={{ display:"flex", justifyContent:"space-between", marginBottom:"3px" }}>
                          <span style={{ fontSize:"10px", color:"#CBD5E1" }}>{z.id} · {z.name.split("—")[1]?.trim() || z.name}</span>
                          <span style={{ fontSize:"10px", color: z.unaccounted > 0 ? "#F87171" : "#4ADE80" }}>
                            {z.total - z.unaccounted}/{z.total}
                          </span>
                        </div>
                        {s.bar(Math.round(((z.total - z.unaccounted) / Math.max(z.total,1)) * 100), z.color)}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"12px" }}>
                <div style={s.card}>
                  <div style={s.cardTitle}>Assembly Points</div>
                  {apStats.map(ap => (
                    <div key={ap.id} style={{ padding:"8px 0", borderBottom:"1px solid rgba(255,255,255,0.05)", display:"flex", justifyContent:"space-between", alignItems:"center" }}>
                      <div>
                        <div style={{ fontSize:"11px", color:"#CBD5E1" }}>{ap.name}</div>
                        <div style={{ fontSize:"9px", color:"#334155", marginTop:"1px" }}>Cap: {ap.maxCapacity}</div>
                      </div>
                      <div style={{ textAlign:"right" }}>
                        <div style={{ fontSize:"18px", fontWeight:"700", color:"#4ADE80" }}>{ap.count}</div>
                        {ap.injured > 0 && <div style={{ fontSize:"9px", color:"#FBBF24" }}>{ap.injured} injured</div>}
                      </div>
                    </div>
                  ))}
                </div>

                <div style={s.card}>
                  <div style={s.cardTitle}>Recent Alerts</div>
                  {alerts.length === 0 && <div style={{ fontSize:"11px", color:"#1E293B" }}>No alerts yet.</div>}
                  {alerts.slice(0,7).map(a => (
                    <div key={a.id} style={{ display:"flex", gap:"8px", padding:"5px 0", borderBottom:"1px solid rgba(255,255,255,0.04)" }}>
                      <AlertDot type={a.type} />
                      <span style={{ fontSize:"9px", color:"#334155", minWidth:"40px" }}>{a.time}</span>
                      <span style={{ fontSize:"10px", color: a.type==="danger"?"#F87171":a.type==="success"?"#4ADE80":a.type==="warning"?"#FBBF24":"#94A3B8" }}>{a.msg}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {view === "personnel" && (
            <div>
              <div style={{ display:"flex", gap:"8px", marginBottom:"12px", flexWrap:"wrap" }}>
                <input style={{ ...s.input, flex:"1", minWidth:"160px" }} placeholder="Search name, ID, badge number..." value={searchQ} onChange={e => setSearchQ(e.target.value)} />
                <select style={s.select} value={filterZone} onChange={e => setFilterZone(e.target.value)}>
                  <option value="all">All zones</option>
                  {ZONES.map(z => <option key={z.id} value={z.id}>{z.id}</option>)}
                </select>
                <select style={s.select} value={filterStatus} onChange={e => setFilterStatus(e.target.value)}>
                  <option value="all">All statuses</option>
                  {STATUSES.map(st => <option key={st} value={st}>{st}</option>)}
                </select>
                <select style={s.select} value={filterDept} onChange={e => setFilterDept(e.target.value)}>
                  <option value="all">All depts</option>
                  {DEPARTMENTS.map(d => <option key={d} value={d}>{d}</option>)}
                </select>
              </div>

              <div style={{ ...s.card, padding:0, overflow:"hidden" }}>
                <div style={{ ...s.tableHead, gridTemplateColumns:"60px 1fr 50px 80px 90px 90px 80px" }}>
                  {["ID","Name","Zone","Dept","Role","Status","Time"].map(h => (
                    <div key={h} style={{ fontSize:"9px", fontWeight:"700", letterSpacing:"0.1em", color:"#1E293B" }}>{h.toUpperCase()}</div>
                  ))}
                </div>

                {filteredPersonnel.map((p, i) => (
                  <div key={p.id}>
                    <div style={{ ...s.tableRow(i%2===0), gridTemplateColumns:"60px 1fr 50px 80px 90px 90px 80px", alignItems:"center" }}
                      onClick={() => setSelectedPerson(selectedPerson?.id === p.id ? null : p)}>
                      <span style={{ fontSize:"9px", color:"#334155" }}>{p.id}</span>
                      <div>
                        <div style={{ fontSize:"11px", color:"#CBD5E1" }}>{p.name}</div>
                        <div style={{ fontSize:"9px", color:"#1E293B" }}>{p.badgeNumber}{p.medicalNeeds && " · [M]"}</div>
                      </div>
                      <span style={{ fontSize:"10px", color:"#60A5FA" }}>{p.zone}</span>
                      <span style={{ fontSize:"10px", color:"#475569" }}>{p.department.slice(0,8)}</span>
                      <span style={{ fontSize:"10px", color:"#475569" }}>{p.role.slice(0,10)}</span>
                      <span style={s.statusTag(p.status)}>{statusIcons[p.status]} {p.status}</span>
                      <span style={{ fontSize:"9px", color:"#334155" }}>{p.checkedInAt || "—"}</span>
                    </div>
                    {selectedPerson?.id === p.id && (
                      <div style={{ background:"#07091A", borderBottom:"1px solid rgba(255,255,255,0.06)", padding:"10px 12px", display:"flex", gap:"16px", flexWrap:"wrap", alignItems:"flex-start" }}>
                        <div><div style={{ fontSize:"9px", color:"#1E293B" }}>Phone</div><div style={{ fontSize:"11px", color:"#CBD5E1" }}>{p.phone}</div></div>
                        <div><div style={{ fontSize:"9px", color:"#1E293B" }}>Assembly Pt</div><div style={{ fontSize:"11px", color:"#CBD5E1" }}>{p.assemblyPoint || "Not assigned"}</div></div>
                        <div><div style={{ fontSize:"9px", color:"#1E293B" }}>Medical</div><div style={{ fontSize:"11px", color: p.medicalNeeds ? "#FBBF24" : "#334155" }}>{p.medicalNeeds ? "Yes — flag for first aider" : "None"}</div></div>
                        {drill.status === "active" && (
                          <div style={{ display:"flex", gap:"6px", flexWrap:"wrap" }}>
                            {STATUSES.filter(st => st !== p.status).map(st => (
                              <button key={st} style={{ ...s.btn("ghost"), padding:"3px 8px", fontSize:"9px" }}
                                onClick={e => { e.stopPropagation(); updatePersonStatus(p.id, st); }}>
                                Mark {st}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
                <div style={{ padding:"8px 12px", fontSize:"9px", color:"#1E293B" }}>
                  {filteredPersonnel.length} of {personnel.length} personnel shown
                </div>
              </div>
            </div>
          )}

          {view === "zones" && (
            <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"12px" }}>
              {zoneStats.map(z => (
                <div key={z.id} style={{ ...s.card, borderLeft:`3px solid ${z.color}` }}>
                  <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:"10px" }}>
                    <div>
                      <div style={{ fontSize:"12px", fontWeight:"700", color:"#F1F5F9", letterSpacing:"0.06em" }}>{z.id}</div>
                      <div style={{ fontSize:"10px", color:"#475569", marginTop:"2px" }}>{z.name}</div>
                    </div>
                    <div style={{ textAlign:"right" }}>
                      <div style={{ fontSize:"18px", fontWeight:"700", color: z.unaccounted === 0 ? "#4ADE80" : "#F87171" }}>{z.total - z.unaccounted}/{z.total}</div>
                      <div style={{ fontSize:"9px", color:"#334155" }}>accounted</div>
                    </div>
                  </div>

                  <div style={{ display:"grid", gridTemplateColumns:"repeat(5,1fr)", gap:"5px", marginBottom:"10px" }}>
                    {[["Safe",z.safe,"#22c55e"],["Evac",z.evacuating,"#3b82f6"],["Injrd",z.injured,"#f59e0b"],["Miss",z.missing,"#ef4444"],["?",z.unaccounted,"#334155"]].map(([l,v,c]) => (
                      <div key={l} style={{ background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.05)", borderRadius:"3px", padding:"5px 6px", textAlign:"center" }}>
                        <div style={{ fontSize:"14px", fontWeight:"700", color:c }}>{v}</div>
                        <div style={{ fontSize:"8px", color:"#334155" }}>{l}</div>
                      </div>
                    ))}
                  </div>

                  {s.bar(Math.round(((z.total - z.unaccounted) / Math.max(z.total,1)) * 100), z.color)}

                  {drill.status === "active" && z.unaccounted > 0 && (
                    <div style={{ display:"flex", gap:"6px", marginTop:"10px" }}>
                      <button style={{ ...s.btn("success"), fontSize:"9px", padding:"4px 10px" }} onClick={() => markAllInZone(z.id, "safe")}>
                        Mark all safe
                      </button>
                      <button style={{ ...s.btn("ghost"), fontSize:"9px", padding:"4px 10px" }} onClick={() => markAllInZone(z.id, "evacuating")}>
                        Mark evacuating
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {view === "assembly" && (
            <div>
              <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr 1fr", gap:"12px", marginBottom:"12px" }}>
                {apStats.map(ap => (
                  <div key={ap.id} style={s.card}>
                    <div style={{ fontSize:"11px", fontWeight:"700", color:"#F1F5F9", marginBottom:"2px" }}>{ap.name}</div>
                    <div style={{ fontSize:"9px", color:"#334155", marginBottom:"12px" }}>Max capacity: {ap.maxCapacity}</div>
                    <div style={{ fontSize:"28px", fontWeight:"700", color:"#4ADE80", marginBottom:"2px" }}>{ap.count}</div>
                    <div style={{ fontSize:"9px", color:"#334155", marginBottom:"8px" }}>confirmed safe</div>
                    {ap.injured > 0 && (
                      <div style={{ padding:"5px 8px", background:"rgba(245,158,11,0.08)", border:"1px solid rgba(245,158,11,0.3)", borderRadius:"3px", fontSize:"10px", color:"#FBBF24", marginBottom:"8px" }}>
                        {ap.injured} injured present
                      </div>
                    )}
                    {s.bar(Math.round((ap.count / ap.maxCapacity) * 100), "#22C55E")}
                    <div style={{ fontSize:"9px", color:"#334155", marginTop:"4px" }}>{Math.round((ap.count / ap.maxCapacity) * 100)}% of capacity</div>
                  </div>
                ))}
              </div>

              <div style={s.card}>
                <div style={s.cardTitle}>Personnel Roster by Assembly Point</div>
                {ASSEMBLY_POINTS.map(ap => {
                  const apPeople = personnel.filter(p => p.assemblyPoint === ap.id);
                  return (
                    <div key={ap.id} style={{ marginBottom:"14px" }}>
                      <div style={{ fontSize:"10px", fontWeight:"700", color:"#60A5FA", marginBottom:"6px", letterSpacing:"0.06em" }}>
                        {ap.name} — {apPeople.length} persons
                      </div>
                      <div style={{ display:"flex", flexWrap:"wrap", gap:"5px" }}>
                        {apPeople.length === 0 && <span style={{ fontSize:"10px", color:"#1E293B" }}>No one checked in yet</span>}
                        {apPeople.map(p => {
                          const c = { safe:"#22c55e", missing:"#ef4444", injured:"#f59e0b", evacuating:"#3b82f6" }[p.status] || "#334155";
                          return (
                            <div key={p.id} style={{ padding:"3px 9px", background:`${c}10`, border:`1px solid ${c}40`, borderRadius:"2px", fontSize:"10px", color:c }}>
                              {p.name}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {view === "alerts" && (
            <div style={s.card}>
              <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:"12px" }}>
                <div style={s.cardTitle}>System Alert Log ({alerts.length} events)</div>
                <button style={s.btn("ghost")} onClick={() => setAlerts([])}>Clear all</button>
              </div>
              {alerts.length === 0 && <div style={{ fontSize:"11px", color:"#1E293B" }}>No alerts recorded yet.</div>}
              {alerts.map(a => (
                <div key={a.id} style={{ display:"flex", gap:"10px", padding:"7px 0", borderBottom:"1px solid rgba(255,255,255,0.04)", alignItems:"flex-start" }}>
                  <AlertDot type={a.type} />
                  <span style={{ fontSize:"9px", color:"#334155", minWidth:"42px" }}>{a.time}</span>
                  <span style={{ fontSize:"10px", color: a.type==="danger"?"#F87171":a.type==="success"?"#4ADE80":a.type==="warning"?"#FBBF24":"#94A3B8" }}>{a.msg}</span>
                </div>
              ))}
            </div>
          )}

          {view === "report" && (
            <div>
              {drill.status !== "completed" && (
                <div style={{ padding:"16px", ...s.card, marginBottom:"12px", color:"#475569", fontSize:"11px" }}>
                  Full report will be generated upon drill completion. Partial data shown below.
                </div>
              )}
              <div style={s.card}>
                <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:"16px" }}>
                  <div>
                    <div style={{ fontSize:"13px", fontWeight:"700", color:"#F1F5F9", marginBottom:"3px" }}>{drill.name}</div>
                    <div style={{ fontSize:"10px", color:"#334155" }}>Generated: {new Date().toLocaleString()}</div>
                  </div>
                  <span style={s.badge(drill.status)}>{drill.status.toUpperCase()}</span>
                </div>

                <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"8px", marginBottom:"16px" }}>
                  {[
                    ["Site", drill.site], ["Incident", drill.incidentType],
                    ["Commander", drill.drillCommander], ["Duration", formatElapsed(drillDuration)],
                    ["Total Personnel", stats.total], ["Accounted", `${stats.accounted} (${pct(stats.accounted)}%)`],
                    ["Safe", `${stats.safe} (${pct(stats.safe)}%)`], ["Unaccounted", `${stats.unaccounted} (${pct(stats.unaccounted)}%)`],
                  ].map(([k,v]) => (
                    <div key={k} style={{ padding:"7px 10px", background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.06)", borderRadius:"3px", display:"flex", justifyContent:"space-between", gap:"8px" }}>
                      <span style={{ fontSize:"9px", color:"#334155" }}>{k}</span>
                      <span style={{ fontSize:"10px", color:"#CBD5E1", fontWeight:"700", textAlign:"right" }}>{v}</span>
                    </div>
                  ))}
                </div>

                <div style={s.cardTitle}>Zone Breakdown</div>
                {zoneStats.map(z => (
                  <div key={z.id} style={{ display:"flex", alignItems:"center", gap:"8px", padding:"7px 0", borderBottom:"1px solid rgba(255,255,255,0.04)" }}>
                    <div style={{ width:"3px", height:"36px", background:z.color, borderRadius:"2px", flexShrink:0 }}></div>
                    <div style={{ flex:1 }}>
                      <div style={{ fontSize:"11px", color:"#CBD5E1" }}>{z.name}</div>
                      <div style={{ display:"flex", gap:"10px", marginTop:"2px" }}>
                        {[["Safe",z.safe,"#22c55e"],["Injured",z.injured,"#f59e0b"],["Missing",z.missing,"#ef4444"],["Unaccounted",z.unaccounted,"#334155"]].map(([l,v,c]) => (
                          <span key={l} style={{ fontSize:"9px", color:c }}>{l}: {v}</span>
                        ))}
                      </div>
                    </div>
                    <div style={{ fontSize:"14px", fontWeight:"700", color: z.unaccounted === 0 ? "#4ADE80" : "#F87171" }}>
                      {z.total - z.unaccounted}/{z.total}
                    </div>
                  </div>
                ))}

                {personnel.filter(p => ["missing","injured"].includes(p.status)).length > 0 && (
                  <div style={{ marginTop:"14px" }}>
                    <div style={s.cardTitle}>Critical — Missing & Injured Personnel</div>
                    {personnel.filter(p => ["missing","injured"].includes(p.status)).map(p => (
                      <div key={p.id} style={{ display:"flex", gap:"8px", alignItems:"center", padding:"6px 0", borderBottom:"1px solid rgba(255,255,255,0.04)" }}>
                        <span style={s.statusTag(p.status)}>{p.status}</span>
                        <span style={{ fontSize:"11px", color:"#CBD5E1" }}>{p.name}</span>
                        <span style={{ fontSize:"10px", color:"#475569" }}>{p.role} · {p.department} · {p.zone}</span>
                        {p.medicalNeeds && <span style={{ fontSize:"9px", color:"#FBBF24" }}>[MEDICAL NEEDS]</span>}
                      </div>
                    ))}
                  </div>
                )}

                <div style={{ marginTop:"14px", padding:"12px", background:"rgba(255,255,255,0.02)", border:"1px solid rgba(255,255,255,0.06)", borderRadius:"3px" }}>
                  <div style={s.cardTitle}>Compliance Score</div>
                  <div style={{ fontSize:"34px", fontWeight:"700", color: pct(stats.accounted) >= 95 ? "#4ADE80" : pct(stats.accounted) >= 80 ? "#FBBF24" : "#F87171" }}>
                    {pct(stats.accounted)}%
                  </div>
                  <div style={{ fontSize:"10px", color:"#475569", marginTop:"4px" }}>
                    {pct(stats.accounted) >= 95 ? "COMPLIANT — Exceeds 95% required threshold" :
                     pct(stats.accounted) >= 80 ? "WARNING — Below 95% compliance threshold" :
                     "NON-COMPLIANT — Immediate investigation required"}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
